"""Immutable public values for tenant-scoped channel data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from .errors import ChannelDataError, ErrorCode


MAX_IDENTIFIER_LENGTH = 256
MAX_DISPLAY_TEXT_LENGTH = 500
MAX_CURSOR_LENGTH = 512
MAX_COUNT = 2**63 - 1


class CoverageLimitation(str, Enum):
    PUBLIC_SUBSCRIPTIONS_ONLY = "PUBLIC_SUBSCRIPTIONS_ONLY"
    PROVIDER_RESULT_CAP_POSSIBLE = "PROVIDER_RESULT_CAP_POSSIBLE"


class SubscriberTraversalStatus(str, Enum):
    COMPLETE = "COMPLETE"


class VideoCoverageScope(str, Enum):
    OWNER_VIDEOS = "OWNER_VIDEOS"
    PUBLIC_VIDEOS = "PUBLIC_VIDEOS"


class CollectionKind(str, Enum):
    SUBSCRIBERS = "SUBSCRIBERS"
    VIDEOS = "VIDEOS"
    COMMENTS = "COMMENTS"


class CollectionStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class CollectionFailureCode(str, Enum):
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    CANCELLED = "CANCELLED"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


class DatasetReadinessCode(str, Enum):
    NO_SUBSCRIBER_SNAPSHOT = "NO_SUBSCRIBER_SNAPSHOT"
    NO_VIDEO_INVENTORY = "NO_VIDEO_INVENTORY"
    PUBLIC_VIDEO_SCOPE_ONLY = "PUBLIC_VIDEO_SCOPE_ONLY"
    COMMENTS_INCOMPLETE = "COMMENTS_INCOMPLETE"


def _invalid(field: str, message: str) -> ChannelDataError:
    return ChannelDataError(
        ErrorCode.INVALID_INPUT,
        message=message,
        field=field,
    )


def _identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_LENGTH
        or "\x00" in value
    ):
        raise _invalid(field, f"{field} must be a bounded non-empty identifier")
    return value


def _display_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_DISPLAY_TEXT_LENGTH
        or "\x00" in value
    ):
        raise _invalid(field, f"{field} must be bounded text")
    return value


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _invalid(field, f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _optional_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _utc(value, field)


def _positive_count(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_COUNT
    ):
        raise _invalid(field, f"{field} must be a bounded positive integer")
    return value


@dataclass(frozen=True, slots=True)
class PageRequest:
    cursor: str | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        if self.cursor is not None:
            if (
                not isinstance(self.cursor, str)
                or not self.cursor
                or self.cursor != self.cursor.strip()
                or len(self.cursor) > MAX_CURSOR_LENGTH
            ):
                raise _invalid("cursor", "cursor must be a bounded opaque token")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 500
        ):
            raise _invalid("limit", "limit must be an integer from 1 to 500")


@dataclass(frozen=True, slots=True)
class SubscriberObservationInput:
    subscriber_channel_id: str
    title: str
    api_published_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.subscriber_channel_id, "subscriber_channel_id")
        _display_text(self.title, "title")
        object.__setattr__(
            self,
            "api_published_at",
            _optional_utc(self.api_published_at, "api_published_at"),
        )


@dataclass(frozen=True, slots=True)
class VideoInput:
    video_id: str
    title: str
    published_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.video_id, "video_id")
        _display_text(self.title, "title")
        object.__setattr__(
            self,
            "published_at",
            _optional_utc(self.published_at, "published_at"),
        )


@dataclass(frozen=True, slots=True)
class VideoCommentActivityInput:
    author_channel_id: str
    comment_count: int
    last_comment_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.author_channel_id, "author_channel_id")
        _positive_count(self.comment_count, "comment_count")
        object.__setattr__(
            self,
            "last_comment_at",
            _utc(self.last_comment_at, "last_comment_at"),
        )
