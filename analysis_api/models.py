"""Immutable public values for workspace-authorized analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum

from subscriber_analytics.analytics_core import (
    AnalysisFilters,
    Segment,
    SubscriptionTimeSource,
)

from .errors import AnalysisApiError, ErrorCode


MAX_IDENTIFIER_LENGTH = 256
MAX_NAME_LENGTH = 120
MAX_CURSOR_LENGTH = 512
MAX_COMPARISON_CHANNELS = 5


class LimitationCode(str, Enum):
    PUBLIC_SUBSCRIPTIONS_ONLY = "PUBLIC_SUBSCRIPTIONS_ONLY"
    PROVIDER_RESULT_CAP_POSSIBLE = "PROVIDER_RESULT_CAP_POSSIBLE"


def _invalid(field_name: str, message: str) -> AnalysisApiError:
    return AnalysisApiError(ErrorCode.INVALID_INPUT, message=message, field=field_name)


def _identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_LENGTH
        or "\x00" in value
    ):
        raise _invalid(field_name, f"{field_name} must be a bounded identifier")
    return value


def _name(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_NAME_LENGTH
        or "\x00" in value
    ):
        raise _invalid(field_name, f"{field_name} must be bounded display text")
    return value.strip()


def _utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _invalid(field_name, f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(field_name, f"{field_name} must be a non-negative integer")
    return value


def _days(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
        raise _invalid(field_name, f"{field_name} must be an integer from 1 to 3650")
    return value


def _date(value: object, field_name: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise _invalid(field_name, f"{field_name} must be a date")
    return value


@dataclass(frozen=True, slots=True)
class AnalysisFilterInput:
    """The exact filter surface of analytics-core, validated at the boundary."""

    subscribed_within_days: int | None = None
    subscribed_since: date | None = None
    subscribed_until: date | None = None
    never_commented: bool = False
    no_comment_within_days: int | None = None
    include_not_seen_latest: bool = False
    segments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _days(self.subscribed_within_days, "subscribed_within_days")
        _date(self.subscribed_since, "subscribed_since")
        _date(self.subscribed_until, "subscribed_until")
        _days(self.no_comment_within_days, "no_comment_within_days")
        for flag_name in ("never_commented", "include_not_seen_latest"):
            if not isinstance(getattr(self, flag_name), bool):
                raise _invalid(flag_name, f"{flag_name} must be a boolean")
        if not isinstance(self.segments, tuple):
            raise _invalid("segments", "segments must be a tuple")
        for value in self.segments:
            if value not in {item.value for item in Segment}:
                raise _invalid("segments", "segments contains an unsupported value")
        if (
            self.subscribed_since is not None
            and self.subscribed_until is not None
            and self.subscribed_until < self.subscribed_since
        ):
            raise _invalid("subscribed_until", "subscribed_until precedes since")

    def to_core(self) -> AnalysisFilters:
        return AnalysisFilters(
            subscribed_within=(
                None
                if self.subscribed_within_days is None
                else timedelta(days=self.subscribed_within_days)
            ),
            subscribed_since=self.subscribed_since,
            subscribed_until=self.subscribed_until,
            never_commented=self.never_commented,
            no_comment_within=(
                None
                if self.no_comment_within_days is None
                else timedelta(days=self.no_comment_within_days)
            ),
            include_not_seen_latest=self.include_not_seen_latest,
            segments=frozenset(Segment(value) for value in self.segments),
        )


@dataclass(frozen=True, slots=True)
class AnalysisSummary:
    channel_id: str
    reference_time: datetime
    scope_count: int
    filtered_count: int
    scope_silent_count: int
    filtered_silent_count: int
    segment_counts: tuple[tuple[str, int], ...]
    limitations: tuple[LimitationCode, ...]
    snapshot_id: str
    inventory_id: str

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        object.__setattr__(
            self, "reference_time", _utc(self.reference_time, "reference_time")
        )
        for field_name in (
            "scope_count",
            "filtered_count",
            "scope_silent_count",
            "filtered_silent_count",
        ):
            _count(getattr(self, field_name), field_name)
        _identifier(self.snapshot_id, "snapshot_id")
        _identifier(self.inventory_id, "inventory_id")


@dataclass(frozen=True, slots=True)
class AnalysisRowView:
    subscriber_channel_id: str
    title: str
    subscribed_at: datetime
    subscribed_at_source: SubscriptionTimeSource
    segment: str
    comment_count: int
    last_comment_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.subscriber_channel_id, "subscriber_channel_id")
        object.__setattr__(
            self, "subscribed_at", _utc(self.subscribed_at, "subscribed_at")
        )
        _count(self.comment_count, "comment_count")


@dataclass(frozen=True, slots=True)
class AnalysisPage:
    summary: AnalysisSummary
    rows: tuple[AnalysisRowView, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple):
            raise _invalid("rows", "rows must be a tuple")
        if self.next_cursor is not None:
            _identifier(self.next_cursor, "next_cursor")


@dataclass(frozen=True, slots=True)
class AnalysisPageRequest:
    cursor: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if self.cursor is not None:
            if (
                not isinstance(self.cursor, str)
                or not self.cursor
                or len(self.cursor) > MAX_CURSOR_LENGTH
            ):
                raise _invalid("cursor", "cursor must be a bounded opaque token")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 200
        ):
            raise _invalid("limit", "limit must be an integer from 1 to 200")


@dataclass(frozen=True, slots=True)
class RunAnalysis:
    channel_id: str
    filters: AnalysisFilterInput = field(default_factory=AnalysisFilterInput)

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        if not isinstance(self.filters, AnalysisFilterInput):
            raise _invalid("filters", "filters must be an analysis filter input")


@dataclass(frozen=True, slots=True)
class CompareChannels:
    channel_ids: tuple[str, ...]
    filters: AnalysisFilterInput = field(default_factory=AnalysisFilterInput)

    def __post_init__(self) -> None:
        if not isinstance(self.channel_ids, tuple) or not self.channel_ids:
            raise _invalid("channel_ids", "channel_ids must be a non-empty tuple")
        if len(self.channel_ids) > MAX_COMPARISON_CHANNELS:
            raise _invalid("channel_ids", "at most five channels can be compared")
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise _invalid("channel_ids", "channel_ids must be unique")
        for channel_id in self.channel_ids:
            _identifier(channel_id, "channel_ids")


@dataclass(frozen=True, slots=True)
class ComparisonEntry:
    channel_id: str
    summary: AnalysisSummary | None
    not_ready_reason: str | None

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        if (self.summary is None) == (self.not_ready_reason is None):
            raise _invalid("summary", "an entry is either ready or not ready")


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    entries: tuple[ComparisonEntry, ...]
    reference_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise _invalid("entries", "entries must be a tuple")
        object.__setattr__(
            self, "reference_time", _utc(self.reference_time, "reference_time")
        )


@dataclass(frozen=True, slots=True)
class SavedView:
    view_id: str
    workspace_id: str
    name: str
    channel_id: str
    filters: AnalysisFilterInput
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.view_id, "view_id")
        _identifier(self.workspace_id, "workspace_id")
        object.__setattr__(self, "name", _name(self.name, "name"))
        _identifier(self.channel_id, "channel_id")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class SaveView:
    name: str
    channel_id: str
    filters: AnalysisFilterInput
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "name"))
        _identifier(self.channel_id, "channel_id")
        if not isinstance(self.filters, AnalysisFilterInput):
            raise _invalid("filters", "filters must be an analysis filter input")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class DeleteView:
    view_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.view_id, "view_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class ExportAnalysis:
    channel_id: str
    filters: AnalysisFilterInput = field(default_factory=AnalysisFilterInput)

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")
        if not isinstance(self.filters, AnalysisFilterInput):
            raise _invalid("filters", "filters must be an analysis filter input")


@dataclass(frozen=True, slots=True)
class ExportDocument:
    filename: str
    content_type: str
    content: bytes
    row_count: int

    def __post_init__(self) -> None:
        _identifier(self.filename, "filename")
        _identifier(self.content_type, "content_type")
        if not isinstance(self.content, bytes):
            raise _invalid("content", "content must be bytes")
        _count(self.row_count, "row_count")
