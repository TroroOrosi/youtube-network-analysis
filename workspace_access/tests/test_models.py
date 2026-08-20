from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

from workspace_access.models import (
    AccessSecret,
    AuthenticatedSession,
    ErrorCode,
    IssuedSession,
    Permission,
    Role,
    SessionEvidence,
    VerifiedIdentity,
    ChangeMembershipRole,
    CreateWorkspace,
    GrantMembership,
    Membership,
    RevokeMembership,
    Workspace,
    WorkspaceContext,
    WorkspaceSelection,
    WorkspaceAccessError,
    permissions_for_role,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class AccessContractTests(unittest.TestCase):
    def test_roles_resolve_to_exact_approved_permission_matrix(self) -> None:
        common = {
            Permission.WORKSPACE_READ,
            Permission.MEMBERSHIP_LIST,
            Permission.CHANNEL_READ,
            Permission.COLLECTION_READ,
            Permission.COLLECTION_RUN,
            Permission.ANALYSIS_READ,
            Permission.ANALYSIS_EXPORT,
        }
        owner_only = {
            Permission.WORKSPACE_UPDATE,
            Permission.WORKSPACE_DELETE,
            Permission.MEMBERSHIP_MANAGE,
            Permission.CHANNEL_MANAGE_CONNECTION,
            Permission.AUDIT_READ,
        }

        self.assertEqual(permissions_for_role(Role.MEMBER), frozenset(common))
        self.assertEqual(
            permissions_for_role(Role.OWNER),
            frozenset(common | owner_only),
        )

    def test_public_records_are_immutable_and_slotted(self) -> None:
        identity = VerifiedIdentity(
            issuer="https://identity.example",
            subject="subject-1",
            authenticated_at=NOW,
            verified_email="analyst@example.test",
            display_name="Analyst",
        )
        session = AuthenticatedSession(
            session_id="session-1",
            user_id="user-1",
            authenticated_at=NOW,
            idle_expires_at=NOW + timedelta(minutes=30),
            absolute_expires_at=NOW + timedelta(hours=12),
        )

        with self.assertRaises(FrozenInstanceError):
            identity.subject = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            session.user_id = "changed"  # type: ignore[misc]
        self.assertFalse(hasattr(identity, "__dict__"))
        self.assertFalse(hasattr(session, "__dict__"))

    def test_datetime_records_reject_naive_values_with_stable_error(self) -> None:
        with self.assertRaises(WorkspaceAccessError) as caught:
            VerifiedIdentity(
                issuer="https://identity.example",
                subject="subject-1",
                authenticated_at=datetime(2026, 8, 20, 12, 0),
            )

        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(caught.exception.field, "authenticated_at")

    def test_session_secrets_are_redacted_from_nested_repr_and_str(self) -> None:
        raw_secret = "raw-session-value-that-must-not-appear"
        secret = AccessSecret(raw_secret)
        evidence = SessionEvidence(secret=secret)
        issued = IssuedSession(
            session_id="session-1",
            secret=secret,
            idle_expires_at=NOW + timedelta(minutes=30),
            absolute_expires_at=NOW + timedelta(hours=12),
        )

        self.assertEqual(secret.reveal(), raw_secret)
        for rendered in (str(secret), repr(secret), repr(evidence), repr(issued)):
            self.assertNotIn(raw_secret, rendered)
            self.assertIn("REDACTED", rendered)

    def test_errors_have_stable_safe_fields(self) -> None:
        error = WorkspaceAccessError(
            ErrorCode.PERMISSION_DENIED,
            message="Permission is required",
            field="required_permission",
            retryable=False,
            correlation_id="correlation-1",
        )

        self.assertEqual(error.code, "PERMISSION_DENIED")
        self.assertEqual(error.message, "Permission is required")
        self.assertEqual(error.field, "required_permission")
        self.assertFalse(error.retryable)
        self.assertEqual(error.correlation_id, "correlation-1")
        self.assertEqual(str(error), "Permission is required")

        generated = WorkspaceAccessError(
            ErrorCode.INVALID_INPUT,
            message="Invalid input",
        )
        self.assertIsNotNone(generated.correlation_id)
        self.assertTrue(generated.correlation_id.startswith("error_"))

    def test_workspace_membership_context_and_commands_are_typed_records(self) -> None:
        workspace = Workspace(
            workspace_id="workspace-1",
            name="Analysis team",
            created_at=NOW,
            authorization_revision=1,
        )
        membership = Membership(
            membership_id="membership-1",
            workspace_id=workspace.workspace_id,
            user_id="user-1",
            role=Role.OWNER,
            created_at=NOW,
        )
        context = WorkspaceContext(
            workspace_id=workspace.workspace_id,
            user_id=membership.user_id,
            membership_id=membership.membership_id,
            role=membership.role,
            permissions=permissions_for_role(membership.role),
            session_id="session-1",
            authorization_revision=workspace.authorization_revision,
            resolved_at=NOW,
        )

        self.assertEqual(WorkspaceSelection(workspace.workspace_id).workspace_id, "workspace-1")
        self.assertEqual(CreateWorkspace("Team").name, "Team")
        self.assertEqual(
            GrantMembership("user-2", Role.MEMBER, "grant-1").idempotency_key,
            "grant-1",
        )
        self.assertEqual(
            ChangeMembershipRole("membership-1", Role.MEMBER, "role-1").role,
            Role.MEMBER,
        )
        self.assertEqual(
            RevokeMembership("membership-1", "revoke-1").membership_id,
            "membership-1",
        )
        self.assertEqual(context.permissions, permissions_for_role(Role.OWNER))
        with self.assertRaises(FrozenInstanceError):
            workspace.name = "Changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
