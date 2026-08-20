"""Private in-memory state for the deterministic reference repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from .models import (
    CollectionState,
    CommentCoverage,
    SubscriberObservation,
    SubscriberRegistryEntry,
    SubscriberSnapshot,
    Video,
    VideoCommentActivity,
    VideoCommentActivityInput,
    VideoInventory,
)


@dataclass(slots=True)
class IdempotencyRecord:
    actor_user_id: str
    channel_id: str | None
    operation: str
    payload_fingerprint: str
    result: object
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriberCandidate:
    snapshot: SubscriberSnapshot
    observations: tuple[SubscriberObservation, ...]


@dataclass(frozen=True, slots=True)
class VideoCandidate:
    inventory: VideoInventory
    videos: tuple[Video, ...]


@dataclass(slots=True)
class CommentCandidate:
    inventory_id: str
    replacements: dict[str, tuple[VideoCommentActivityInput, ...]] = field(
        default_factory=dict
    )
    replaced_at: dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CursorRecord:
    channel_id: str
    query_key: tuple[object, ...]
    items: tuple[object, ...]
    offset: int
    captured_revision: int


@dataclass(slots=True)
class MemoryState:
    revision: int = 0
    collections: dict[tuple[str, str], CollectionState] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)
    subscriber_candidates: dict[tuple[str, str], SubscriberCandidate] = field(
        default_factory=dict
    )
    subscriber_snapshots: dict[tuple[str, str, str], SubscriberSnapshot] = field(
        default_factory=dict
    )
    subscriber_observations: dict[
        tuple[str, str, str], tuple[SubscriberObservation, ...]
    ] = field(default_factory=dict)
    subscriber_registry: dict[
        tuple[str, str, str], SubscriberRegistryEntry
    ] = field(default_factory=dict)
    accepted_subscriber_snapshot: dict[tuple[str, str], str] = field(
        default_factory=dict
    )
    video_candidates: dict[tuple[str, str], VideoCandidate] = field(
        default_factory=dict
    )
    video_inventories: dict[tuple[str, str, str], VideoInventory] = field(
        default_factory=dict
    )
    videos: dict[tuple[str, str, str], tuple[Video, ...]] = field(
        default_factory=dict
    )
    accepted_video_inventory: dict[tuple[str, str], str] = field(
        default_factory=dict
    )
    comment_candidates: dict[tuple[str, str], CommentCandidate] = field(
        default_factory=dict
    )
    comment_activity: dict[
        tuple[str, str, str], tuple[VideoCommentActivity, ...]
    ] = field(default_factory=dict)
    comment_coverage: dict[tuple[str, str, str], CommentCoverage] = field(
        default_factory=dict
    )
    cursors: dict[tuple[str, str], CursorRecord] = field(default_factory=dict)
