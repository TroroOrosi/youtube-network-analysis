from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from channel_data.models import (
    CollectionKind,
    CollectionStatus,
    CoverageLimitation,
    FinishCollection,
    PublishSubscriberSnapshot,
    StartCollection,
    SubscriberObservationInput,
    SubscriberRegistryQuery,
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


class SubscriberPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ChannelDataService()
        self.writer = context("workspace-1", Permission.COLLECTION_RUN)
        self.reader = context("workspace-1", Permission.ANALYSIS_READ)

    def publish(
        self,
        collection_id: str,
        snapshot_id: str,
        captured_at: datetime,
        observations: tuple[SubscriberObservationInput, ...],
        *,
        workspace_id: str = "workspace-1",
        terminal: CollectionStatus = CollectionStatus.COMPLETE,
    ):
        writer = context(workspace_id, Permission.COLLECTION_RUN)
        self.service.start_collection(
            writer,
            StartCollection(
                "channel-1",
                collection_id,
                CollectionKind.SUBSCRIBERS,
                captured_at - timedelta(minutes=1),
                f"start-{collection_id}",
            ),
        )
        command = PublishSubscriberSnapshot(
            "channel-1",
            collection_id,
            snapshot_id,
            captured_at,
            SubscriberTraversalStatus.COMPLETE,
            LIMITATIONS,
            observations,
            f"stage-{collection_id}",
        )
        staged = self.service.publish_subscriber_snapshot(writer, command)
        finished = self.service.finish_collection(
            writer,
            FinishCollection(
                "channel-1",
                collection_id,
                terminal,
                captured_at + timedelta(minutes=1),
                len(observations),
                len(observations),
                None,
                f"finish-{collection_id}",
            ),
        )
        return staged, finished, command

    def test_candidate_is_invisible_until_complete_then_folds_once(self) -> None:
        self.service.start_collection(
            self.writer,
            StartCollection(
                "channel-1",
                "collection-1",
                CollectionKind.SUBSCRIBERS,
                NOW - timedelta(minutes=1),
                "start-1",
            ),
        )
        command = PublishSubscriberSnapshot(
            "channel-1",
            "collection-1",
            "snapshot-1",
            NOW,
            SubscriberTraversalStatus.COMPLETE,
            LIMITATIONS,
            (SubscriberObservationInput("subscriber-1", "First", NOW),),
            "stage-1",
        )
        staged = self.service.publish_subscriber_snapshot(self.writer, command)
        self.assertIs(self.service.publish_subscriber_snapshot(self.writer, command), staged)
        self.assertEqual(
            self.service.list_subscriber_snapshots(
                self.reader, SubscriberSnapshotQuery("channel-1")
            ).items,
            (),
        )
        self.assertEqual(
            self.service.list_subscriber_registry(
                self.reader, SubscriberRegistryQuery("channel-1")
            ).items,
            (),
        )

        finish = FinishCollection(
            "channel-1",
            "collection-1",
            CollectionStatus.COMPLETE,
            NOW + timedelta(minutes=1),
            1,
            1,
            None,
            "finish-1",
        )
        completed = self.service.finish_collection(self.writer, finish)
        self.assertEqual(completed.accepted_generation_id, "snapshot-1")
        self.assertIs(self.service.finish_collection(self.writer, finish), completed)
        snapshots = self.service.list_subscriber_snapshots(
            self.reader, SubscriberSnapshotQuery("channel-1")
        ).items
        registry = self.service.list_subscriber_registry(
            self.reader, SubscriberRegistryQuery("channel-1")
        ).items
        self.assertEqual(snapshots, (staged,))
        self.assertEqual(registry[0].observation_count, 1)
        self.assertEqual(registry[0].last_snapshot_id, "snapshot-1")

    def test_absence_does_not_unsubscribe_and_out_of_order_fold_is_deterministic(self) -> None:
        older = NOW - timedelta(days=10)
        newer = NOW
        rows_old = (
            SubscriberObservationInput("subscriber-1", "Old", older),
            SubscriberObservationInput("subscriber-2", "Only old", older),
        )
        rows_new = (SubscriberObservationInput("subscriber-1", "New", newer),)

        first_service = self.service
        self.publish("old", "snapshot-a", older, rows_old)
        self.publish("new", "snapshot-b", newer, rows_new)
        first_registry = first_service.list_subscriber_registry(
            self.reader, SubscriberRegistryQuery("channel-1")
        ).items

        self.service = ChannelDataService()
        self.publish("new", "snapshot-b", newer, rows_new)
        self.publish("old", "snapshot-a", older, rows_old)
        second_registry = self.service.list_subscriber_registry(
            self.reader, SubscriberRegistryQuery("channel-1")
        ).items

        self.assertEqual(first_registry, second_registry)
        entries = {entry.subscriber_channel_id: entry for entry in first_registry}
        self.assertEqual(entries["subscriber-1"].observation_count, 2)
        self.assertEqual(entries["subscriber-1"].title, "New")
        self.assertEqual(entries["subscriber-1"].first_seen_at, older)
        self.assertEqual(entries["subscriber-1"].last_seen_at, newer)
        self.assertIn("subscriber-2", entries)

    def test_partial_candidate_never_publishes_and_foreign_identical_ids_are_independent(self) -> None:
        self.publish(
            "partial",
            "same-snapshot",
            NOW,
            (SubscriberObservationInput("subscriber-1", "Partial", NOW),),
            terminal=CollectionStatus.PARTIAL,
        )
        self.assertEqual(
            self.service.list_subscriber_snapshots(
                self.reader, SubscriberSnapshotQuery("channel-1")
            ).items,
            (),
        )

        self.publish(
            "accepted-left",
            "same-snapshot",
            NOW + timedelta(hours=1),
            (SubscriberObservationInput("left-subscriber", "Left", NOW),),
        )
        self.publish(
            "accepted-right",
            "same-snapshot",
            NOW + timedelta(hours=1),
            (SubscriberObservationInput("right-subscriber", "Right", NOW),),
            workspace_id="workspace-2",
        )
        left = self.service.list_subscriber_registry(
            context("workspace-1", Permission.ANALYSIS_READ),
            SubscriberRegistryQuery("channel-1"),
        ).items
        right = self.service.list_subscriber_registry(
            context("workspace-2", Permission.ANALYSIS_READ),
            SubscriberRegistryQuery("channel-1"),
        ).items
        self.assertEqual({item.subscriber_channel_id for item in left}, {"left-subscriber"})
        self.assertEqual({item.subscriber_channel_id for item in right}, {"right-subscriber"})


if __name__ == "__main__":
    unittest.main()
