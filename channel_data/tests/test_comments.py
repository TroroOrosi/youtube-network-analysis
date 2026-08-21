from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import UTC, datetime, timedelta

from channel_data.errors import ChannelDataError
from channel_data.models import (
    CollectionKind,
    CollectionStatus,
    CoverageLimitation,
    FinishCollection,
    PublishSubscriberSnapshot,
    PublishVideoInventory,
    ReplaceVideoCommentActivity,
    StartCollection,
    SubscriberObservationInput,
    SubscriberTraversalStatus,
    VideoCommentActivity,
    VideoCommentActivityInput,
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


class VideoCommentReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ChannelDataService()
        self.writer = context(Permission.COLLECTION_RUN)
        self.reader = context(Permission.ANALYSIS_READ)

    def start(self, collection_id: str, kind: CollectionKind) -> None:
        self.service.start_collection(
            self.writer,
            StartCollection(
                "channel-1",
                collection_id,
                kind,
                NOW,
                f"start-{collection_id}",
            ),
        )

    def finish(
        self,
        collection_id: str,
        status: CollectionStatus,
        current: int,
        total: int,
    ):
        return self.service.finish_collection(
            self.writer,
            FinishCollection(
                "channel-1",
                collection_id,
                status,
                NOW + timedelta(minutes=5),
                current,
                total,
                None,
                f"finish-{collection_id}",
            ),
        )

    def accept_subscribers(self) -> None:
        self.start("subscribers", CollectionKind.SUBSCRIBERS)
        self.service.publish_subscriber_snapshot(
            self.writer,
            PublishSubscriberSnapshot(
                "channel-1",
                "subscribers",
                "snapshot-1",
                NOW,
                SubscriberTraversalStatus.COMPLETE,
                LIMITATIONS,
                (
                    SubscriberObservationInput("silent", "Silent", NOW),
                    SubscriberObservationInput("active", "Active", NOW),
                ),
                "stage-subscribers",
            ),
        )
        self.finish("subscribers", CollectionStatus.COMPLETE, 2, 2)

    def accept_inventory(
        self,
        collection_id: str,
        inventory_id: str,
        scope: VideoCoverageScope,
        videos: tuple[VideoInput, ...],
        status: CollectionStatus = CollectionStatus.COMPLETE,
    ) -> None:
        self.start(collection_id, CollectionKind.VIDEOS)
        self.service.publish_video_inventory(
            self.writer,
            PublishVideoInventory(
                "channel-1",
                collection_id,
                inventory_id,
                NOW + timedelta(minutes=1),
                scope,
                videos,
                f"stage-{collection_id}",
            ),
        )
        self.finish(collection_id, status, len(videos), len(videos))

    def test_dataset_fails_closed_for_each_prerequisite(self) -> None:
        with self.assertRaises(ChannelDataError) as denied:
            self.service.load_silent_analysis_dataset(self.writer, "channel-1")
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

        with self.assertRaises(ChannelDataError) as caught:
            self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(caught.exception.reason_code, "NO_SUBSCRIBER_SNAPSHOT")

        self.accept_subscribers()
        with self.assertRaises(ChannelDataError) as caught:
            self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(caught.exception.reason_code, "NO_VIDEO_INVENTORY")

        self.accept_inventory(
            "public-videos",
            "public-inventory",
            VideoCoverageScope.PUBLIC_VIDEOS,
            (VideoInput("video-1", "Public", NOW),),
        )
        with self.assertRaises(ChannelDataError) as caught:
            self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(caught.exception.reason_code, "PUBLIC_VIDEO_SCOPE_ONLY")

        self.accept_inventory(
            "owner-videos",
            "owner-inventory",
            VideoCoverageScope.OWNER_VIDEOS,
            (VideoInput("video-1", "Owner", NOW),),
        )
        with self.assertRaises(ChannelDataError) as caught:
            self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(caught.exception.reason_code, "COMMENTS_INCOMPLETE")

    def test_complete_requires_every_video_including_explicit_empty_replacement(self) -> None:
        self.accept_subscribers()
        self.accept_inventory(
            "videos",
            "inventory-1",
            VideoCoverageScope.OWNER_VIDEOS,
            (
                VideoInput("video-1", "One", NOW),
                VideoInput("video-2", "Two", NOW),
                VideoInput("video-3", "Three", NOW),
            ),
        )
        self.start("comments-partial", CollectionKind.COMMENTS)
        coverage = self.service.replace_video_comment_activity(
            self.writer,
            ReplaceVideoCommentActivity(
                "channel-1",
                "comments-partial",
                "inventory-1",
                "video-1",
                NOW + timedelta(minutes=2),
                (VideoCommentActivityInput("active", 2, NOW),),
                "replace-one",
            ),
        )
        self.assertFalse(coverage.is_complete)
        self.assertEqual(coverage.videos_missing, 2)
        with self.assertRaises(ChannelDataError) as caught:
            self.finish("comments-partial", CollectionStatus.COMPLETE, 1, 3)
        self.assertEqual(caught.exception.code, "INVALID_COLLECTION_TRANSITION")

        self.finish("comments-partial", CollectionStatus.PARTIAL, 1, 3)
        self.start("comments-complete", CollectionKind.COMMENTS)
        self.service.replace_video_comment_activity(
            self.writer,
            ReplaceVideoCommentActivity(
                "channel-1",
                "comments-complete",
                "inventory-1",
                "video-1",
                NOW + timedelta(minutes=3),
                (VideoCommentActivityInput("active", 2, NOW),),
                "replace-complete-one",
            ),
        )
        self.service.replace_video_comment_activity(
            self.writer,
            ReplaceVideoCommentActivity(
                "channel-1",
                "comments-complete",
                "inventory-1",
                "video-2",
                NOW + timedelta(minutes=4),
                (),
                "replace-complete-empty",
            ),
        )
        complete_coverage = self.service.replace_video_comment_activity(
            self.writer,
            ReplaceVideoCommentActivity(
                "channel-1",
                "comments-complete",
                "inventory-1",
                "video-3",
                NOW + timedelta(minutes=5),
                (
                    VideoCommentActivityInput(
                        "active",
                        3,
                        NOW + timedelta(hours=1),
                    ),
                ),
                "replace-complete-three",
            ),
        )
        self.assertTrue(complete_coverage.is_complete)
        with self.assertRaises(ChannelDataError) as caught:
            self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(caught.exception.reason_code, "COMMENTS_INCOMPLETE")
        completed = self.finish("comments-complete", CollectionStatus.COMPLETE, 3, 3)
        self.assertEqual(completed.accepted_generation_id, "inventory-1")

        dataset = self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(dataset.inventory_id, "inventory-1")
        self.assertEqual(dataset.comment_coverage, complete_coverage)
        self.assertEqual(dataset.author_activity[0].author_channel_id, "active")
        self.assertEqual(dataset.author_activity[0].comment_count, 5)
        self.assertEqual(dataset.author_activity[0].last_comment_at, NOW + timedelta(hours=1))
        self.assertEqual(dataset.subscriber_limitations, LIMITATIONS)

    def test_partial_new_inventory_preserves_ready_dataset_and_old_inventory_is_rejected_after_replace(self) -> None:
        self.accept_subscribers()
        self.accept_inventory(
            "videos-1",
            "inventory-1",
            VideoCoverageScope.OWNER_VIDEOS,
            (VideoInput("video-1", "One", NOW),),
        )
        self.start("comments-1", CollectionKind.COMMENTS)
        self.service.replace_video_comment_activity(
            self.writer,
            ReplaceVideoCommentActivity(
                "channel-1",
                "comments-1",
                "inventory-1",
                "video-1",
                NOW + timedelta(minutes=2),
                (),
                "replace-1",
            ),
        )
        self.finish("comments-1", CollectionStatus.COMPLETE, 1, 1)

        self.accept_inventory(
            "videos-partial",
            "inventory-partial",
            VideoCoverageScope.OWNER_VIDEOS,
            (VideoInput("video-2", "Two", NOW),),
            status=CollectionStatus.PARTIAL,
        )
        self.assertEqual(
            self.service.load_silent_analysis_dataset(self.reader, "channel-1").inventory_id,
            "inventory-1",
        )

        self.accept_inventory(
            "videos-2",
            "inventory-2",
            VideoCoverageScope.OWNER_VIDEOS,
            (VideoInput("video-2", "Two", NOW),),
        )
        self.start("comments-stale", CollectionKind.COMMENTS)
        with self.assertRaises(ChannelDataError):
            self.service.replace_video_comment_activity(
                self.writer,
                ReplaceVideoCommentActivity(
                    "channel-1",
                    "comments-stale",
                    "inventory-1",
                    "video-1",
                    NOW,
                    (),
                    "stale-replacement",
                ),
            )
        with self.assertRaises(ChannelDataError) as caught:
            self.service.load_silent_analysis_dataset(self.reader, "channel-1")
        self.assertEqual(caught.exception.reason_code, "COMMENTS_INCOMPLETE")

    def test_stored_comment_record_has_only_minimized_aggregate_fields(self) -> None:
        self.assertEqual(
            {field.name for field in fields(VideoCommentActivity)},
            {
                "workspace_id",
                "channel_id",
                "inventory_id",
                "video_id",
                "author_channel_id",
                "comment_count",
                "last_comment_at",
            },
        )


if __name__ == "__main__":
    unittest.main()
