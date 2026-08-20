from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

from channel_data.errors import ChannelDataError, ErrorCode
from channel_data.models import (
    CollectionFailureCode,
    CollectionKind,
    CollectionStatus,
    CoverageLimitation,
    DatasetReadinessCode,
    PageRequest,
    SubscriberObservationInput,
    SubscriberTraversalStatus,
    VideoCommentActivityInput,
    VideoCoverageScope,
    VideoInput,
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


if __name__ == "__main__":
    unittest.main()
