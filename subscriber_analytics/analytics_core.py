"""Pure subscriber segmentation and filtering contracts.

This module intentionally performs no file, network, credential, or UI work.
Adapters normalize external data into these immutable records before analysis.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence


class Segment(str, Enum):
    NEW_SILENT = "NEW_SILENT"
    OLD_SILENT = "OLD_SILENT"
    DORMANT = "DORMANT"
    ACTIVE = "ACTIVE"


class SubscriptionTimeSource(str, Enum):
    API_PUBLISHED_AT = "API_PUBLISHED_AT"
    FIRST_SEEN_AT = "FIRST_SEEN_AT"


class LimitationCode(str, Enum):
    PUBLIC_SUBSCRIPTIONS_ONLY = "PUBLIC_SUBSCRIPTIONS_ONLY"


class AnalysisValidationError(ValueError):
    """Invalid core input with a stable machine-readable error code."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class SubscriberRecord:
    channel_id: str
    title: str
    api_published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class CommentActivity:
    author_channel_id: str
    comment_count: int
    last_comment_at: datetime | None


@dataclass(frozen=True, slots=True)
class SegmentPolicy:
    recent_subscriber_window: timedelta = timedelta(days=90)
    recent_activity_window: timedelta = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class AnalysisFilters:
    subscribed_within: timedelta | None = None
    subscribed_since: date | None = None
    subscribed_until: date | None = None
    never_commented: bool = False
    no_comment_within: timedelta | None = None
    include_not_seen_latest: bool = False
    segments: frozenset[Segment] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    reference_time: datetime
    filters: AnalysisFilters = field(default_factory=AnalysisFilters)
    segment_policy: SegmentPolicy = field(default_factory=SegmentPolicy)


@dataclass(frozen=True, slots=True)
class AnalyzedSubscriber:
    channel_id: str
    title: str
    api_published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    subscribed_at: datetime
    subscribed_at_source: SubscriptionTimeSource
    is_in_latest_observation: bool
    comment_count: int
    last_comment_at: datetime | None
    segment: Segment


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    rows: tuple[AnalyzedSubscriber, ...]
    reference_time: datetime
    scope_count: int
    filtered_count: int
    excluded_not_seen_latest_count: int
    scope_segment_counts: Mapping[Segment, int]
    filtered_segment_counts: Mapping[Segment, int]
    scope_silent_count: int
    filtered_silent_count: int
    filters: AnalysisFilters
    segment_policy: SegmentPolicy
    limitations: tuple[LimitationCode, ...]


def _to_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalysisValidationError(
            "NAIVE_DATETIME",
            field_name,
            f"{field_name} must be timezone-aware",
        )
    return value.astimezone(UTC)


def _segment_counts(rows: Sequence[AnalyzedSubscriber]) -> Mapping[Segment, int]:
    counts = Counter(row.segment for row in rows)
    return MappingProxyType({segment: counts[segment] for segment in Segment})


def _silent_count(counts: Mapping[Segment, int]) -> int:
    return counts[Segment.NEW_SILENT] + counts[Segment.OLD_SILENT]


def _sorted_rows(
    rows: Sequence[AnalyzedSubscriber],
) -> tuple[AnalyzedSubscriber, ...]:
    channel_ordered = sorted(rows, key=lambda row: row.channel_id)
    return tuple(
        sorted(channel_ordered, key=lambda row: row.subscribed_at, reverse=True)
    )


def analyze(
    subscribers: Sequence[SubscriberRecord],
    comment_activity: Sequence[CommentActivity],
    request: AnalysisRequest,
) -> AnalysisResult:
    """Return deterministic subscriber analysis without I/O or input mutation."""

    reference_time = _to_utc(request.reference_time, "request.reference_time")
    activity_by_channel = {
        activity.author_channel_id: activity for activity in comment_activity
    }
    normalized_subscribers = [
        (
            subscriber,
            _to_utc(
                subscriber.api_published_at,
                f"subscribers[{index}].api_published_at",
            )
            if subscriber.api_published_at is not None
            else None,
            _to_utc(
                subscriber.first_seen_at,
                f"subscribers[{index}].first_seen_at",
            ),
            _to_utc(
                subscriber.last_seen_at,
                f"subscribers[{index}].last_seen_at",
            ),
        )
        for index, subscriber in enumerate(subscribers)
    ]
    latest_observation = max(
        (last_seen_at for _, _, _, last_seen_at in normalized_subscribers),
        default=None,
    )
    recent_subscriber_cutoff = (
        reference_time - request.segment_policy.recent_subscriber_window
    )
    recent_activity_cutoff = (
        reference_time - request.segment_policy.recent_activity_window
    )

    analyzed_rows: list[AnalyzedSubscriber] = []
    for subscriber, api_published_at, first_seen_at, last_seen_at in normalized_subscribers:
        if api_published_at is not None:
            subscribed_at = api_published_at
            subscribed_at_source = SubscriptionTimeSource.API_PUBLISHED_AT
        else:
            subscribed_at = first_seen_at
            subscribed_at_source = SubscriptionTimeSource.FIRST_SEEN_AT

        activity = activity_by_channel.get(subscriber.channel_id)
        comment_count = activity.comment_count if activity is not None else 0
        last_comment_at = (
            _to_utc(
                activity.last_comment_at,
                f"comment_activity[{subscriber.channel_id}].last_comment_at",
            )
            if activity is not None and activity.last_comment_at is not None
            else None
        )
        if comment_count > 0 and last_comment_at is not None:
            segment = (
                Segment.ACTIVE
                if last_comment_at >= recent_activity_cutoff
                else Segment.DORMANT
            )
        elif subscribed_at >= recent_subscriber_cutoff:
            segment = Segment.NEW_SILENT
        else:
            segment = Segment.OLD_SILENT

        analyzed_rows.append(
            AnalyzedSubscriber(
                channel_id=subscriber.channel_id,
                title=subscriber.title,
                api_published_at=api_published_at,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                subscribed_at=subscribed_at,
                subscribed_at_source=subscribed_at_source,
                is_in_latest_observation=last_seen_at == latest_observation,
                comment_count=comment_count,
                last_comment_at=last_comment_at,
                segment=segment,
            )
        )

    if request.filters.include_not_seen_latest:
        scope_rows = analyzed_rows
        excluded_not_seen_latest_count = 0
    else:
        scope_rows = [row for row in analyzed_rows if row.is_in_latest_observation]
        excluded_not_seen_latest_count = len(analyzed_rows) - len(scope_rows)

    rows = _sorted_rows(scope_rows)
    scope_segment_counts = _segment_counts(scope_rows)
    filtered_segment_counts = _segment_counts(rows)
    return AnalysisResult(
        rows=rows,
        reference_time=reference_time,
        scope_count=len(scope_rows),
        filtered_count=len(rows),
        excluded_not_seen_latest_count=excluded_not_seen_latest_count,
        scope_segment_counts=scope_segment_counts,
        filtered_segment_counts=filtered_segment_counts,
        scope_silent_count=_silent_count(scope_segment_counts),
        filtered_silent_count=_silent_count(filtered_segment_counts),
        filters=request.filters,
        segment_policy=request.segment_policy,
        limitations=(LimitationCode.PUBLIC_SUBSCRIPTIONS_ONLY,),
    )
