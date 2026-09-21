"""Authorized in-memory facade for tenant-scoped channel data."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from types import TracebackType
from typing import Any

from workspace_access.models import Permission, WorkspaceContext

from .errors import ChannelDataError, ErrorCode
from .memory import (
    CommentCandidate,
    CursorRecord,
    IdempotencyRecord,
    MemoryState,
    SubscriberCandidate,
    VideoCandidate,
)
from . import snapshot
from .ports import Clock, StateStore, TokenGenerator
from .models import (
    AuthorCommentActivity,
    ChannelDataFreshness,
    ChannelDataRetentionReport,
    CommentCoverage,
    CollectionFreshness,
    CollectionHistoryQuery,
    CollectionKind,
    CollectionState,
    CollectionStatus,
    DatasetReadinessCode,
    DeleteChannelData,
    DeleteWorkspaceData,
    FinishCollection,
    Page,
    PageRequest,
    PublishSubscriberSnapshot,
    PublishVideoInventory,
    ReplaceVideoCommentActivity,
    SilentAnalysisDataset,
    StartCollection,
    SubscriberObservation,
    SubscriberRegistryEntry,
    SubscriberRegistryQuery,
    SubscriberSnapshot,
    SubscriberSnapshotQuery,
    Video,
    VideoCommentActivity,
    VideoCoverageScope,
    VideoInventory,
)


_NO_REPLAY = object()


def _safe_error(code: ErrorCode, *, retryable: bool = False) -> ChannelDataError:
    messages = {
        ErrorCode.PERMISSION_DENIED: "Permission denied",
        ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN: "Resource not found or forbidden",
        ErrorCode.IDEMPOTENCY_CONFLICT: "Idempotency key conflicts with another operation",
        ErrorCode.OPERATION_IN_PROGRESS: "Operation is already in progress",
        ErrorCode.INVALID_COLLECTION_TRANSITION: "Invalid collection transition",
        ErrorCode.INVALID_CURSOR: "Invalid cursor",
        ErrorCode.CURSOR_EXPIRED: "Cursor expired",
    }
    return ChannelDataError(code, message=messages[code], retryable=retryable)


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, tuple):
        return tuple(_canonical(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(
            (item.name, _canonical(getattr(value, item.name)))
            for item in fields(value)
            if item.name != "idempotency_key"
        )
    return value


def _fingerprint(value: object) -> str:
    encoded = repr(_canonical(value)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dataset_not_ready(reason: DatasetReadinessCode) -> ChannelDataError:
    return ChannelDataError(
        ErrorCode.DATASET_NOT_READY,
        message="Silent analysis dataset is not ready",
        reason_code=reason.value,
    )


class _StateLock:
    """The service lock, which also writes the state document on release.

    Every command mutates under this lock, so the end of the outermost hold is
    the one moment a snapshot is both consistent and impossible to forget.
    """

    def __init__(self, flush: Callable[[], None]) -> None:
        self._lock = RLock()
        self._flush = flush
        self._depth = 0

    def __enter__(self) -> None:
        self._lock.acquire()
        self._depth += 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._depth -= 1
        try:
            if self._depth == 0:
                self._flush()
        finally:
            self._lock.release()


class ChannelDataService:
    """Standard-library reference implementation with one atomic lock."""

    def __init__(
        self,
        state: MemoryState | None = None,
        token_generator: TokenGenerator | None = None,
        clock: Clock | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._store = state_store
        self._document: str | None = None
        self._state = state or self._restored() or MemoryState()
        self._lock = _StateLock(self._flush)
        self._token_generator = token_generator
        self._clock = clock

    def _restored(self) -> MemoryState | None:
        """Load what a previous process collected, or nothing on a fresh start.

        A document this code cannot read is an error, never an empty start: a
        deployment that silently forgot its collected data would report an
        empty channel as the truth and spend quota collecting it again.
        """

        if self._store is None:
            return None
        document = self._store.load()
        if document is None:
            return None
        state = snapshot.load(document)
        self._document = document
        return state

    def _flush(self) -> None:
        """Write the whole state document once a command has finished.

        ponytail: the document is rewritten in full on every command; move to
        per-collection rows when a workspace holds more than a few channels.

        A write that fails takes its change with it. Without that, memory holds
        rows the document has never heard of, the caller is told the write
        failed, and the next restart quietly reinstates the older truth — the
        one shape of data loss nobody goes looking for. Putting the state back
        to what the store still holds keeps the two readings of the world the
        same, and the error is raised so the caller knows nothing was kept.
        """

        if self._store is None:
            return
        document = snapshot.dump(self._state)
        if document == self._document:
            return
        try:
            self._store.save(document)
        except Exception:
            self._state = (
                snapshot.load(self._document)
                if self._document is not None
                else MemoryState()
            )
            raise
        self._document = document

    def start_collection(
        self,
        context: WorkspaceContext,
        command: StartCollection,
    ) -> CollectionState:
        self._require(context, Permission.COLLECTION_RUN)
        with self._lock:
            replay = self._replay(context, "start_collection", command)
            if replay is not _NO_REPLAY:
                return replay

            collection_key = (context.workspace_id, command.collection_id)
            if collection_key in self._state.collections:
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)

            state = CollectionState(
                collection_id=command.collection_id,
                workspace_id=context.workspace_id,
                channel_id=command.channel_id,
                kind=command.kind,
                status=CollectionStatus.IN_PROGRESS,
                started_at=command.started_at,
                completed_at=None,
                progress_current=0,
                progress_total=0,
                failure_code=None,
                accepted_generation_id=None,
            )
            self._state.collections[collection_key] = state
            self._state.revision += 1
            self._remember(context, "start_collection", command, state, command.started_at)
            return state

    def finish_collection(
        self,
        context: WorkspaceContext,
        command: FinishCollection,
    ) -> CollectionState:
        self._require(context, Permission.COLLECTION_RUN)
        with self._lock:
            replay = self._replay(context, "finish_collection", command)
            if replay is not _NO_REPLAY:
                return replay

            key = (context.workspace_id, command.collection_id)
            current = self._state.collections.get(key)
            if current is None or current.channel_id != command.channel_id:
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)
            if current.status is not CollectionStatus.IN_PROGRESS:
                raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)

            accepted_generation_id = None
            if command.status is CollectionStatus.COMPLETE:
                accepted_generation_id = self._candidate_generation_id(current)
                if accepted_generation_id is None:
                    raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)
                self._validate_candidate_finish(current, command)

            finished = CollectionState(
                collection_id=current.collection_id,
                workspace_id=current.workspace_id,
                channel_id=current.channel_id,
                kind=current.kind,
                status=command.status,
                started_at=current.started_at,
                completed_at=command.completed_at,
                progress_current=command.progress_current,
                progress_total=command.progress_total,
                failure_code=command.failure_code,
                accepted_generation_id=accepted_generation_id,
            )
            if command.status is CollectionStatus.COMPLETE:
                promoted_generation_id = self._promote_candidate(current)
                if promoted_generation_id != accepted_generation_id:
                    raise RuntimeError("candidate generation changed during promotion")
            else:
                self._discard_candidate(current)
            self._state.collections[key] = finished
            self._state.revision += 1
            self._remember(
                context,
                "finish_collection",
                command,
                finished,
                command.completed_at,
            )
            return finished

    def publish_subscriber_snapshot(
        self,
        context: WorkspaceContext,
        command: PublishSubscriberSnapshot,
    ) -> SubscriberSnapshot:
        self._require(context, Permission.COLLECTION_RUN)
        with self._lock:
            replay = self._replay(context, "publish_subscriber_snapshot", command)
            if replay is not _NO_REPLAY:
                return replay
            collection = self._active_collection(
                context,
                command.channel_id,
                command.collection_id,
                CollectionKind.SUBSCRIBERS,
            )
            candidate_key = (context.workspace_id, collection.collection_id)
            if candidate_key in self._state.subscriber_candidates:
                raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)
            if any(
                workspace_id == context.workspace_id
                and snapshot_id == command.snapshot_id
                for workspace_id, _, snapshot_id in self._state.subscriber_snapshots
            ):
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)
            if any(
                workspace_id == context.workspace_id
                and candidate.snapshot.snapshot_id == command.snapshot_id
                for (workspace_id, _), candidate in self._state.subscriber_candidates.items()
            ):
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)

            snapshot = SubscriberSnapshot(
                snapshot_id=command.snapshot_id,
                workspace_id=context.workspace_id,
                channel_id=command.channel_id,
                captured_at=command.captured_at,
                observed_count=len(command.observations),
                traversal_status=command.traversal_status,
                limitations=command.limitations,
            )
            observations = tuple(
                SubscriberObservation(
                    snapshot_id=command.snapshot_id,
                    subscriber_channel_id=row.subscriber_channel_id,
                    title=row.title,
                    api_published_at=row.api_published_at,
                )
                for row in command.observations
            )
            self._state.subscriber_candidates[candidate_key] = SubscriberCandidate(
                snapshot,
                observations,
            )
            self._remember(
                context,
                "publish_subscriber_snapshot",
                command,
                snapshot,
                command.captured_at,
            )
            return snapshot

    def publish_video_inventory(
        self,
        context: WorkspaceContext,
        command: PublishVideoInventory,
    ) -> VideoInventory:
        self._require(context, Permission.COLLECTION_RUN)
        with self._lock:
            replay = self._replay(context, "publish_video_inventory", command)
            if replay is not _NO_REPLAY:
                return replay
            collection = self._active_collection(
                context,
                command.channel_id,
                command.collection_id,
                CollectionKind.VIDEOS,
            )
            candidate_key = (context.workspace_id, collection.collection_id)
            if candidate_key in self._state.video_candidates:
                raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)
            if any(
                workspace_id == context.workspace_id
                and inventory_id == command.inventory_id
                for workspace_id, _, inventory_id in self._state.video_inventories
            ) or any(
                workspace_id == context.workspace_id
                and candidate.inventory.inventory_id == command.inventory_id
                for (workspace_id, _), candidate in self._state.video_candidates.items()
            ):
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)

            inventory = VideoInventory(
                inventory_id=command.inventory_id,
                workspace_id=context.workspace_id,
                channel_id=command.channel_id,
                captured_at=command.captured_at,
                coverage_scope=command.coverage_scope,
                video_count=len(command.videos),
            )
            videos = tuple(
                Video(
                    workspace_id=context.workspace_id,
                    channel_id=command.channel_id,
                    inventory_id=command.inventory_id,
                    video_id=row.video_id,
                    title=row.title,
                    published_at=row.published_at,
                )
                for row in command.videos
            )
            self._state.video_candidates[candidate_key] = VideoCandidate(
                inventory,
                videos,
            )
            self._remember(
                context,
                "publish_video_inventory",
                command,
                inventory,
                command.captured_at,
            )
            return inventory

    def replace_video_comment_activity(
        self,
        context: WorkspaceContext,
        command: ReplaceVideoCommentActivity,
    ) -> CommentCoverage:
        self._require(context, Permission.COLLECTION_RUN)
        with self._lock:
            replay = self._replay(
                context,
                "replace_video_comment_activity",
                command,
            )
            if replay is not _NO_REPLAY:
                return replay
            collection = self._active_collection(
                context,
                command.channel_id,
                command.collection_id,
                CollectionKind.COMMENTS,
            )
            channel_key = (context.workspace_id, command.channel_id)
            if self._state.accepted_video_inventory.get(channel_key) != command.inventory_id:
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)
            inventory_key = (
                context.workspace_id,
                command.channel_id,
                command.inventory_id,
            )
            inventory = self._state.video_inventories[inventory_key]
            videos = self._state.videos[inventory_key]
            if command.video_id not in {video.video_id for video in videos}:
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)

            candidate_key = (context.workspace_id, collection.collection_id)
            candidate = self._state.comment_candidates.get(candidate_key)
            if candidate is None:
                candidate = CommentCandidate(command.inventory_id)
                self._state.comment_candidates[candidate_key] = candidate
            if candidate.inventory_id != command.inventory_id:
                raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)
            if command.video_id in candidate.replacements:
                raise _safe_error(ErrorCode.IDEMPOTENCY_CONFLICT)
            candidate.replacements[command.video_id] = command.activity
            candidate.replaced_at[command.video_id] = command.replaced_at
            coverage = self._comment_candidate_coverage(inventory, candidate)
            self._remember(
                context,
                "replace_video_comment_activity",
                command,
                coverage,
                command.replaced_at,
            )
            return coverage

    def get_freshness(
        self,
        context: WorkspaceContext,
        channel_id: str,
    ) -> ChannelDataFreshness:
        self._require(context, Permission.COLLECTION_READ)
        CollectionHistoryQuery(channel_id)
        with self._lock:
            rows = self._collection_rows(context.workspace_id, channel_id, None)
            return ChannelDataFreshness(
                channel_id=channel_id,
                subscribers=self._freshness(rows, CollectionKind.SUBSCRIBERS),
                videos=self._freshness(rows, CollectionKind.VIDEOS),
                comments=self._freshness(rows, CollectionKind.COMMENTS),
            )

    def list_collection_history(
        self,
        context: WorkspaceContext,
        query: CollectionHistoryQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[CollectionState]:
        self._require(context, Permission.COLLECTION_READ)
        if not isinstance(query, CollectionHistoryQuery) or not isinstance(page, PageRequest):
            raise ChannelDataError(
                ErrorCode.INVALID_INPUT,
                message="Invalid collection history request",
            )
        with self._lock:
            rows = self._collection_rows(
                context.workspace_id,
                query.channel_id,
                query.kind,
            )
            return self._page(
                context.workspace_id,
                query.channel_id,
                (
                    "collection_history",
                    query.channel_id,
                    query.kind.value if query.kind is not None else None,
                    page.limit,
                ),
                tuple(rows),
                page,
            )

    def list_subscriber_registry(
        self,
        context: WorkspaceContext,
        query: SubscriberRegistryQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[SubscriberRegistryEntry]:
        self._require(context, Permission.ANALYSIS_READ)
        if not isinstance(query, SubscriberRegistryQuery) or not isinstance(page, PageRequest):
            raise ChannelDataError(ErrorCode.INVALID_INPUT, message="Invalid registry request")
        with self._lock:
            rows = [
                entry
                for (workspace_id, channel_id, _), entry in self._state.subscriber_registry.items()
                if workspace_id == context.workspace_id and channel_id == query.channel_id
            ]
            rows.sort(key=lambda entry: entry.subscriber_channel_id)
            rows.sort(key=lambda entry: entry.last_seen_at, reverse=True)
            return self._page(
                context.workspace_id,
                query.channel_id,
                ("subscriber_registry", query.channel_id, page.limit),
                tuple(rows),
                page,
            )

    def list_subscriber_snapshots(
        self,
        context: WorkspaceContext,
        query: SubscriberSnapshotQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[SubscriberSnapshot]:
        self._require(context, Permission.ANALYSIS_READ)
        if not isinstance(query, SubscriberSnapshotQuery) or not isinstance(page, PageRequest):
            raise ChannelDataError(ErrorCode.INVALID_INPUT, message="Invalid snapshot request")
        with self._lock:
            rows = [
                snapshot
                for (workspace_id, channel_id, _), snapshot in self._state.subscriber_snapshots.items()
                if workspace_id == context.workspace_id and channel_id == query.channel_id
            ]
            rows.sort(key=lambda snapshot: snapshot.snapshot_id)
            rows.sort(key=lambda snapshot: snapshot.captured_at, reverse=True)
            return self._page(
                context.workspace_id,
                query.channel_id,
                ("subscriber_snapshots", query.channel_id, page.limit),
                tuple(rows),
                page,
            )

    def load_audience_network_viewers(
        self,
        context: WorkspaceContext,
        channel_id: str,
    ) -> tuple[str, ...]:
        """Return the distinct comment-author ids from accepted complete coverage.

        Audience-network collection deliberately does not depend on the owner's
        public-subscriber snapshot. The two datasets answer different questions:
        this panel is comment authors whose own public subscription lists may be
        traversed, while the subscriber snapshot is the owner's public subscriber
        feed and may validly contain zero rows.
        """

        self._require(context, Permission.ANALYSIS_READ)
        SubscriberRegistryQuery(channel_id)
        with self._lock:
            channel_key = (context.workspace_id, channel_id)
            inventory_id = self._state.accepted_video_inventory.get(channel_key)
            if inventory_id is None:
                raise _dataset_not_ready(DatasetReadinessCode.NO_VIDEO_INVENTORY)
            inventory_key = (context.workspace_id, channel_id, inventory_id)
            inventory = self._state.video_inventories[inventory_key]
            if inventory.coverage_scope is not VideoCoverageScope.OWNER_VIDEOS:
                raise _dataset_not_ready(DatasetReadinessCode.PUBLIC_VIDEO_SCOPE_ONLY)
            coverage = self._state.comment_coverage.get(inventory_key)
            if (
                coverage is None
                or coverage.inventory_id != inventory_id
                or coverage.coverage_scope is not VideoCoverageScope.OWNER_VIDEOS
                or not coverage.is_complete
            ):
                raise _dataset_not_ready(DatasetReadinessCode.COMMENTS_INCOMPLETE)
            return tuple(
                sorted({
                    row.author_channel_id
                    for row in self._state.comment_activity.get(inventory_key, ())
                })
            )

    def load_silent_analysis_dataset(
        self,
        context: WorkspaceContext,
        channel_id: str,
    ) -> SilentAnalysisDataset:
        self._require(context, Permission.ANALYSIS_READ)
        SubscriberRegistryQuery(channel_id)
        with self._lock:
            channel_key = (context.workspace_id, channel_id)
            snapshot_id = self._state.accepted_subscriber_snapshot.get(channel_key)
            if snapshot_id is None:
                raise _dataset_not_ready(DatasetReadinessCode.NO_SUBSCRIBER_SNAPSHOT)
            inventory_id = self._state.accepted_video_inventory.get(channel_key)
            if inventory_id is None:
                raise _dataset_not_ready(DatasetReadinessCode.NO_VIDEO_INVENTORY)
            snapshot = self._state.subscriber_snapshots[
                (context.workspace_id, channel_id, snapshot_id)
            ]
            inventory_key = (context.workspace_id, channel_id, inventory_id)
            inventory = self._state.video_inventories[inventory_key]
            if inventory.coverage_scope is not VideoCoverageScope.OWNER_VIDEOS:
                raise _dataset_not_ready(DatasetReadinessCode.PUBLIC_VIDEO_SCOPE_ONLY)
            coverage = self._state.comment_coverage.get(inventory_key)
            if (
                coverage is None
                or coverage.inventory_id != inventory_id
                or coverage.coverage_scope is not VideoCoverageScope.OWNER_VIDEOS
                or not coverage.is_complete
            ):
                raise _dataset_not_ready(DatasetReadinessCode.COMMENTS_INCOMPLETE)

            author_totals: dict[str, tuple[int, datetime]] = {}
            for row in self._state.comment_activity.get(inventory_key, ()):
                count, last = author_totals.get(
                    row.author_channel_id,
                    (0, row.last_comment_at),
                )
                author_totals[row.author_channel_id] = (
                    count + row.comment_count,
                    max(last, row.last_comment_at),
                )
            author_activity = tuple(
                AuthorCommentActivity(author_id, count, last)
                for author_id, (count, last) in sorted(author_totals.items())
            )
            registry = tuple(
                sorted(
                    (
                        entry
                        for (workspace_id, row_channel_id, _), entry
                        in self._state.subscriber_registry.items()
                        if workspace_id == context.workspace_id
                        and row_channel_id == channel_id
                    ),
                    key=lambda entry: entry.subscriber_channel_id,
                )
            )
            return SilentAnalysisDataset(
                subscriber_registry=registry,
                author_activity=author_activity,
                snapshot_id=snapshot.snapshot_id,
                snapshot_captured_at=snapshot.captured_at,
                inventory_id=inventory.inventory_id,
                inventory_captured_at=inventory.captured_at,
                subscriber_limitations=snapshot.limitations,
                comment_coverage=coverage,
            )

    def delete_channel_data(
        self,
        context: WorkspaceContext,
        command: DeleteChannelData,
    ) -> None:
        self._require(context, Permission.CHANNEL_MANAGE_CONNECTION)
        with self._lock:
            replay = self._replay(context, "delete_channel_data", command)
            if replay is not _NO_REPLAY:
                return None
            if not self._state.channel_exists(context.workspace_id, command.channel_id):
                raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)
            self._state.delete_channel(context.workspace_id, command.channel_id)
            self._state.revision += 1
            self._remember(
                context,
                "delete_channel_data",
                command,
                None,
                self._now(),
            )
            return None

    def delete_workspace_data(
        self,
        context: WorkspaceContext,
        command: DeleteWorkspaceData,
    ) -> None:
        self._require(context, Permission.WORKSPACE_DELETE)
        with self._lock:
            replay = self._replay(context, "delete_workspace_data", command)
            if replay is not _NO_REPLAY:
                return None
            self._state.delete_workspace(context.workspace_id)
            self._state.revision += 1
            self._remember(
                context,
                "delete_workspace_data",
                command,
                None,
                self._now(),
            )
            return None

    def purge_retention(
        self,
        context: WorkspaceContext,
        reference_time: datetime,
    ) -> ChannelDataRetentionReport:
        self._require(context, Permission.CHANNEL_MANAGE_CONNECTION)
        if (
            not isinstance(reference_time, datetime)
            or reference_time.tzinfo is None
            or reference_time.utcoffset() is None
        ):
            raise ChannelDataError(
                ErrorCode.INVALID_INPUT,
                message="reference_time must be timezone-aware",
                field="reference_time",
            )
        reference = reference_time.astimezone(UTC)
        with self._lock:
            snapshots_removed, attempts_removed, idempotency_removed = (
                self._state.purge_retention(context.workspace_id, reference)
            )
            if snapshots_removed or attempts_removed or idempotency_removed:
                self._state.revision += 1
            return ChannelDataRetentionReport(
                snapshots_removed=snapshots_removed,
                collection_attempts_removed=attempts_removed,
                idempotency_records_removed=idempotency_removed,
            )

    def _collection_rows(
        self,
        workspace_id: str,
        channel_id: str,
        kind: CollectionKind | None,
    ) -> list[CollectionState]:
        rows = [
            state
            for (row_workspace_id, _), state in self._state.collections.items()
            if row_workspace_id == workspace_id
            and state.channel_id == channel_id
            and (kind is None or state.kind is kind)
        ]
        rows.sort(key=lambda state: state.collection_id)
        rows.sort(key=lambda state: state.started_at, reverse=True)
        return rows

    def _page(
        self,
        workspace_id: str,
        channel_id: str,
        query_key: tuple[object, ...],
        current_items: tuple[object, ...],
        page: PageRequest,
    ) -> Page[Any]:
        items = current_items
        offset = 0
        captured_revision = self._state.revision
        if page.cursor is not None:
            cursor_key = (workspace_id, page.cursor)
            record = self._state.cursors.get(cursor_key)
            if record is None or record.query_key != query_key or record.channel_id != channel_id:
                raise _safe_error(ErrorCode.INVALID_CURSOR)
            del self._state.cursors[cursor_key]
            items = record.items
            offset = record.offset
            captured_revision = record.captured_revision

        end = min(offset + page.limit, len(items))
        next_cursor = None
        if end < len(items):
            next_cursor = self._new_cursor_token(workspace_id)
            self._state.cursors[(workspace_id, next_cursor)] = CursorRecord(
                channel_id=channel_id,
                query_key=query_key,
                items=items,
                offset=end,
                captured_revision=captured_revision,
            )
        return Page(tuple(items[offset:end]), next_cursor)

    def _new_cursor_token(self, workspace_id: str) -> str:
        for _ in range(16):
            if self._token_generator is None:
                token = secrets.token_urlsafe(24)
            else:
                token = self._token_generator.new_token()
            if (
                isinstance(token, str)
                and token
                and len(token) <= 256
                and (workspace_id, token) not in self._state.cursors
            ):
                return token
        raise _safe_error(ErrorCode.OPERATION_IN_PROGRESS, retryable=True)

    def _now(self) -> datetime:
        value = datetime.now(UTC) if self._clock is None else self._clock.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _freshness(
        rows: list[CollectionState],
        kind: CollectionKind,
    ) -> CollectionFreshness:
        matching = [state for state in rows if state.kind is kind]
        accepted = [
            state
            for state in matching
            if state.status is CollectionStatus.COMPLETE
            and state.accepted_generation_id is not None
        ]
        return CollectionFreshness(
            latest_attempt=matching[0] if matching else None,
            latest_accepted_success=accepted[0] if accepted else None,
        )

    def _candidate_generation_id(self, state: CollectionState) -> str | None:
        if state.kind is CollectionKind.SUBSCRIBERS:
            candidate = self._state.subscriber_candidates.get(
                (state.workspace_id, state.collection_id)
            )
            return candidate.snapshot.snapshot_id if candidate is not None else None
        if state.kind is CollectionKind.VIDEOS:
            candidate = self._state.video_candidates.get(
                (state.workspace_id, state.collection_id)
            )
            return candidate.inventory.inventory_id if candidate is not None else None
        if state.kind is CollectionKind.COMMENTS:
            candidate = self._state.comment_candidates.get(
                (state.workspace_id, state.collection_id)
            )
            if candidate is None:
                return None
            inventory = self._state.video_inventories.get(
                (state.workspace_id, state.channel_id, candidate.inventory_id)
            )
            if inventory is None:
                return None
            coverage = self._comment_candidate_coverage(inventory, candidate)
            return candidate.inventory_id if coverage.is_complete else None
        return None

    def _validate_candidate_finish(
        self,
        state: CollectionState,
        command: FinishCollection,
    ) -> None:
        expected = 0
        if state.kind is CollectionKind.SUBSCRIBERS:
            expected = self._state.subscriber_candidates[
                (state.workspace_id, state.collection_id)
            ].snapshot.observed_count
        elif state.kind is CollectionKind.VIDEOS:
            expected = self._state.video_candidates[
                (state.workspace_id, state.collection_id)
            ].inventory.video_count
        elif state.kind is CollectionKind.COMMENTS:
            candidate = self._state.comment_candidates[
                (state.workspace_id, state.collection_id)
            ]
            inventory = self._state.video_inventories[
                (state.workspace_id, state.channel_id, candidate.inventory_id)
            ]
            expected = inventory.video_count
        if command.progress_current != expected or command.progress_total != expected:
            raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)

    def _active_collection(
        self,
        context: WorkspaceContext,
        channel_id: str,
        collection_id: str,
        kind: CollectionKind,
    ) -> CollectionState:
        collection = self._state.collections.get((context.workspace_id, collection_id))
        if collection is None or collection.channel_id != channel_id:
            raise _safe_error(ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN)
        if collection.kind is not kind or collection.status is not CollectionStatus.IN_PROGRESS:
            raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)
        return collection

    def _promote_candidate(self, state: CollectionState) -> str:
        if state.kind is CollectionKind.VIDEOS:
            return self._promote_video_candidate(state)
        if state.kind is CollectionKind.COMMENTS:
            return self._promote_comment_candidate(state)
        if state.kind is not CollectionKind.SUBSCRIBERS:
            raise _safe_error(ErrorCode.INVALID_COLLECTION_TRANSITION)
        key = (state.workspace_id, state.collection_id)
        candidate = self._state.subscriber_candidates[key]
        registry = dict(self._state.subscriber_registry)
        for observation in candidate.observations:
            entry_key = (
                state.workspace_id,
                state.channel_id,
                observation.subscriber_channel_id,
            )
            existing = registry.get(entry_key)
            if existing is None:
                registry[entry_key] = SubscriberRegistryEntry(
                    workspace_id=state.workspace_id,
                    channel_id=state.channel_id,
                    subscriber_channel_id=observation.subscriber_channel_id,
                    title=observation.title,
                    api_published_at=observation.api_published_at,
                    first_seen_at=candidate.snapshot.captured_at,
                    last_seen_at=candidate.snapshot.captured_at,
                    observation_count=1,
                    last_snapshot_id=candidate.snapshot.snapshot_id,
                )
                continue
            is_latest = (
                candidate.snapshot.captured_at,
                candidate.snapshot.snapshot_id,
            ) >= (existing.last_seen_at, existing.last_snapshot_id)
            registry[entry_key] = SubscriberRegistryEntry(
                workspace_id=existing.workspace_id,
                channel_id=existing.channel_id,
                subscriber_channel_id=existing.subscriber_channel_id,
                title=(observation.title or existing.title) if is_latest else existing.title,
                api_published_at=(
                    observation.api_published_at or existing.api_published_at
                    if is_latest
                    else existing.api_published_at
                ),
                first_seen_at=min(existing.first_seen_at, candidate.snapshot.captured_at),
                last_seen_at=max(existing.last_seen_at, candidate.snapshot.captured_at),
                observation_count=existing.observation_count + 1,
                last_snapshot_id=(
                    candidate.snapshot.snapshot_id if is_latest else existing.last_snapshot_id
                ),
            )

        snapshot_key = (
            state.workspace_id,
            state.channel_id,
            candidate.snapshot.snapshot_id,
        )
        self._state.subscriber_registry = registry
        self._state.subscriber_snapshots[snapshot_key] = candidate.snapshot
        self._state.subscriber_observations[snapshot_key] = candidate.observations
        self._state.accepted_subscriber_snapshot[
            (state.workspace_id, state.channel_id)
        ] = candidate.snapshot.snapshot_id
        del self._state.subscriber_candidates[key]
        return candidate.snapshot.snapshot_id

    def _discard_candidate(self, state: CollectionState) -> None:
        if state.kind is CollectionKind.SUBSCRIBERS:
            self._state.subscriber_candidates.pop(
                (state.workspace_id, state.collection_id),
                None,
            )
        elif state.kind is CollectionKind.VIDEOS:
            self._state.video_candidates.pop(
                (state.workspace_id, state.collection_id),
                None,
            )
        elif state.kind is CollectionKind.COMMENTS:
            self._state.comment_candidates.pop(
                (state.workspace_id, state.collection_id),
                None,
            )

    @staticmethod
    def _comment_candidate_coverage(
        inventory: VideoInventory,
        candidate: CommentCandidate,
    ) -> CommentCoverage:
        covered = len(candidate.replacements)
        missing = inventory.video_count - covered
        complete = missing == 0
        completed_at = max(candidate.replaced_at.values()) if complete else None
        return CommentCoverage(
            inventory_id=inventory.inventory_id,
            coverage_scope=inventory.coverage_scope,
            videos_expected=inventory.video_count,
            videos_covered=covered,
            videos_missing=missing,
            completed_at=completed_at,
            is_complete=complete,
        )

    def _promote_video_candidate(self, state: CollectionState) -> str:
        candidate_key = (state.workspace_id, state.collection_id)
        candidate = self._state.video_candidates[candidate_key]
        channel_key = (state.workspace_id, state.channel_id)
        old_inventory_id = self._state.accepted_video_inventory.get(channel_key)
        if old_inventory_id is not None:
            old_key = (state.workspace_id, state.channel_id, old_inventory_id)
            self._state.video_inventories.pop(old_key, None)
            self._state.videos.pop(old_key, None)
            self._state.comment_activity.pop(old_key, None)
            self._state.comment_coverage.pop(old_key, None)
        inventory_key = (
            state.workspace_id,
            state.channel_id,
            candidate.inventory.inventory_id,
        )
        self._state.video_inventories[inventory_key] = candidate.inventory
        self._state.videos[inventory_key] = candidate.videos
        self._state.accepted_video_inventory[channel_key] = candidate.inventory.inventory_id
        if candidate.inventory.video_count == 0:
            self._state.comment_activity[inventory_key] = ()
            self._state.comment_coverage[inventory_key] = CommentCoverage(
                inventory_id=candidate.inventory.inventory_id,
                coverage_scope=candidate.inventory.coverage_scope,
                videos_expected=0,
                videos_covered=0,
                videos_missing=0,
                completed_at=candidate.inventory.captured_at,
                is_complete=True,
            )
        del self._state.video_candidates[candidate_key]
        return candidate.inventory.inventory_id

    def _promote_comment_candidate(self, state: CollectionState) -> str:
        candidate_key = (state.workspace_id, state.collection_id)
        candidate = self._state.comment_candidates[candidate_key]
        inventory_key = (state.workspace_id, state.channel_id, candidate.inventory_id)
        inventory = self._state.video_inventories[inventory_key]
        rows = tuple(
            VideoCommentActivity(
                workspace_id=state.workspace_id,
                channel_id=state.channel_id,
                inventory_id=candidate.inventory_id,
                video_id=video_id,
                author_channel_id=row.author_channel_id,
                comment_count=row.comment_count,
                last_comment_at=row.last_comment_at,
            )
            for video_id in sorted(candidate.replacements)
            for row in sorted(
                candidate.replacements[video_id],
                key=lambda item: item.author_channel_id,
            )
        )
        coverage = self._comment_candidate_coverage(inventory, candidate)
        self._state.comment_activity[inventory_key] = rows
        self._state.comment_coverage[inventory_key] = coverage
        del self._state.comment_candidates[candidate_key]
        return candidate.inventory_id

    @staticmethod
    def _require(context: WorkspaceContext, permission: Permission) -> None:
        if not isinstance(context, WorkspaceContext) or permission not in context.permissions:
            raise _safe_error(ErrorCode.PERMISSION_DENIED)

    def _replay(
        self,
        context: WorkspaceContext,
        operation: str,
        command: object,
    ) -> Any | None:
        key = (context.workspace_id, getattr(command, "idempotency_key"))
        record = self._state.idempotency.get(key)
        if record is None:
            return _NO_REPLAY
        if (
            record.actor_user_id != context.user_id
            or record.operation != operation
            or record.payload_fingerprint != _fingerprint(command)
        ):
            raise _safe_error(ErrorCode.IDEMPOTENCY_CONFLICT)
        return record.result

    def _remember(
        self,
        context: WorkspaceContext,
        operation: str,
        command: object,
        result: object,
        completed_at: datetime,
    ) -> None:
        key = (context.workspace_id, getattr(command, "idempotency_key"))
        self._state.idempotency[key] = IdempotencyRecord(
            actor_user_id=context.user_id,
            channel_id=getattr(command, "channel_id", None),
            operation=operation,
            payload_fingerprint=_fingerprint(command),
            result=result,
            completed_at=completed_at,
        )
