from __future__ import annotations

import threading
import unittest
from datetime import UTC, datetime

from workspace_access.models import (
    AccessSecret,
    ChangeMembershipRole,
    CreateWorkspace,
    ErrorCode,
    GrantMembership,
    Permission,
    RevokeMembership,
    Role,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceAccessError,
    WorkspaceSelection,
)
from workspace_access.service import WorkspaceAccessService


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


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


class MembershipAdministrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = WorkspaceAccessService(
            clock=FixedClock(),
            token_source=PredictableTokens(),
        )
        self.owner = self.login("owner")
        self.workspace = self.service.create_workspace(
            self.owner,
            CreateWorkspace("Team"),
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

    def owner_context(self):
        return self.service.resolve_workspace_context(
            self.owner,
            WorkspaceSelection(self.workspace.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )

    def test_owner_grants_member_and_member_cannot_administer_memberships(self) -> None:
        member_session = self.login("member")
        membership = self.service.grant_membership(
            self.owner_context(),
            GrantMembership(member_session.user_id, Role.MEMBER, "grant-member"),
        )
        member_context = self.service.resolve_workspace_context(
            member_session,
            WorkspaceSelection(self.workspace.workspace_id),
            Permission.MEMBERSHIP_LIST,
        )

        self.assertEqual(membership.role, Role.MEMBER)
        self.assertEqual(member_context.role, Role.MEMBER)
        with self.assertRaises(WorkspaceAccessError) as caught:
            self.service.grant_membership(
                member_context,
                GrantMembership(self.login("third").user_id, Role.MEMBER, "blocked"),
            )
        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED.value)

    def test_mutation_invalidates_old_context_on_next_command(self) -> None:
        old_context = self.owner_context()
        member = self.login("member")
        self.service.grant_membership(
            old_context,
            GrantMembership(member.user_id, Role.MEMBER, "grant-member"),
        )

        with self.assertRaises(WorkspaceAccessError) as caught:
            self.service.grant_membership(
                old_context,
                GrantMembership(self.login("third").user_id, Role.MEMBER, "grant-third"),
            )

        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED.value)

    def test_idempotent_replay_returns_original_result_and_payload_change_conflicts(self) -> None:
        member = self.login("member")
        context = self.owner_context()
        command = GrantMembership(member.user_id, Role.MEMBER, "same-key")

        first = self.service.grant_membership(context, command)
        replay = self.service.grant_membership(context, command)

        self.assertEqual(first, replay)
        with self.assertRaises(WorkspaceAccessError) as caught:
            self.service.grant_membership(
                context,
                GrantMembership(member.user_id, Role.OWNER, "same-key"),
            )
        self.assertEqual(
            caught.exception.code,
            ErrorCode.IDEMPOTENCY_CONFLICT.value,
        )

    def test_last_owner_cannot_be_demoted_revoked_or_self_removed(self) -> None:
        context = self.owner_context()

        with self.assertRaises(WorkspaceAccessError) as demote_error:
            self.service.change_membership_role(
                context,
                ChangeMembershipRole(context.membership_id, Role.MEMBER, "demote"),
            )
        with self.assertRaises(WorkspaceAccessError) as revoke_error:
            self.service.revoke_membership(
                context,
                RevokeMembership(context.membership_id, "revoke"),
            )

        self.assertEqual(demote_error.exception.code, ErrorCode.LAST_OWNER_REQUIRED.value)
        self.assertEqual(revoke_error.exception.code, ErrorCode.LAST_OWNER_REQUIRED.value)
        self.assertEqual(self.service._debug_owner_count(context.workspace_id), 1)  # noqa: SLF001

    def test_role_change_and_revocation_apply_to_next_context_resolution(self) -> None:
        member_session = self.login("member")
        member = self.service.grant_membership(
            self.owner_context(),
            GrantMembership(member_session.user_id, Role.MEMBER, "grant"),
        )
        promoted = self.service.change_membership_role(
            self.owner_context(),
            ChangeMembershipRole(member.membership_id, Role.OWNER, "promote"),
        )
        promoted_context = self.service.resolve_workspace_context(
            member_session,
            WorkspaceSelection(self.workspace.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        self.assertEqual(promoted.role, Role.OWNER)
        self.assertEqual(promoted_context.role, Role.OWNER)

        self.service.revoke_membership(
            self.owner_context(),
            RevokeMembership(promoted.membership_id, "revoke-promoted"),
        )
        with self.assertRaises(WorkspaceAccessError) as caught:
            self.service.resolve_workspace_context(
                member_session,
                WorkspaceSelection(self.workspace.workspace_id),
                Permission.WORKSPACE_READ,
            )
        self.assertEqual(
            caught.exception.code,
            ErrorCode.WORKSPACE_NOT_FOUND_OR_FORBIDDEN.value,
        )

    def test_missing_and_foreign_membership_targets_use_same_error(self) -> None:
        other_owner = self.login("other-owner")
        other_workspace = self.service.create_workspace(
            other_owner,
            CreateWorkspace("Other"),
        )
        errors = []
        for membership_id in (
            other_workspace.membership_id,
            "missing-membership",
            [],  # type: ignore[list-item]
        ):
            with self.assertRaises(WorkspaceAccessError) as caught:
                self.service.revoke_membership(
                    self.owner_context(),
                    RevokeMembership(
                        membership_id,  # type: ignore[arg-type]
                        "revoke-target",
                    ),
                )
            errors.append(caught.exception)

        self.assertTrue(all(error.code == errors[0].code for error in errors))
        self.assertTrue(all(error.message == errors[0].message for error in errors))
        self.assertEqual(
            errors[0].code,
            ErrorCode.MEMBERSHIP_NOT_FOUND_OR_FORBIDDEN.value,
        )

    def test_command_role_and_idempotency_key_are_validated(self) -> None:
        member = self.login("member")

        with self.assertRaises(WorkspaceAccessError) as role_error:
            self.service.grant_membership(
                self.owner_context(),
                GrantMembership(member.user_id, "OWNER", "grant"),  # type: ignore[arg-type]
            )
        with self.assertRaises(WorkspaceAccessError) as key_error:
            self.service.grant_membership(
                self.owner_context(),
                GrantMembership(member.user_id, Role.MEMBER, "   "),
            )

        self.assertEqual(role_error.exception.code, ErrorCode.INVALID_INPUT.value)
        self.assertEqual(role_error.exception.field, "role")
        self.assertEqual(key_error.exception.code, ErrorCode.INVALID_INPUT.value)
        self.assertEqual(key_error.exception.field, "idempotency_key")

    def test_concurrent_owner_departures_leave_exactly_one_owner(self) -> None:
        second_owner_session = self.login("second-owner")
        second_owner = self.service.grant_membership(
            self.owner_context(),
            GrantMembership(second_owner_session.user_id, Role.OWNER, "grant-owner"),
        )
        first_context = self.owner_context()
        second_context = self.service.resolve_workspace_context(
            second_owner_session,
            WorkspaceSelection(self.workspace.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        barrier = threading.Barrier(2)
        results: list[str] = []
        results_lock = threading.Lock()

        def depart(context, membership_id: str, key: str) -> None:
            barrier.wait()
            try:
                self.service.revoke_membership(
                    context,
                    RevokeMembership(membership_id, key),
                )
                result = "removed"
            except WorkspaceAccessError as error:
                result = error.code
            with results_lock:
                results.append(result)

        threads = [
            threading.Thread(
                target=depart,
                args=(first_context, first_context.membership_id, "leave-first"),
            ),
            threading.Thread(
                target=depart,
                args=(second_context, second_owner.membership_id, "leave-second"),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results.count("removed"), 1)
        self.assertEqual(results.count(ErrorCode.LAST_OWNER_REQUIRED.value), 1)
        self.assertEqual(self.service._debug_owner_count(self.workspace.workspace_id), 1)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
