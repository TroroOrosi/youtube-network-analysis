from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from workspace_access.models import (
    AccessSecret,
    AuditAction,
    CreateWorkspace,
    GrantMembership,
    Permission,
    Role,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceAccessError,
    WorkspaceSelection,
)
from workspace_access.service import WorkspaceAccessService


START = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = START) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class PredictableTokens:
    def __init__(self) -> None:
        self.counter = 0

    def new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def new_session_secret(self) -> str:
        self.counter += 1
        return f"session-secret-{self.counter}-" + ("x" * 43)


class FakeStore:
    """One document, exactly what a restart would find on disk."""

    def __init__(self) -> None:
        self.document: str | None = None
        self.saves = 0

    def load(self) -> str | None:
        return self.document

    def save(self, document: str) -> None:
        self.document = document
        self.saves += 1


def identity(subject: str) -> VerifiedIdentity:
    return VerifiedIdentity(
        issuer="https://identity.example",
        subject=subject,
        authenticated_at=START,
        display_name=subject,
    )


class RestartFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.tokens = PredictableTokens()
        self.store = FakeStore()
        self.service = self.restart()

    def restart(self) -> WorkspaceAccessService:
        self.service = WorkspaceAccessService(
            clock=self.clock, token_source=self.tokens, state_store=self.store
        )
        return self.service

    def sign_in(self, subject: str = "subject-1") -> tuple[str, object]:
        issued = self.service.establish_session(identity(subject))
        return issued.secret.reveal(), issued

    def session(self, secret: str):
        return self.service.authenticate_session(SessionEvidence(AccessSecret(secret)))


class SessionRestartTests(RestartFixture):
    def test_a_session_outlives_the_process_that_issued_it(self) -> None:
        secret, issued = self.sign_in()

        self.restart()

        self.assertEqual(self.session(secret).session_id, issued.session_id)

    def test_a_logged_out_session_stays_logged_out(self) -> None:
        secret, _ = self.sign_in()
        self.service.logout(SessionEvidence(AccessSecret(secret)))

        self.restart()

        with self.assertRaises(WorkspaceAccessError):
            self.session(secret)

    def test_the_session_secret_is_never_written_down(self) -> None:
        secret, _ = self.sign_in()

        self.assertIsNotNone(self.store.document)
        self.assertNotIn(secret, self.store.document or "")


class WorkspaceRestartTests(RestartFixture):
    def setUp(self) -> None:
        super().setUp()
        self.secret, _ = self.sign_in()
        self.context = self.service.create_workspace(
            self.session(self.secret), CreateWorkspace(name="本番運用")
        )

    def test_a_workspace_and_its_owner_survive(self) -> None:
        self.restart()

        summaries = self.service.list_accessible_workspaces(self.session(self.secret))

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].workspace_id, self.context.workspace_id)
        self.assertEqual(summaries[0].role, Role.OWNER)

    def test_the_last_used_workspace_is_still_the_default(self) -> None:
        second = self.service.create_workspace(
            self.session(self.secret), CreateWorkspace(name="検証用")
        )
        self.service.resolve_workspace_context(
            self.session(self.secret),
            WorkspaceSelection(workspace_id=second.workspace_id),
            Permission.WORKSPACE_READ,
        )

        self.restart()

        resolved = self.service.resolve_workspace_context(
            self.session(self.secret), None, Permission.WORKSPACE_READ
        )
        self.assertEqual(resolved.workspace_id, second.workspace_id)

    def test_a_replayed_command_is_still_answered_once(self) -> None:
        other = self.service.establish_session(identity("subject-2"))
        other_user = self.service.authenticate_session(
            SessionEvidence(AccessSecret(other.secret.reveal()))
        ).user_id
        command = GrantMembership(
            user_id=other_user, role=Role.MEMBER, idempotency_key="grant-1"
        )
        granted = self.service.grant_membership(self.context, command)

        self.restart()

        context = self.service.resolve_workspace_context(
            self.session(self.secret),
            WorkspaceSelection(workspace_id=self.context.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        replayed = self.service.grant_membership(context, command)
        self.assertEqual(replayed.membership_id, granted.membership_id)

    def test_the_audit_trail_survives(self) -> None:
        self.restart()

        context = self.service.resolve_workspace_context(
            self.session(self.secret),
            WorkspaceSelection(workspace_id=self.context.workspace_id),
            Permission.AUDIT_READ,
        )
        actions = [event.action for event in self.service.list_audit_events(context)]
        self.assertIn(AuditAction.WORKSPACE_CREATED, actions)


class DocumentTests(RestartFixture):
    def test_an_empty_store_is_not_written_until_something_happens(self) -> None:
        self.assertIsNone(self.store.document)
        self.assertEqual(self.store.saves, 0)

    def test_a_document_from_a_newer_version_is_refused(self) -> None:
        self.store.document = json.dumps({"version": 99, "users": []})

        with self.assertRaises(ValueError):
            self.restart()

    def test_an_unreadable_document_is_refused(self) -> None:
        self.store.document = "{not json"

        with self.assertRaises(ValueError):
            self.restart()

    def test_a_service_without_a_store_still_works(self) -> None:
        service = WorkspaceAccessService(clock=self.clock, token_source=self.tokens)

        issued = service.establish_session(identity("subject-9"))

        self.assertTrue(issued.session_id)


if __name__ == "__main__":
    unittest.main()
