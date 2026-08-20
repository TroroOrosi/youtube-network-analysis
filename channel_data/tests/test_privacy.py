from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from channel_data.errors import ChannelDataError
from channel_data.models import (
    CollectionKind,
    CollectionStatus,
    CoverageLimitation,
    DeleteChannelData,
    DeleteWorkspaceData,
    FinishCollection,
    PageRequest,
    PublishSubscriberSnapshot,
    StartCollection,
    SubscriberObservationInput,
    SubscriberSnapshotQuery,
    SubscriberTraversalStatus,
)
from channel_data.service import ChannelDataService
from workspace_access.models import Permission, Role, WorkspaceContext


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
LIMITATIONS = (
    CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
    CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
)


def context(workspace_id: str, *permissions: Permission) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        user_id="user-1",
        membership_id=f"membership-{workspace_id}",
        role=Role.OWNER,
        permissions=frozenset(permissions),
        session_id=f"session-{workspace_id}",
        authorization_revision=1,
        resolved_at=NOW,
    )


class PrivacyLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ChannelDataService()

    def accept_snapshot(
        self,
        workspace_id: str,
        suffix: str,
        captured_at: datetime,
    ) -> None:
        writer = context(workspace_id, Permission.COLLECTION_RUN)
        collection_id = f"collection-{suffix}"
        self.service.start_collection(
            writer,
            StartCollection(
                "channel-1",
                collection_id,
                CollectionKind.SUBSCRIBERS,
                captured_at - timedelta(minutes=1),
                f"start-{suffix}",
            ),
        )
        self.service.publish_subscriber_snapshot(
            writer,
            PublishSubscriberSnapshot(
                "channel-1",
                collection_id,
                f"snapshot-{suffix}",
                captured_at,
                SubscriberTraversalStatus.COMPLETE,
                LIMITATIONS,
                (SubscriberObservationInput(f"subscriber-{suffix}", suffix, captured_at),),
                f"stage-{suffix}",
            ),
        )
        self.service.finish_collection(
            writer,
            FinishCollection(
                "channel-1",
                collection_id,
                CollectionStatus.COMPLETE,
                captured_at + timedelta(minutes=1),
                1,
                1,
                None,
                f"finish-{suffix}",
            ),
        )

    def test_retention_uses_exact_cutoffs_and_keeps_in_progress_attempts(self) -> None:
        cutoff_snapshot = NOW - timedelta(days=365)
        recent_snapshot = cutoff_snapshot + timedelta(microseconds=1)
        self.accept_snapshot("workspace-1", "cutoff", cutoff_snapshot)
        self.accept_snapshot("workspace-1", "recent", recent_snapshot)

        writer = context("workspace-1", Permission.COLLECTION_RUN)
        terminal_start = NOW - timedelta(days=90, minutes=1)
        self.service.start_collection(
            writer,
            StartCollection(
                "channel-1",
                "terminal-cutoff",
                CollectionKind.COMMENTS,
                terminal_start,
                "start-terminal-cutoff",
            ),
        )
        self.service.finish_collection(
            writer,
            FinishCollection(
                "channel-1",
                "terminal-cutoff",
                CollectionStatus.PARTIAL,
                NOW - timedelta(days=90),
                0,
                1,
                None,
                "finish-terminal-cutoff",
            ),
        )
        self.service.start_collection(
            writer,
            StartCollection(
                "channel-1",
                "in-progress-old",
                CollectionKind.COMMENTS,
                NOW - timedelta(days=100),
                "start-in-progress-old",
            ),
        )
        self.service.start_collection(
            writer,
            StartCollection(
                "channel-1",
                "terminal-recent",
                CollectionKind.COMMENTS,
                NOW - timedelta(days=90, minutes=1),
                "start-terminal-recent",
            ),
        )
        self.service.finish_collection(
            writer,
            FinishCollection(
                "channel-1",
                "terminal-recent",
                CollectionStatus.PARTIAL,
                NOW - timedelta(days=90) + timedelta(microseconds=1),
                0,
                1,
                None,
                "finish-terminal-recent",
            ),
        )

        report = self.service.purge_retention(
            context("workspace-1", Permission.CHANNEL_MANAGE_CONNECTION),
            NOW,
        )
        self.assertGreaterEqual(report.snapshots_removed, 1)
        self.assertGreaterEqual(report.collection_attempts_removed, 1)
        self.assertGreaterEqual(report.idempotency_records_removed, 1)
        snapshot_ids = {
            key[2]
            for key in self.service._state.subscriber_snapshots
            if key[0] == "workspace-1"
        }
        self.assertNotIn("snapshot-cutoff", snapshot_ids)
        self.assertIn("snapshot-recent", snapshot_ids)
        self.assertFalse(
            any(key[2] == "snapshot-cutoff" for key in self.service._state.subscriber_observations)
        )
        self.assertIn(("workspace-1", "in-progress-old"), self.service._state.collections)
        self.assertNotIn(("workspace-1", "terminal-cutoff"), self.service._state.collections)
        self.assertIn(("workspace-1", "terminal-recent"), self.service._state.collections)
        self.assertIn(("workspace-1", "finish-terminal-recent"), self.service._state.idempotency)

    def test_channel_delete_cascades_all_copies_preserves_foreign_tenant_and_replays(self) -> None:
        self.accept_snapshot("workspace-1", "left", NOW)
        self.accept_snapshot("workspace-2", "right", NOW)
        reader = context("workspace-1", Permission.ANALYSIS_READ)
        first_page = self.service.list_subscriber_snapshots(
            reader,
            SubscriberSnapshotQuery("channel-1"),
            PageRequest(limit=1),
        )
        # A second snapshot creates a cursor without changing the deletion target.
        self.accept_snapshot("workspace-1", "left-2", NOW + timedelta(minutes=1))
        first_page = self.service.list_subscriber_snapshots(
            reader,
            SubscriberSnapshotQuery("channel-1"),
            PageRequest(limit=1),
        )
        self.assertIsNotNone(first_page.next_cursor)
        # Seed every remaining private channel store to prove the cascade list is complete.
        state = self.service._state
        candidate_key = ("workspace-1", "collection-left")
        state.video_candidates[candidate_key] = object()
        state.comment_candidates[candidate_key] = object()
        row_key = ("workspace-1", "channel-1", "inventory-private")
        state.video_inventories[row_key] = object()
        state.videos[row_key] = ()
        state.comment_activity[row_key] = ()
        state.comment_coverage[row_key] = object()
        state.accepted_video_inventory[("workspace-1", "channel-1")] = "inventory-private"

        command = DeleteChannelData("channel-1", "delete-channel-1")
        administrator = context(
            "workspace-1",
            Permission.CHANNEL_MANAGE_CONNECTION,
        )
        self.assertIsNone(self.service.delete_channel_data(administrator, command))
        self.assertIsNone(self.service.delete_channel_data(administrator, command))
        with self.assertRaises(ChannelDataError) as caught:
            self.service.delete_channel_data(
                administrator,
                DeleteChannelData("channel-1", "different-delete"),
            )
        self.assertEqual(caught.exception.code, "RESOURCE_NOT_FOUND_OR_FORBIDDEN")
        self.assertNotIn("channel-1", caught.exception.message)

        self.assertFalse(
            any(
                key[0] == "workspace-1"
                for mapping in vars_for_workspace_scan(self.service)
                for key in mapping
            )
        )
        self.assertEqual(
            [key for key in self.service._state.idempotency if key[0] == "workspace-1"],
            [("workspace-1", "delete-channel-1")],
        )
        foreign = self.service.list_subscriber_snapshots(
            context("workspace-2", Permission.ANALYSIS_READ),
            SubscriberSnapshotQuery("channel-1"),
        )
        self.assertEqual([item.snapshot_id for item in foreign.items], ["snapshot-right"])

    def test_workspace_delete_and_concurrent_publish_leave_no_addressable_rows(self) -> None:
        self.accept_snapshot("workspace-1", "existing", NOW)
        writer = context("workspace-1", Permission.COLLECTION_RUN)
        self.service.start_collection(
            writer,
            StartCollection(
                "channel-1",
                "concurrent",
                CollectionKind.SUBSCRIBERS,
                NOW,
                "start-concurrent",
            ),
        )
        publish = PublishSubscriberSnapshot(
            "channel-1",
            "concurrent",
            "snapshot-concurrent",
            NOW,
            SubscriberTraversalStatus.COMPLETE,
            LIMITATIONS,
            (SubscriberObservationInput("subscriber-concurrent", "Concurrent", NOW),),
            "stage-concurrent",
        )
        delete_context = context("workspace-1", Permission.WORKSPACE_DELETE)

        def attempt_publish() -> None:
            try:
                self.service.publish_subscriber_snapshot(writer, publish)
            except ChannelDataError:
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(attempt_publish),
                pool.submit(
                    self.service.delete_workspace_data,
                    delete_context,
                    DeleteWorkspaceData("delete-workspace-1"),
                ),
            )
            for future in futures:
                future.result()

        self.assertIsNone(
            self.service.delete_workspace_data(
                delete_context,
                DeleteWorkspaceData("delete-workspace-1"),
            )
        )
        self.assertFalse(
            any(
                key[0] == "workspace-1"
                for mapping in vars_for_workspace_scan(self.service)
                for key in mapping
            )
        )


def vars_for_workspace_scan(service: ChannelDataService):
    return (
        service._state.collections,
        service._state.subscriber_candidates,
        service._state.subscriber_snapshots,
        service._state.subscriber_observations,
        service._state.subscriber_registry,
        service._state.accepted_subscriber_snapshot,
        service._state.video_candidates,
        service._state.video_inventories,
        service._state.videos,
        service._state.accepted_video_inventory,
        service._state.comment_candidates,
        service._state.comment_activity,
        service._state.comment_coverage,
        service._state.cursors,
    )


if __name__ == "__main__":
    unittest.main()
