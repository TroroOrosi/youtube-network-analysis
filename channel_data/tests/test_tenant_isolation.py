from __future__ import annotations

import unittest
from datetime import UTC, datetime

from channel_data.errors import ChannelDataError
from channel_data.models import CollectionKind, CollectionStatus, StartCollection
from channel_data.service import ChannelDataService
from workspace_access.models import Permission, Role, WorkspaceContext


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def context(
    workspace_id: str,
    user_id: str,
    permissions: frozenset[Permission] = frozenset({Permission.COLLECTION_RUN}),
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        user_id=user_id,
        membership_id=f"membership-{workspace_id}-{user_id}",
        role=Role.OWNER,
        permissions=permissions,
        session_id=f"session-{workspace_id}-{user_id}",
        authorization_revision=1,
        resolved_at=NOW,
    )


class TenantScopedCollectionStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ChannelDataService()

    def test_start_requires_collection_run_and_returns_immutable_in_progress_state(self) -> None:
        with self.assertRaises(ChannelDataError) as caught:
            self.service.start_collection(
                context("workspace-1", "user-1", frozenset()),
                StartCollection(
                    "channel-1",
                    "collection-1",
                    CollectionKind.SUBSCRIBERS,
                    NOW,
                    "intent-1",
                ),
            )
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")
        self.assertFalse(caught.exception.retryable)

        state = self.service.start_collection(
            context("workspace-1", "user-1"),
            StartCollection(
                "channel-1",
                "collection-1",
                CollectionKind.SUBSCRIBERS,
                NOW,
                "intent-1",
            ),
        )
        self.assertEqual(state.workspace_id, "workspace-1")
        self.assertIs(state.status, CollectionStatus.IN_PROGRESS)
        self.assertIsNone(state.completed_at)

    def test_exact_replay_returns_original_but_changed_actor_or_payload_conflicts(self) -> None:
        command = StartCollection(
            "channel-1",
            "collection-1",
            CollectionKind.SUBSCRIBERS,
            NOW,
            "intent-1",
        )
        first = self.service.start_collection(context("workspace-1", "user-1"), command)
        replay = self.service.start_collection(context("workspace-1", "user-1"), command)
        self.assertIs(replay, first)

        for changed_context, changed_command in (
            (context("workspace-1", "user-2"), command),
            (
                context("workspace-1", "user-1"),
                StartCollection(
                    "channel-2",
                    "collection-1",
                    CollectionKind.SUBSCRIBERS,
                    NOW,
                    "intent-1",
                ),
            ),
        ):
            with self.subTest(user=changed_context.user_id, channel=changed_command.channel_id):
                with self.assertRaises(ChannelDataError) as caught:
                    self.service.start_collection(changed_context, changed_command)
                self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_identical_ids_and_idempotency_keys_are_independent_between_workspaces(self) -> None:
        command = StartCollection(
            "same-channel",
            "same-collection",
            CollectionKind.VIDEOS,
            NOW,
            "same-intent",
        )
        left = self.service.start_collection(context("workspace-left", "user-1"), command)
        right = self.service.start_collection(context("workspace-right", "user-1"), command)

        self.assertEqual(left.collection_id, right.collection_id)
        self.assertEqual(left.channel_id, right.channel_id)
        self.assertNotEqual(left.workspace_id, right.workspace_id)

    def test_collection_id_collision_in_one_tenant_fails_without_mutation(self) -> None:
        caller = context("workspace-1", "user-1")
        first = self.service.start_collection(
            caller,
            StartCollection(
                "channel-1",
                "collection-1",
                CollectionKind.SUBSCRIBERS,
                NOW,
                "intent-1",
            ),
        )
        with self.assertRaises(ChannelDataError) as caught:
            self.service.start_collection(
                caller,
                StartCollection(
                    "channel-2",
                    "collection-1",
                    CollectionKind.SUBSCRIBERS,
                    NOW,
                    "intent-2",
                ),
            )
        self.assertEqual(caught.exception.code, "RESOURCE_NOT_FOUND_OR_FORBIDDEN")
        self.assertIs(self.service.start_collection(caller, first_to_command(first)), first)


def first_to_command(state):
    return StartCollection(
        state.channel_id,
        state.collection_id,
        state.kind,
        state.started_at,
        "intent-1",
    )


if __name__ == "__main__":
    unittest.main()
