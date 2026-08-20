"""Private in-memory state for the deterministic reference repository."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from .models import (
    CollectionState,
    CollectionStatus,
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

    def channel_exists(self, workspace_id: str, channel_id: str) -> bool:
        return any(
            key[0] == workspace_id and state.channel_id == channel_id
            for key, state in self.collections.items()
        ) or any(
            key[0] == workspace_id and key[1] == channel_id
            for mapping in (
                self.subscriber_snapshots,
                self.subscriber_registry,
                self.video_inventories,
                self.comment_activity,
            )
            for key in mapping
        )

    @staticmethod
    def _drop_keys(
        mapping: dict[Any, Any],
        predicate: Callable[[Any, Any], bool],
    ) -> None:
        for key in [
            key
            for key, value in mapping.items()
            if predicate(key, value)
        ]:
            mapping.pop(key, None)

    def delete_channel(self, workspace_id: str, channel_id: str) -> None:
        collection_ids = {
            state.collection_id
            for key, state in self.collections.items()
            if key[0] == workspace_id and state.channel_id == channel_id
        }
        self._drop_keys(
            self.collections,
            lambda key, state: key[0] == workspace_id and state.channel_id == channel_id,
        )
        for mapping in (
            self.subscriber_candidates,
            self.video_candidates,
            self.comment_candidates,
        ):
            self._drop_keys(
                mapping,
                lambda key, _: key[0] == workspace_id and key[1] in collection_ids,
            )
        for mapping in (
            self.subscriber_snapshots,
            self.subscriber_observations,
            self.subscriber_registry,
            self.video_inventories,
            self.videos,
            self.comment_activity,
            self.comment_coverage,
        ):
            self._drop_keys(
                mapping,
                lambda key, _: key[0] == workspace_id and key[1] == channel_id,
            )
        for mapping in (
            self.accepted_subscriber_snapshot,
            self.accepted_video_inventory,
        ):
            mapping.pop((workspace_id, channel_id), None)
        self._drop_keys(
            self.cursors,
            lambda key, record: key[0] == workspace_id
            and record.channel_id == channel_id,
        )
        self._drop_keys(
            self.idempotency,
            lambda key, record: key[0] == workspace_id
            and record.channel_id == channel_id,
        )

    def delete_workspace(self, workspace_id: str) -> None:
        for mapping in (
            self.collections,
            self.idempotency,
            self.subscriber_candidates,
            self.subscriber_snapshots,
            self.subscriber_observations,
            self.subscriber_registry,
            self.accepted_subscriber_snapshot,
            self.video_candidates,
            self.video_inventories,
            self.videos,
            self.accepted_video_inventory,
            self.comment_candidates,
            self.comment_activity,
            self.comment_coverage,
            self.cursors,
        ):
            self._drop_keys(mapping, lambda key, _: key[0] == workspace_id)

    def purge_retention(
        self,
        workspace_id: str,
        reference: datetime,
    ) -> tuple[int, int, int]:
        snapshot_cutoff = reference - timedelta(days=365)
        terminal_cutoff = reference - timedelta(days=90)
        snapshot_keys = [
            key
            for key, snapshot in self.subscriber_snapshots.items()
            if key[0] == workspace_id and snapshot.captured_at <= snapshot_cutoff
        ]
        for key in snapshot_keys:
            snapshot = self.subscriber_snapshots.pop(key)
            self.subscriber_observations.pop(key, None)
            channel_key = (key[0], key[1])
            if self.accepted_subscriber_snapshot.get(channel_key) == snapshot.snapshot_id:
                self.accepted_subscriber_snapshot.pop(channel_key, None)

        collection_keys = [
            key
            for key, state in self.collections.items()
            if key[0] == workspace_id
            and state.status is not CollectionStatus.IN_PROGRESS
            and state.completed_at is not None
            and state.completed_at <= terminal_cutoff
        ]
        for key in collection_keys:
            self.collections.pop(key, None)

        idempotency_keys = [
            key
            for key, record in self.idempotency.items()
            if key[0] == workspace_id and record.completed_at <= terminal_cutoff
        ]
        for key in idempotency_keys:
            self.idempotency.pop(key, None)
        if snapshot_keys or collection_keys or idempotency_keys:
            self._drop_keys(self.cursors, lambda key, _: key[0] == workspace_id)
        return len(snapshot_keys), len(collection_keys), len(idempotency_keys)
