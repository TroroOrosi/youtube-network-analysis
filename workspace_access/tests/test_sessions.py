from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from workspace_access.models import (
    AccessSecret,
    ErrorCode,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceAccessError,
)
from workspace_access.ports import SystemClock, SystemTokenSource
from workspace_access.service import WorkspaceAccessService


START = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = START) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class PredictableTokens:
    def __init__(self) -> None:
        self.counter = 0

    def new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def new_session_secret(self) -> str:
        self.counter += 1
        return f"session-secret-{self.counter}-" + ("x" * 43)


def identity(
    subject: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
) -> VerifiedIdentity:
    return VerifiedIdentity(
        issuer="https://identity.example",
        subject=subject,
        authenticated_at=START,
        verified_email=email,
        display_name=display_name,
    )


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.tokens = PredictableTokens()
        self.service = WorkspaceAccessService(
            clock=self.clock,
            token_source=self.tokens,
        )

    def evidence(self, raw_secret: str) -> SessionEvidence:
        return SessionEvidence(AccessSecret(raw_secret))

    def test_verified_issuer_subject_maps_to_one_user_not_email(self) -> None:
        first = self.service.establish_session(
            identity(
                "subject-1",
                email="first@example.test",
                display_name="First name",
            )
        )
        second = self.service.establish_session(
            identity(
                "subject-1",
                email="updated@example.test",
                display_name="Updated name",
            )
        )
        different_subject = self.service.establish_session(
            identity("subject-2", email="updated@example.test")
        )

        first_authenticated = self.service.authenticate_session(
            self.evidence(first.secret.reveal())
        )
        second_authenticated = self.service.authenticate_session(
            self.evidence(second.secret.reveal())
        )
        different_subject_authenticated = self.service.authenticate_session(
            self.evidence(different_subject.secret.reveal())
        )
        self.assertEqual(first_authenticated.user_id, second_authenticated.user_id)
        self.assertNotEqual(
            first_authenticated.user_id,
            different_subject_authenticated.user_id,
        )
        self.assertNotEqual(first.session_id, second.session_id)

    def test_raw_secret_is_returned_once_but_only_digest_is_retained(self) -> None:
        issued = self.service.establish_session(identity("subject-1"))
        raw_secret = issued.secret.reveal()
        expected_digest = hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()

        self.assertEqual(
            self.service._debug_session_digests(),  # noqa: SLF001 - security invariant
            frozenset({expected_digest}),
        )
        self.assertNotIn(raw_secret, repr(self.service))
        self.assertNotIn(raw_secret, repr(issued))

    def test_success_slides_idle_expiry_without_extending_absolute_expiry(self) -> None:
        issued = self.service.establish_session(identity("subject-1"))
        self.clock.advance(timedelta(minutes=29))

        authenticated = self.service.authenticate_session(
            self.evidence(issued.secret.reveal())
        )

        self.assertEqual(
            authenticated.idle_expires_at,
            START + timedelta(minutes=59),
        )
        self.assertEqual(
            authenticated.absolute_expires_at,
            START + timedelta(hours=12),
        )

    def test_idle_and_absolute_cutoffs_are_exclusive(self) -> None:
        idle_session = self.service.establish_session(identity("idle-user"))
        self.clock.advance(timedelta(minutes=30))
        with self.assertRaises(WorkspaceAccessError) as idle_error:
            self.service.authenticate_session(
                self.evidence(idle_session.secret.reveal())
            )
        self.assertEqual(
            idle_error.exception.code,
            ErrorCode.SESSION_EXPIRED_OR_REVOKED.value,
        )

        self.clock.current = START
        absolute_session = self.service.establish_session(identity("absolute-user"))
        self.clock.current = START + timedelta(hours=12)
        with self.assertRaises(WorkspaceAccessError) as absolute_error:
            self.service.authenticate_session(
                self.evidence(absolute_session.secret.reveal())
            )
        self.assertEqual(
            absolute_error.exception.code,
            ErrorCode.SESSION_EXPIRED_OR_REVOKED.value,
        )

    def test_logout_is_idempotent_and_revokes_current_session(self) -> None:
        issued = self.service.establish_session(identity("subject-1"))
        evidence = self.evidence(issued.secret.reveal())

        self.service.logout(evidence)
        self.service.logout(evidence)

        with self.assertRaises(WorkspaceAccessError) as caught:
            self.service.authenticate_session(evidence)
        self.assertEqual(
            caught.exception.code,
            ErrorCode.SESSION_EXPIRED_OR_REVOKED.value,
        )

    def test_user_cannot_revoke_another_users_session(self) -> None:
        actor_issued = self.service.establish_session(identity("actor"))
        other_issued = self.service.establish_session(identity("other"))
        actor = self.service.authenticate_session(
            self.evidence(actor_issued.secret.reveal())
        )

        self.service.revoke_session(actor, other_issued.session_id)

        other = self.service.authenticate_session(
            self.evidence(other_issued.secret.reveal())
        )
        self.assertEqual(other.session_id, other_issued.session_id)

    def test_logout_all_revokes_only_the_authenticated_users_sessions(self) -> None:
        first = self.service.establish_session(identity("same-user"))
        second = self.service.establish_session(identity("same-user"))
        other = self.service.establish_session(identity("other-user"))
        authenticated = self.service.authenticate_session(
            self.evidence(first.secret.reveal())
        )

        self.service.revoke_all_user_sessions(authenticated)

        for issued in (first, second):
            with self.assertRaises(WorkspaceAccessError):
                self.service.authenticate_session(
                    self.evidence(issued.secret.reveal())
                )
        self.assertEqual(
            self.service.authenticate_session(
                self.evidence(other.secret.reveal())
            ).session_id,
            other.session_id,
        )

    def test_suspended_user_and_all_existing_sessions_fail_closed(self) -> None:
        issued = self.service.establish_session(identity("subject-1"))
        authenticated = self.service.authenticate_session(
            self.evidence(issued.secret.reveal())
        )

        self.service.suspend_user_for_security(authenticated.user_id)

        with self.assertRaises(WorkspaceAccessError) as existing_error:
            self.service.authenticate_session(
                self.evidence(issued.secret.reveal())
            )
        with self.assertRaises(WorkspaceAccessError) as new_error:
            self.service.establish_session(identity("subject-1"))
        self.assertEqual(
            existing_error.exception.code,
            ErrorCode.SESSION_EXPIRED_OR_REVOKED.value,
        )
        self.assertEqual(new_error.exception.code, ErrorCode.UNAUTHENTICATED.value)


class ProductionDefaultPortTests(unittest.TestCase):
    def test_system_clock_returns_aware_utc_time(self) -> None:
        now = SystemClock().now()

        self.assertIs(now.tzinfo, UTC)
        self.assertIsNotNone(now.utcoffset())

    def test_system_token_source_returns_unique_ids_and_long_secrets(self) -> None:
        source = SystemTokenSource()

        ids = {source.new_id("session") for _ in range(2)}
        secrets = {source.new_session_secret() for _ in range(2)}

        self.assertEqual(len(ids), 2)
        self.assertTrue(all(value.startswith("session_") for value in ids))
        self.assertEqual(len(secrets), 2)
        self.assertTrue(all(len(value) >= 43 for value in secrets))


if __name__ == "__main__":
    unittest.main()
