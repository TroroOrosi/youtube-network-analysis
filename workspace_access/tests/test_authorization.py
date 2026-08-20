from __future__ import annotations

import unittest
from datetime import UTC, datetime

from workspace_access.models import (
    AccessSecret,
    CreateWorkspace,
    DeleteWorkspace,
    ErrorCode,
    GrantMembership,
    Permission,
    Role,
    SessionEvidence,
    VerifiedIdentity,
    UpdateWorkspace,
    WorkspaceAccessError,
    WorkspaceSelection,
    permissions_for_role,
)
from workspace_access.service import WorkspaceAccessService


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class PredictableTokens:
    def __init__(self) -> None:
        self.counter = 0

    def new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def new_session_secret(self) -> str:
        self.counter += 1
        return f"secret-{self.counter}-" + ("x" * 43)


class WorkspaceAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = WorkspaceAccessService(
            clock=FixedClock(),
            token_source=PredictableTokens(),
        )

    def login(self, subject: str):
        issued = self.service.establish_session(
            VerifiedIdentity(
                issuer="https://identity.example",
                subject=subject,
                authenticated_at=NOW,
            )
        )
        return self.service.authenticate_session(
            SessionEvidence(AccessSecret(issued.secret.reveal()))
        )

    def test_creation_atomically_returns_first_owner_context(self) -> None:
        session = self.login("owner")

        context = self.service.create_workspace(
            session,
            CreateWorkspace("  Analysis team  "),
        )
        summaries = self.service.list_accessible_workspaces(session)

        self.assertEqual(context.user_id, session.user_id)
        self.assertEqual(context.role, Role.OWNER)
        self.assertEqual(context.permissions, permissions_for_role(Role.OWNER))
        self.assertEqual(context.authorization_revision, 1)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].workspace_id, context.workspace_id)
        self.assertEqual(summaries[0].name, "Analysis team")
        self.assertEqual(summaries[0].role, Role.OWNER)

    def test_one_workspace_falls_back_but_multiple_require_explicit_selection(self) -> None:
        session = self.login("owner")
        first = self.service.create_workspace(session, CreateWorkspace("First"))

        fallback = self.service.resolve_workspace_context(
            session,
            None,
            Permission.ANALYSIS_READ,
        )
        self.assertEqual(fallback.workspace_id, first.workspace_id)

        second = self.service.create_workspace(session, CreateWorkspace("Second"))
        with self.assertRaises(WorkspaceAccessError) as ambiguous:
            self.service.resolve_workspace_context(
                session,
                None,
                Permission.ANALYSIS_READ,
            )
        self.assertEqual(
            ambiguous.exception.code,
            ErrorCode.WORKSPACE_SELECTION_REQUIRED.value,
        )

        explicit = self.service.resolve_workspace_context(
            session,
            WorkspaceSelection(second.workspace_id),
            Permission.ANALYSIS_READ,
        )
        remembered = self.service.resolve_workspace_context(
            session,
            None,
            Permission.ANALYSIS_READ,
        )
        self.assertEqual(explicit.workspace_id, second.workspace_id)
        self.assertEqual(remembered.workspace_id, second.workspace_id)

    def test_stale_preference_is_revalidated_and_never_authority(self) -> None:
        session = self.login("owner")
        only = self.service.create_workspace(session, CreateWorkspace("Only"))
        self.service._debug_set_workspace_preference(  # noqa: SLF001
            session.user_id,
            "workspace-no-longer-accessible",
        )

        resolved = self.service.resolve_workspace_context(
            session,
            None,
            Permission.WORKSPACE_READ,
        )

        self.assertEqual(resolved.workspace_id, only.workspace_id)

    def test_foreign_and_missing_workspace_have_identical_safe_errors(self) -> None:
        actor = self.login("actor")
        other = self.login("other")
        foreign = self.service.create_workspace(other, CreateWorkspace("Private name"))

        errors = []
        for workspace_id in (
            foreign.workspace_id,
            "workspace-does-not-exist",
            [],  # type: ignore[list-item]
        ):
            with self.assertRaises(WorkspaceAccessError) as caught:
                self.service.resolve_workspace_context(
                    actor,
                    WorkspaceSelection(workspace_id),
                    Permission.WORKSPACE_READ,
                )
            errors.append(caught.exception)

        self.assertTrue(all(error.code == errors[0].code for error in errors))
        self.assertTrue(all(error.message == errors[0].message for error in errors))
        self.assertEqual(
            errors[0].code,
            ErrorCode.WORKSPACE_NOT_FOUND_OR_FORBIDDEN.value,
        )
        self.assertNotIn(foreign.workspace_id, errors[0].message)
        self.assertNotIn("Private name", errors[0].message)

    def test_accessible_workspace_list_is_tenant_scoped_and_stably_sorted(self) -> None:
        owner = self.login("owner")
        outsider = self.login("outsider")
        self.service.create_workspace(owner, CreateWorkspace("zeta"))
        self.service.create_workspace(owner, CreateWorkspace("Alpha"))
        self.service.create_workspace(outsider, CreateWorkspace("Hidden"))

        summaries = self.service.list_accessible_workspaces(owner)

        self.assertEqual([summary.name for summary in summaries], ["Alpha", "zeta"])
        self.assertNotIn("Hidden", {summary.name for summary in summaries})

    def test_user_without_membership_fails_closed(self) -> None:
        session = self.login("no-workspaces")

        with self.assertRaises(WorkspaceAccessError) as list_error:
            self.service.list_accessible_workspaces(session)
        with self.assertRaises(WorkspaceAccessError) as context_error:
            self.service.resolve_workspace_context(
                session,
                None,
                Permission.WORKSPACE_READ,
            )

        self.assertEqual(
            list_error.exception.code,
            ErrorCode.NO_ACCESSIBLE_WORKSPACE.value,
        )
        self.assertEqual(
            context_error.exception.code,
            ErrorCode.NO_ACCESSIBLE_WORKSPACE.value,
        )

    def test_workspace_name_is_validated_at_service_boundary(self) -> None:
        session = self.login("owner")

        for name in ("", "   ", "x" * 101, None):
            with self.subTest(name=repr(name)):
                with self.assertRaises(WorkspaceAccessError) as caught:
                    self.service.create_workspace(
                        session,
                        CreateWorkspace(name),  # type: ignore[arg-type]
                    )
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT.value)
                self.assertEqual(caught.exception.field, "name")

    def test_owner_updates_workspace_name_with_idempotent_replay(self) -> None:
        owner = self.login("owner")
        created = self.service.create_workspace(owner, CreateWorkspace("Before"))
        context = self.service.resolve_workspace_context(
            owner,
            WorkspaceSelection(created.workspace_id),
            Permission.WORKSPACE_UPDATE,
        )
        command = UpdateWorkspace("  After  ", "update-name")

        updated = self.service.update_workspace(context, command)
        replay = self.service.update_workspace(context, command)

        self.assertEqual(updated.name, "After")
        self.assertEqual(updated, replay)
        self.assertEqual(
            self.service.list_accessible_workspaces(owner)[0].name,
            "After",
        )

    def test_member_cannot_update_or_delete_workspace(self) -> None:
        owner = self.login("owner")
        member = self.login("member")
        created = self.service.create_workspace(owner, CreateWorkspace("Team"))
        owner_context = self.service.resolve_workspace_context(
            owner,
            WorkspaceSelection(created.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        self.service.grant_membership(
            owner_context,
            GrantMembership(member.user_id, Role.MEMBER, "grant-member"),
        )
        member_context = self.service.resolve_workspace_context(
            member,
            WorkspaceSelection(created.workspace_id),
            Permission.WORKSPACE_READ,
        )

        with self.assertRaises(WorkspaceAccessError) as update_error:
            self.service.update_workspace(
                member_context,
                UpdateWorkspace("Changed", "member-update"),
            )
        with self.assertRaises(WorkspaceAccessError) as delete_error:
            self.service.delete_workspace(
                member_context,
                DeleteWorkspace("member-delete"),
            )

        self.assertEqual(update_error.exception.code, ErrorCode.PERMISSION_DENIED.value)
        self.assertEqual(delete_error.exception.code, ErrorCode.PERMISSION_DENIED.value)

    def test_explicit_deletion_invalidates_all_members_and_replays_safely(self) -> None:
        owner = self.login("owner")
        member = self.login("member")
        created = self.service.create_workspace(owner, CreateWorkspace("Team"))
        self.service.grant_membership(
            self.service.resolve_workspace_context(
                owner,
                WorkspaceSelection(created.workspace_id),
                Permission.MEMBERSHIP_MANAGE,
            ),
            GrantMembership(member.user_id, Role.MEMBER, "grant-member"),
        )
        delete_context = self.service.resolve_workspace_context(
            owner,
            WorkspaceSelection(created.workspace_id),
            Permission.WORKSPACE_DELETE,
        )
        command = DeleteWorkspace("delete-team")

        self.service.delete_workspace(delete_context, command)
        self.service.delete_workspace(delete_context, command)

        for session in (owner, member):
            with self.assertRaises(WorkspaceAccessError) as caught:
                self.service.resolve_workspace_context(
                    session,
                    WorkspaceSelection(created.workspace_id),
                    Permission.WORKSPACE_READ,
                )
            self.assertEqual(
                caught.exception.code,
                ErrorCode.WORKSPACE_NOT_FOUND_OR_FORBIDDEN.value,
            )


if __name__ == "__main__":
    unittest.main()
