"""Immutable public values for tenant-scoped channel data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Generic, TypeVar

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


def _nonnegative_count(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_COUNT
    ):
        raise _invalid(field, f"{field} must be a bounded non-negative integer")
    return value


def _enum(value: object, enum_type: type[Enum], field: str) -> None:
    if not isinstance(value, enum_type):
        raise _invalid(field, f"{field} has an unsupported value")


def _tuple(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise _invalid(field, f"{field} must be a tuple")
    return value


def _subscriber_limitations(value: object) -> tuple[CoverageLimitation, ...]:
    limitations = _tuple(value, "limitations")
    if (
        len(limitations) != 2
        or any(not isinstance(item, CoverageLimitation) for item in limitations)
        or set(limitations)
        != {
            CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
            CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
        }
    ):
        raise _invalid(
            "limitations",
            "subscriber coverage must include the approved public-only limitations",
        )
    return limitations  # type: ignore[return-value]


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


@dataclass(frozen=True, slots=True)
class SubscriberSnapshot:
    snapshot_id: str
    workspace_id: str
    channel_id: str
    captured_at: datetime
    observed_count: int
    traversal_status: SubscriberTraversalStatus
    limitations: tuple[CoverageLimitation, ...]

    def __post_init__(self) -> None:
        _identifier(self.snapshot_id, "snapshot_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.channel_id, "channel_id")
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        _nonnegative_count(self.observed_count, "observed_count")
        _enum(self.traversal_status, SubscriberTraversalStatus, "traversal_status")
        _subscriber_limitations(self.limitations)


@dataclass(frozen=True, slots=True)
class SubscriberObservation:
    snapshot_id: str
    subscriber_channel_id: str
    title: str
    api_published_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.snapshot_id, "snapshot_id")
        _identifier(self.subscriber_channel_id, "subscriber_channel_id")
        _display_text(self.title, "title")
        object.__setattr__(
            self,
            "api_published_at",
            _optional_utc(self.api_published_at, "api_published_at"),
        )


@dataclass(frozen=True, slots=True)
class SubscriberRegistryEntry:
    workspace_id: str
    channel_id: str
    subscriber_channel_id: str
    title: str
    api_published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int
    last_snapshot_id: str

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.channel_id, "channel_id")
        _identifier(self.subscriber_channel_id, "subscriber_channel_id")
        _identifier(self.last_snapshot_id, "last_snapshot_id")
        _display_text(self.title, "title")
        object.__setattr__(
            self,
            "api_published_at",
            _optional_utc(self.api_published_at, "api_published_at"),
        )
        first = _utc(self.first_seen_at, "first_seen_at")
        last = _utc(self.last_seen_at, "last_seen_at")
        if first > last:
            raise _invalid("last_seen_at", "last_seen_at must not precede first_seen_at")
        object.__setattr__(self, "first_seen_at", first)
        object.__setattr__(self, "last_seen_at", last)
        _positive_count(self.observation_count, "observation_count")


@dataclass(frozen=True, slots=True)
class VideoInventory:
    inventory_id: str
    workspace_id: str
    channel_id: str
    captured_at: datetime
    coverage_scope: VideoCoverageScope
    video_count: int

    def __post_init__(self) -> None:
        _identifier(self.inventory_id, "inventory_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.channel_id, "channel_id")
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        _enum(self.coverage_scope, VideoCoverageScope, "coverage_scope")
        _nonnegative_count(self.video_count, "video_count")


@dataclass(frozen=True, slots=True)
class Video:
    workspace_id: str
    channel_id: str
    inventory_id: str
    video_id: str
    title: str
    published_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.channel_id, "channel_id")
        _identifier(self.inventory_id, "inventory_id")
        _identifier(self.video_id, "video_id")
        _display_text(self.title, "title")
        object.__setattr__(
            self,
            "published_at",
            _optional_utc(self.published_at, "published_at"),
        )


@dataclass(frozen=True, slots=True)
class VideoCommentActivity:
    workspace_id: str
    channel_id: str
    inventory_id: str
    video_id: str
    author_channel_id: str
    comment_count: int
    last_comment_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.channel_id, "channel_id")
        _identifier(self.inventory_id, "inventory_id")
        _identifier(self.video_id, "video_id")
        _identifier(self.author_channel_id, "author_channel_id")
        _positive_count(self.comment_count, "comment_count")
        object.__setattr__(
            self,
            "last_comment_at",
            _utc(self.last_comment_at, "last_comment_at"),
        )


@dataclass(frozen=True, slots=True)
class CommentCoverage:
    inventory_id: str
    coverage_scope: VideoCoverageScope
    videos_expected: int
    videos_covered: int
    videos_missing: int
    completed_at: datetime | None
    is_complete: bool

    def __post_init__(self) -> None:
        _identifier(self.inventory_id, "inventory_id")
        _enum(self.coverage_scope, VideoCoverageScope, "coverage_scope")
        expected = _nonnegative_count(self.videos_expected, "videos_expected")
        covered = _nonnegative_count(self.videos_covered, "videos_covered")
        missing = _nonnegative_count(self.videos_missing, "videos_missing")
        if covered + missing != expected:
            raise _invalid("videos_missing", "coverage counts must balance")
        if not isinstance(self.is_complete, bool):
            raise _invalid("is_complete", "is_complete must be boolean")
        completed = _optional_utc(self.completed_at, "completed_at")
        if self.is_complete != (missing == 0) or self.is_complete != (completed is not None):
            raise _invalid("is_complete", "completion must match coverage counts and time")
        object.__setattr__(self, "completed_at", completed)


@dataclass(frozen=True, slots=True)
class CollectionState:
    collection_id: str
    workspace_id: str
    channel_id: str
    kind: CollectionKind
    status: CollectionStatus
    started_at: datetime
    completed_at: datetime | None
    progress_current: int
    progress_total: int
    failure_code: CollectionFailureCode | None
    accepted_generation_id: str | None

    def __post_init__(self) -> None:
        _identifier(self.collection_id, "collection_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.channel_id, "channel_id")
        _enum(self.kind, CollectionKind, "kind")
        _enum(self.status, CollectionStatus, "status")
        started = _utc(self.started_at, "started_at")
        completed = _optional_utc(self.completed_at, "completed_at")
        current = _nonnegative_count(self.progress_current, "progress_current")
        total = _nonnegative_count(self.progress_total, "progress_total")
        if current > total:
            raise _invalid("progress_current", "progress_current must not exceed total")
        if completed is not None and completed < started:
            raise _invalid("completed_at", "completed_at must not precede started_at")
        if self.failure_code is not None:
            _enum(self.failure_code, CollectionFailureCode, "failure_code")
        if self.accepted_generation_id is not None:
            _identifier(self.accepted_generation_id, "accepted_generation_id")
        if self.status is CollectionStatus.IN_PROGRESS:
            if completed is not None or self.failure_code is not None or self.accepted_generation_id is not None:
                raise _invalid("status", "in-progress state cannot be terminal")
        else:
            if completed is None:
                raise _invalid("completed_at", "terminal state requires completed_at")
            if self.status is CollectionStatus.FAILED and self.failure_code is None:
                raise _invalid("failure_code", "failed state requires a failure code")
            if self.status is not CollectionStatus.FAILED and self.failure_code is not None:
                raise _invalid("failure_code", "only failed state carries a failure code")
            if (
                self.status is CollectionStatus.COMPLETE
                and self.accepted_generation_id is None
            ):
                raise _invalid(
                    "accepted_generation_id",
                    "complete state requires an accepted generation",
                )
            if self.status is not CollectionStatus.COMPLETE and self.accepted_generation_id is not None:
                raise _invalid("accepted_generation_id", "only complete state accepts a generation")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)


@dataclass(frozen=True, slots=True)
class CollectionFreshness:
    latest_attempt: CollectionState | None
    latest_accepted_success: CollectionState | None


@dataclass(frozen=True, slots=True)
class ChannelDataFreshness:
    channel_id: str
    subscribers: CollectionFreshness | None
    videos: CollectionFreshness | None
    comments: CollectionFreshness | None

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")


@dataclass(frozen=True, slots=True)
class AuthorCommentActivity:
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


@dataclass(frozen=True, slots=True)
class SilentAnalysisDataset:
    subscriber_registry: tuple[SubscriberRegistryEntry, ...]
    author_activity: tuple[AuthorCommentActivity, ...]
    snapshot_id: str
    snapshot_captured_at: datetime
    inventory_id: str
    inventory_captured_at: datetime
    subscriber_limitations: tuple[CoverageLimitation, ...]
    comment_coverage: CommentCoverage

    def __post_init__(self) -> None:
        _tuple(self.subscriber_registry, "subscriber_registry")
        _tuple(self.author_activity, "author_activity")
        _identifier(self.snapshot_id, "snapshot_id")
        _identifier(self.inventory_id, "inventory_id")
        object.__setattr__(
            self,
            "snapshot_captured_at",
            _utc(self.snapshot_captured_at, "snapshot_captured_at"),
        )
        object.__setattr__(
            self,
            "inventory_captured_at",
            _utc(self.inventory_captured_at, "inventory_captured_at"),
        )
        _subscriber_limitations(self.subscriber_limitations)
        if self.comment_coverage.inventory_id != self.inventory_id:
            raise _invalid("comment_coverage", "coverage must name the dataset inventory")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        _tuple(self.items, "items")
        if self.next_cursor is not None:
            _identifier(self.next_cursor, "next_cursor")


@dataclass(frozen=True, slots=True)
class CollectionHistoryQuery:
    channel_id: str
    kind: CollectionKind | None = None

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        if self.kind is not None:
            _enum(self.kind, CollectionKind, "kind")


@dataclass(frozen=True, slots=True)
class SubscriberRegistryQuery:
    channel_id: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")


@dataclass(frozen=True, slots=True)
class SubscriberSnapshotQuery:
    channel_id: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")


@dataclass(frozen=True, slots=True)
class StartCollection:
    channel_id: str
    collection_id: str
    kind: CollectionKind
    started_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        _identifier(self.collection_id, "collection_id")
        _enum(self.kind, CollectionKind, "kind")
        object.__setattr__(self, "started_at", _utc(self.started_at, "started_at"))
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class PublishSubscriberSnapshot:
    channel_id: str
    collection_id: str
    snapshot_id: str
    captured_at: datetime
    traversal_status: SubscriberTraversalStatus
    limitations: tuple[CoverageLimitation, ...]
    observations: tuple[SubscriberObservationInput, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        _identifier(self.collection_id, "collection_id")
        _identifier(self.snapshot_id, "snapshot_id")
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        _enum(self.traversal_status, SubscriberTraversalStatus, "traversal_status")
        _subscriber_limitations(self.limitations)
        rows = _tuple(self.observations, "observations")
        if any(not isinstance(row, SubscriberObservationInput) for row in rows):
            raise _invalid("observations", "observations contain an unsupported value")
        keys = [row.subscriber_channel_id for row in self.observations]
        if len(keys) != len(set(keys)):
            raise _invalid("observations", "observations must have unique subscriber ids")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class PublishVideoInventory:
    channel_id: str
    collection_id: str
    inventory_id: str
    captured_at: datetime
    coverage_scope: VideoCoverageScope
    videos: tuple[VideoInput, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        _identifier(self.collection_id, "collection_id")
        _identifier(self.inventory_id, "inventory_id")
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        _enum(self.coverage_scope, VideoCoverageScope, "coverage_scope")
        rows = _tuple(self.videos, "videos")
        if any(not isinstance(row, VideoInput) for row in rows):
            raise _invalid("videos", "videos contain an unsupported value")
        keys = [row.video_id for row in self.videos]
        if len(keys) != len(set(keys)):
            raise _invalid("videos", "videos must have unique video ids")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class ReplaceVideoCommentActivity:
    channel_id: str
    collection_id: str
    inventory_id: str
    video_id: str
    replaced_at: datetime
    activity: tuple[VideoCommentActivityInput, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        _identifier(self.collection_id, "collection_id")
        _identifier(self.inventory_id, "inventory_id")
        _identifier(self.video_id, "video_id")
        object.__setattr__(self, "replaced_at", _utc(self.replaced_at, "replaced_at"))
        rows = _tuple(self.activity, "activity")
        if any(not isinstance(row, VideoCommentActivityInput) for row in rows):
            raise _invalid("activity", "activity contains an unsupported value")
        keys = [row.author_channel_id for row in self.activity]
        if len(keys) != len(set(keys)):
            raise _invalid("activity", "activity must have unique author ids")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class FinishCollection:
    channel_id: str
    collection_id: str
    status: CollectionStatus
    completed_at: datetime
    progress_current: int
    progress_total: int
    failure_code: CollectionFailureCode | None
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        _identifier(self.collection_id, "collection_id")
        _enum(self.status, CollectionStatus, "status")
        if self.status is CollectionStatus.IN_PROGRESS:
            raise _invalid("status", "finish status must be terminal")
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))
        current = _nonnegative_count(self.progress_current, "progress_current")
        total = _nonnegative_count(self.progress_total, "progress_total")
        if current > total:
            raise _invalid("progress_current", "progress_current must not exceed total")
        if self.status is CollectionStatus.FAILED:
            if not isinstance(self.failure_code, CollectionFailureCode):
                raise _invalid("failure_code", "failed finish requires a failure code")
        elif self.failure_code is not None:
            raise _invalid("failure_code", "only failed finish carries a failure code")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class DeleteChannelData:
    channel_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class DeleteWorkspaceData:
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class ChannelDataRetentionReport:
    snapshots_removed: int
    collection_attempts_removed: int
    idempotency_records_removed: int

    def __post_init__(self) -> None:
        _nonnegative_count(self.snapshots_removed, "snapshots_removed")
        _nonnegative_count(self.collection_attempts_removed, "collection_attempts_removed")
        _nonnegative_count(self.idempotency_records_removed, "idempotency_records_removed")
