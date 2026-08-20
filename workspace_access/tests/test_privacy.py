from __future__ import annotations

import dataclasses
import threading
import unittest
from datetime import UTC, datetime, timedelta

from workspace_access.models import (
    AccessSecret,
    AuditCategory,
    CreateWorkspace,
    ErrorCode,
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


class MutableClock:
    def __init__(self) -> None:
        self.current = START

    def now(self) -> datetime:
        return self.current


class PredictableTokens:
    def __init__(self) -> None:
        self.counter = 0
        self.lock = threading.Lock()

    def new_id(self, prefix: str) -> str:
        with self.lock:
            self.counter += 1
            return f"{prefix}-{self.counter}"

    def new_session_secret(self) -> str:
        with self.lock:
            self.counter += 1
            return f"secret-{self.counter}-" + ("x" * 43)


class PrivacyLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.service = WorkspaceAccessService(
            clock=self.clock,
            token_source=PredictableTokens(),
        )

    def login(
        self,
        subject: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ):
        issued = self.service.establish_session(
            VerifiedIdentity(
                issuer="https://identity.example",
                subject=subject,
                authenticated_at=START,
                verified_email=email,
                display_name=display_name,
            )
        )
        authenticated = self.service.authenticate_session(
            SessionEvidence(AccessSecret(issued.secret.reveal()))
        )
        return issued, authenticated

    def test_export_is_allowlisted_and_excludes_secrets_and_other_user_pii(self) -> None:
        owner_issued, owner = self.login(
            "owner-subject",
            email="owner@example.test",
            display_name="Owner",
        )
        _, member = self.login(
            "member-subject",
            email="member-private@example.test",
            display_name="Member private name",
        )
        workspace = self.service.create_workspace(owner, CreateWorkspace("Team"))
        owner_context = self.service.resolve_workspace_context(
            owner,
            WorkspaceSelection(workspace.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        self.service.grant_membership(
            owner_context,
            GrantMembership(member.user_id, Role.MEMBER, "grant-member"),
        )

        exported = self.service.export_my_access_data(owner)
        rendered = repr(exported)

        self.assertEqual(
            {field.name for field in dataclasses.fields(exported)},
            {
                "user_id",
                "issuer",
                "subject",
                "verified_email",
                "display_name",
                "memberships",
                "audit_events",
            },
        )
        self.assertEqual(exported.verified_email, "owner@example.test")
        self.assertEqual(exported.memberships[0].workspace_name, "Team")
        self.assertNotIn(owner_issued.secret.reveal(), rendered)
        self.assertNotIn("member-private@example.test", rendered)
        self.assertNotIn("Member private name", rendered)
        self.assertNotIn("digest", rendered.lower())

    def test_sole_owner_deletion_fails_without_partial_mutation(self) -> None:
        issued, owner = self.login("owner")
        workspace = self.service.create_workspace(owner, CreateWorkspace("Team"))

        with self.assertRaises(WorkspaceAccessError) as caught:
            self.service.request_account_deletion(owner)

        self.assertEqual(caught.exception.code, ErrorCode.LAST_OWNER_REQUIRED.value)
        still_authenticated = self.service.authenticate_session(
            SessionEvidence(AccessSecret(issued.secret.reveal()))
        )
        context = self.service.resolve_workspace_context(
            still_authenticated,
            WorkspaceSelection(workspace.workspace_id),
            Permission.WORKSPACE_READ,
        )
        self.assertEqual(context.user_id, owner.user_id)

    def test_deletion_revokes_sessions_removes_membership_and_tombstones_pii(self) -> None:
        deleted_issued, deleted_user = self.login(
            "departing-subject",
            email="departing@example.test",
            display_name="Departing user",
        )
        _, surviving_owner = self.login("surviving-owner")
        workspace = self.service.create_workspace(
            deleted_user,
            CreateWorkspace("Team"),
        )
        self.service.grant_membership(
            self.service.resolve_workspace_context(
                deleted_user,
                WorkspaceSelection(workspace.workspace_id),
                Permission.MEMBERSHIP_MANAGE,
            ),
            GrantMembership(surviving_owner.user_id, Role.OWNER, "grant-owner"),
        )

        self.service.request_account_deletion(deleted_user)

        with self.assertRaises(WorkspaceAccessError):
            self.service.authenticate_session(
                SessionEvidence(AccessSecret(deleted_issued.secret.reveal()))
            )
        with self.assertRaises(WorkspaceAccessError) as deleted_context:
            self.service.resolve_workspace_context(
                deleted_user,
                WorkspaceSelection(workspace.workspace_id),
                Permission.WORKSPACE_READ,
            )
        self.assertEqual(deleted_context.exception.code, ErrorCode.UNAUTHENTICATED.value)
        self.assertEqual(
            self.service._debug_user_direct_identifiers(deleted_user.user_id),  # noqa: SLF001
            (None, None, None, None),
        )
        self.assertNotIn(
            deleted_user.user_id,
            self.service._debug_idempotency_actor_ids(),  # noqa: SLF001
        )
        survivor_context = self.service.resolve_workspace_context(
            surviving_owner,
            WorkspaceSelection(workspace.workspace_id),
            Permission.WORKSPACE_READ,
        )
        self.assertEqual(survivor_context.role, Role.OWNER)

    def test_retention_purges_records_at_exact_30_90_and_365_day_boundaries(self) -> None:
        issued, owner = self.login("owner")
        self.service.create_workspace(owner, CreateWorkspace("Team"))

        session_cutoff = issued.absolute_expires_at + timedelta(days=30)
        first = self.service.purge_expired_data(session_cutoff)
        self.assertEqual(first.session_records_purged, 1)

        security_cutoff = START + timedelta(days=90)
        second = self.service.purge_expired_data(security_cutoff)
        self.assertGreaterEqual(second.security_audit_events_purged, 1)
        counts = self.service._debug_audit_counts()  # noqa: SLF001
        self.assertEqual(counts[AuditCategory.SECURITY], 0)
        self.assertGreater(counts[AuditCategory.ADMINISTRATION], 0)

        administration_cutoff = START + timedelta(days=365)
        third = self.service.purge_expired_data(administration_cutoff)
        self.assertGreaterEqual(third.administration_audit_events_purged, 1)
        self.assertEqual(
            self.service._debug_audit_counts()[AuditCategory.ADMINISTRATION],  # noqa: SLF001
            0,
        )

    def test_audit_events_are_allowlisted_and_never_contain_secret_or_pii_payloads(self) -> None:
        issued, owner = self.login(
            "owner",
            email="owner-private@example.test",
            display_name="Owner private name",
        )
        _, member = self.login(
            "member",
            email="member-private@example.test",
        )
        workspace = self.service.create_workspace(owner, CreateWorkspace("Team"))
        context = self.service.resolve_workspace_context(
            owner,
            WorkspaceSelection(workspace.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        self.service.grant_membership(
            context,
            GrantMembership(member.user_id, Role.MEMBER, "grant-member"),
        )
        audit_context = self.service.resolve_workspace_context(
            owner,
            WorkspaceSelection(workspace.workspace_id),
            Permission.AUDIT_READ,
        )

        events = self.service.list_audit_events(audit_context)
        rendered = repr(events)

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(
            {field.name for field in dataclasses.fields(events[0])},
            {
                "event_id",
                "category",
                "action",
                "outcome",
                "actor_user_id",
                "workspace_id",
                "target_id",
                "occurred_at",
                "correlation_id",
            },
        )
        for forbidden in (
            issued.secret.reveal(),
            "owner-private@example.test",
            "member-private@example.test",
            "Owner private name",
            "grant-member",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
