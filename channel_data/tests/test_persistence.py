from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from channel_data.models import (
    CollectionHistoryQuery,
    CollectionKind,
    CollectionStatus,
    CoverageLimitation,
    FinishCollection,
    PublishSubscriberSnapshot,
    PublishVideoInventory,
    StartCollection,
    SubscriberObservationInput,
    SubscriberRegistryQuery,
    SubscriberSnapshotQuery,
    SubscriberTraversalStatus,
    VideoCoverageScope,
    VideoInput,
)
from channel_data.service import ChannelDataService
from workspace_access.models import Permission, Role, WorkspaceContext


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
LIMITATIONS = (
    CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
    CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
)


def context(*permissions: Permission) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="workspace-1",
        user_id="user-1",
        membership_id="membership-1",
        role=Role.OWNER,
        permissions=frozenset(permissions),
        session_id="session-1",
        authorization_revision=1,
        resolved_at=NOW,
    )


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


class RestartFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.writer = context(Permission.COLLECTION_RUN)
        self.reader = context(Permission.ANALYSIS_READ, Permission.COLLECTION_READ)
        self.service = self.restart()

    def restart(self) -> ChannelDataService:
        self.service = ChannelDataService(state_store=self.store)
        return self.service

    def collect_subscribers(
        self, collection_id: str = "collection-1", key: str = "1"
    ) -> PublishSubscriberSnapshot:
        self.service.start_collection(
            self.writer,
            StartCollection(
                "channel-1",
                collection_id,
                CollectionKind.SUBSCRIBERS,
                NOW - timedelta(minutes=1),
                f"start-{key}",
            ),
        )
        command = PublishSubscriberSnapshot(
            "channel-1",
            collection_id,
            f"snapshot-{key}",
            NOW,
            SubscriberTraversalStatus.COMPLETE,
            LIMITATIONS,
            (SubscriberObservationInput("subscriber-1", "登録者1", NOW),),
            f"stage-{key}",
        )
        self.service.publish_subscriber_snapshot(self.writer, command)
        self.service.finish_collection(
            self.writer,
            FinishCollection(
                "channel-1",
                collection_id,
                CollectionStatus.COMPLETE,
                NOW + timedelta(minutes=1),
                1,
                1,
                None,
                f"finish-{key}",
            ),
        )
        return command


class CollectedDataRestartTests(RestartFixture):
    def test_a_published_snapshot_outlives_the_process(self) -> None:
        self.collect_subscribers()

        self.restart()

        snapshots = self.service.list_subscriber_snapshots(
            self.reader, SubscriberSnapshotQuery("channel-1")
        )
        self.assertEqual(len(snapshots.items), 1)
        self.assertEqual(snapshots.items[0].snapshot_id, "snapshot-1")

    def test_the_subscriber_registry_outlives_the_process(self) -> None:
        self.collect_subscribers()

        self.restart()

        registry = self.service.list_subscriber_registry(
            self.reader, SubscriberRegistryQuery("channel-1")
        )
        self.assertEqual(
            [entry.subscriber_channel_id for entry in registry.items], ["subscriber-1"]
        )

    def test_freshness_still_knows_the_last_collection(self) -> None:
        self.collect_subscribers()

        self.restart()

        freshness = self.service.get_freshness(self.reader, "channel-1")
        accepted = freshness.subscribers.latest_accepted_success
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.collection_id, "collection-1")

    def test_a_video_inventory_outlives_the_process(self) -> None:
        self.service.start_collection(
            self.writer,
            StartCollection(
                "channel-1",
                "collection-videos",
                CollectionKind.VIDEOS,
                NOW - timedelta(minutes=1),
                "start-v",
            ),
        )
        self.service.publish_video_inventory(
            self.writer,
            PublishVideoInventory(
                "channel-1",
                "collection-videos",
                "inventory-1",
                NOW,
                VideoCoverageScope.OWNER_VIDEOS,
                (VideoInput("video-1", "動画1", NOW),),
                "stage-v",
            ),
        )
        self.service.finish_collection(
            self.writer,
            FinishCollection(
                "channel-1",
                "collection-videos",
                CollectionStatus.COMPLETE,
                NOW + timedelta(minutes=1),
                1,
                1,
                None,
                "finish-v",
            ),
        )

        self.restart()

        freshness = self.service.get_freshness(self.reader, "channel-1")
        accepted = freshness.videos.latest_accepted_success
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.collection_id, "collection-videos")

    def test_a_replayed_publication_is_still_answered_once(self) -> None:
        command = self.collect_subscribers()

        self.restart()

        replayed = self.service.publish_subscriber_snapshot(self.writer, command)
        snapshots = self.service.list_subscriber_snapshots(
            self.reader, SubscriberSnapshotQuery("channel-1")
        )
        self.assertEqual(replayed.snapshot_id, "snapshot-1")
        self.assertEqual(len(snapshots.items), 1)


class DocumentTests(RestartFixture):
    def test_an_empty_store_is_not_written_until_something_happens(self) -> None:
        self.assertIsNone(self.store.document)
        self.assertEqual(self.store.saves, 0)

    def test_a_document_from_a_newer_version_is_refused(self) -> None:
        self.store.document = json.dumps({"version": 99})

        with self.assertRaises(ValueError):
            self.restart()

    def test_an_unreadable_document_is_refused(self) -> None:
        self.store.document = "{not json"

        with self.assertRaises(ValueError):
            self.restart()

    def test_a_service_without_a_store_still_works(self) -> None:
        service = ChannelDataService()

        service.start_collection(
            self.writer,
            StartCollection(
                "channel-1",
                "collection-1",
                CollectionKind.SUBSCRIBERS,
                NOW,
                "start-1",
            ),
        )

        history = service.list_collection_history(
            self.reader, CollectionHistoryQuery("channel-1")
        )
        self.assertEqual(len(history.items), 1)


if __name__ == "__main__":
    unittest.main()
