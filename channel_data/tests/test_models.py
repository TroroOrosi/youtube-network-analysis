from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from inspect import signature

from channel_data.errors import ChannelDataError, ErrorCode
from channel_data.models import (
    AuthorCommentActivity,
    ChannelDataFreshness,
    ChannelDataRetentionReport,
    CollectionFailureCode,
    CollectionHistoryQuery,
    CollectionKind,
    CollectionStatus,
    CollectionState,
    CommentCoverage,
    CoverageLimitation,
    DatasetReadinessCode,
    DeleteChannelData,
    DeleteWorkspaceData,
    FinishCollection,
    PageRequest,
    PublishSubscriberSnapshot,
    PublishVideoInventory,
    ReplaceVideoCommentActivity,
    SilentAnalysisDataset,
    StartCollection,
    SubscriberObservation,
    SubscriberObservationInput,
    SubscriberRegistryEntry,
    SubscriberRegistryQuery,
    SubscriberSnapshot,
    SubscriberSnapshotQuery,
    SubscriberTraversalStatus,
    Video,
    VideoCommentActivityInput,
    VideoCommentActivity,
    VideoCoverageScope,
    VideoInventory,
    VideoInput,
)
from channel_data.ports import (
    AnalysisDataReader,
    ChannelDataAdministrator,
    CollectionReader,
    CollectionWriter,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class ChannelDataErrorContractTests(unittest.TestCase):
    def test_error_codes_are_stable_machine_values(self) -> None:
        self.assertEqual(ErrorCode.INVALID_INPUT.value, "INVALID_INPUT")
        self.assertEqual(
            ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN.value,
            "RESOURCE_NOT_FOUND_OR_FORBIDDEN",
        )
        self.assertEqual(ErrorCode.DATASET_NOT_READY.value, "DATASET_NOT_READY")
        self.assertEqual(ErrorCode.INVALID_CURSOR.value, "INVALID_CURSOR")

    def test_errors_expose_only_stable_safe_fields(self) -> None:
        error = ChannelDataError(
            ErrorCode.PERMISSION_DENIED,
            message="Required permission is missing",
            field="context.permissions",
            retryable=False,
            correlation_id="correlation-1",
            reason_code="MISSING_PERMISSION",
        )

        self.assertEqual(error.code, "PERMISSION_DENIED")
        self.assertEqual(error.message, "Required permission is missing")
        self.assertEqual(error.field, "context.permissions")
        self.assertFalse(error.retryable)
        self.assertEqual(error.correlation_id, "correlation-1")
        self.assertEqual(error.reason_code, "MISSING_PERMISSION")
        self.assertEqual(str(error), error.message)

    def test_generated_correlation_id_is_nonempty_and_nonsecret(self) -> None:
        error = ChannelDataError(
            ErrorCode.INVALID_INPUT,
            message="Invalid input",
        )

        self.assertTrue(error.correlation_id.startswith("error_"))
        self.assertNotIn("Invalid input", error.correlation_id)


class ChannelDataValueContractTests(unittest.TestCase):
    def test_enums_use_the_approved_stable_values(self) -> None:
        self.assertEqual(
            {item.value for item in CoverageLimitation},
            {"PUBLIC_SUBSCRIPTIONS_ONLY", "PROVIDER_RESULT_CAP_POSSIBLE"},
        )
        self.assertEqual(
            {item.value for item in CollectionKind},
            {"SUBSCRIBERS", "VIDEOS", "COMMENTS"},
        )
        self.assertEqual(
            {item.value for item in CollectionStatus},
            {"IN_PROGRESS", "PARTIAL", "COMPLETE", "FAILED"},
        )
        self.assertEqual(SubscriberTraversalStatus.COMPLETE.value, "COMPLETE")
        self.assertEqual(VideoCoverageScope.OWNER_VIDEOS.value, "OWNER_VIDEOS")
        self.assertIn(
            CollectionFailureCode.QUOTA_EXHAUSTED,
            tuple(CollectionFailureCode),
        )
        self.assertEqual(
            DatasetReadinessCode.COMMENTS_INCOMPLETE.value,
            "COMMENTS_INCOMPLETE",
        )

    def test_external_value_objects_are_immutable_slotted_and_normalize_utc(self) -> None:
        offset = datetime(
            2026,
            8,
            20,
            21,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )
        observation = SubscriberObservationInput(
            subscriber_channel_id="subscriber-1",
            title="",
            api_published_at=offset,
        )
        video = VideoInput(
            video_id="video-1",
            title="Title",
            published_at=offset,
        )
        activity = VideoCommentActivityInput(
            author_channel_id="author-1",
            comment_count=2,
            last_comment_at=offset,
        )

        self.assertEqual(observation.api_published_at, NOW)
        self.assertEqual(video.published_at, NOW)
        self.assertEqual(activity.last_comment_at, NOW)
        for value in (observation, video, activity, PageRequest()):
            self.assertFalse(hasattr(value, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            observation.title = "changed"  # type: ignore[misc]

    def test_identifiers_and_display_text_are_bounded(self) -> None:
        invalid = (
            ("", "subscriber_channel_id"),
            (" ", "subscriber_channel_id"),
            ("x" * 257, "subscriber_channel_id"),
        )
        for value, field in invalid:
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ChannelDataError) as caught:
                    SubscriberObservationInput(value, "", None)
                self.assertEqual(caught.exception.code, "INVALID_INPUT")
                self.assertEqual(caught.exception.field, field)

        with self.assertRaises(ChannelDataError) as caught:
            VideoInput("video-1", "x" * 501, None)
        self.assertEqual(caught.exception.field, "title")

    def test_time_and_count_validation_rejects_malformed_external_values(self) -> None:
        with self.assertRaises(ChannelDataError) as caught:
            VideoInput("video-1", "Title", datetime(2026, 8, 20, 12, 0))
        self.assertEqual(caught.exception.field, "published_at")

        for count in (True, 0, -1):
            with self.subTest(count=count):
                with self.assertRaises(ChannelDataError) as caught:
                    VideoCommentActivityInput("author-1", count, NOW)
                self.assertEqual(caught.exception.field, "comment_count")

    def test_page_request_has_safe_bounded_defaults(self) -> None:
        self.assertEqual(PageRequest(), PageRequest(cursor=None, limit=100))
        self.assertEqual(PageRequest(limit=1).limit, 1)
        self.assertEqual(PageRequest(limit=500).limit, 500)

        for limit in (True, 0, 501):
            with self.subTest(limit=limit):
                with self.assertRaises(ChannelDataError) as caught:
                    PageRequest(limit=limit)
                self.assertEqual(caught.exception.field, "limit")

        for cursor in ("", " ", "x" * 513):
            with self.subTest(cursor_length=len(cursor)):
                with self.assertRaises(ChannelDataError) as caught:
                    PageRequest(cursor=cursor)
                self.assertEqual(caught.exception.field, "cursor")

    def test_output_records_are_immutable_and_normalize_nested_public_values(self) -> None:
        snapshot = SubscriberSnapshot(
            snapshot_id="snapshot-1",
            workspace_id="workspace-1",
            channel_id="channel-1",
            captured_at=NOW,
            observed_count=1,
            traversal_status=SubscriberTraversalStatus.COMPLETE,
            limitations=(
                CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
                CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
            ),
        )
        observation = SubscriberObservation(
            snapshot_id="snapshot-1",
            subscriber_channel_id="subscriber-1",
            title="Subscriber",
            api_published_at=NOW,
        )
        registry = SubscriberRegistryEntry(
            workspace_id="workspace-1",
            channel_id="channel-1",
            subscriber_channel_id="subscriber-1",
            title="Subscriber",
            api_published_at=NOW,
            first_seen_at=NOW,
            last_seen_at=NOW,
            observation_count=1,
            last_snapshot_id="snapshot-1",
        )
        inventory = VideoInventory(
            inventory_id="inventory-1",
            workspace_id="workspace-1",
            channel_id="channel-1",
            captured_at=NOW,
            coverage_scope=VideoCoverageScope.OWNER_VIDEOS,
            video_count=1,
        )
        video = Video(
            workspace_id="workspace-1",
            channel_id="channel-1",
            inventory_id="inventory-1",
            video_id="video-1",
            title="Video",
            published_at=NOW,
        )
        activity = VideoCommentActivity(
            workspace_id="workspace-1",
            channel_id="channel-1",
            inventory_id="inventory-1",
            video_id="video-1",
            author_channel_id="author-1",
            comment_count=2,
            last_comment_at=NOW,
        )
        coverage = CommentCoverage(
            inventory_id="inventory-1",
            coverage_scope=VideoCoverageScope.OWNER_VIDEOS,
            videos_expected=1,
            videos_covered=1,
            videos_missing=0,
            completed_at=NOW,
            is_complete=True,
        )
        state = CollectionState(
            collection_id="collection-1",
            workspace_id="workspace-1",
            channel_id="channel-1",
            kind=CollectionKind.SUBSCRIBERS,
            status=CollectionStatus.COMPLETE,
            started_at=NOW,
            completed_at=NOW,
            progress_current=1,
            progress_total=1,
            failure_code=None,
            accepted_generation_id="snapshot-1",
        )
        dataset = SilentAnalysisDataset(
            subscriber_registry=(registry,),
            author_activity=(AuthorCommentActivity("author-1", 2, NOW),),
            snapshot_id="snapshot-1",
            snapshot_captured_at=NOW,
            inventory_id="inventory-1",
            inventory_captured_at=NOW,
            subscriber_limitations=snapshot.limitations,
            comment_coverage=coverage,
        )

        for value in (
            snapshot,
            observation,
            inventory,
            video,
            activity,
            coverage,
            state,
            dataset,
            ChannelDataFreshness("channel-1", None, None, None),
            ChannelDataRetentionReport(0, 0, 0),
        ):
            self.assertFalse(hasattr(value, "__dict__"))

    def test_commands_reject_duplicates_and_forbidden_coverage_combinations(self) -> None:
        observations = (
            SubscriberObservationInput("subscriber-1", "First", NOW),
            SubscriberObservationInput("subscriber-1", "Second", NOW),
        )
        with self.assertRaises(ChannelDataError) as caught:
            PublishSubscriberSnapshot(
                "channel-1",
                "collection-1",
                "snapshot-1",
                NOW,
                SubscriberTraversalStatus.COMPLETE,
                (
                    CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
                    CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
                ),
                observations,
                "intent-1",
            )
        self.assertEqual(caught.exception.field, "observations")

        with self.assertRaises(ChannelDataError) as caught:
            PublishSubscriberSnapshot(
                "channel-1",
                "collection-1",
                "snapshot-1",
                NOW,
                SubscriberTraversalStatus.COMPLETE,
                (CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,),
                (),
                "intent-1",
            )
        self.assertEqual(caught.exception.field, "limitations")

        with self.assertRaises(ChannelDataError) as caught:
            PublishVideoInventory(
                "channel-1",
                "collection-1",
                "inventory-1",
                NOW,
                VideoCoverageScope.OWNER_VIDEOS,
                (VideoInput("video-1", "One", NOW), VideoInput("video-1", "Two", NOW)),
                "intent-2",
            )
        self.assertEqual(caught.exception.field, "videos")

        with self.assertRaises(ChannelDataError) as caught:
            ReplaceVideoCommentActivity(
                "channel-1",
                "collection-1",
                "inventory-1",
                "video-1",
                NOW,
                (
                    VideoCommentActivityInput("author-1", 1, NOW),
                    VideoCommentActivityInput("author-1", 2, NOW),
                ),
                "intent-3",
            )
        self.assertEqual(caught.exception.field, "activity")

    def test_finish_command_enforces_terminal_status_failure_contract(self) -> None:
        StartCollection("channel-1", "collection-1", CollectionKind.VIDEOS, NOW, "intent-1")
        FinishCollection(
            "channel-1",
            "collection-1",
            CollectionStatus.PARTIAL,
            NOW,
            1,
            2,
            None,
            "intent-2",
        )
        with self.assertRaises(ChannelDataError):
            FinishCollection(
                "channel-1",
                "collection-1",
                CollectionStatus.IN_PROGRESS,
                NOW,
                0,
                0,
                None,
                "intent-2",
            )
        with self.assertRaises(ChannelDataError):
            FinishCollection(
                "channel-1",
                "collection-1",
                CollectionStatus.FAILED,
                NOW,
                0,
                0,
                None,
                "intent-2",
            )
        with self.assertRaises(ChannelDataError):
            FinishCollection(
                "channel-1",
                "collection-1",
                CollectionStatus.COMPLETE,
                NOW,
                0,
                0,
                CollectionFailureCode.UNEXPECTED_FAILURE,
                "intent-2",
            )

    def test_queries_and_delete_commands_keep_workspace_scope_out_of_input(self) -> None:
        CollectionHistoryQuery("channel-1", CollectionKind.COMMENTS)
        SubscriberRegistryQuery("channel-1")
        SubscriberSnapshotQuery("channel-1")
        DeleteChannelData("channel-1", "delete-1")
        DeleteWorkspaceData("delete-workspace-1")

        for protocol in (
            CollectionWriter,
            CollectionReader,
            AnalysisDataReader,
            ChannelDataAdministrator,
        ):
            for name, member in protocol.__dict__.items():
                if name.startswith("_") or not callable(member):
                    continue
                parameters = tuple(signature(member).parameters)
                self.assertGreaterEqual(len(parameters), 2)
                self.assertEqual(parameters[1], "context")


if __name__ == "__main__":
    unittest.main()
