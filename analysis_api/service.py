"""Workspace-authorized analysis orchestration.

This module resolves permissions, loads accepted `channel-data` generations, and
delegates every segment and filter rule to `analytics-core`. It computes no
analytics rule of its own and never contacts a provider or a job.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any, Protocol

from channel_data.errors import ChannelDataError
from subscriber_analytics.analytics_core import (
    AnalysisRequest,
    CommentActivity,
    SubscriberRecord,
    analyze,
)
from workspace_access.models import Permission, WorkspaceContext

from .errors import AnalysisApiError, ErrorCode
from .models import (
    AnalysisFilterInput,
    AnalysisPage,
    AnalysisPageRequest,
    AnalysisRowView,
    AnalysisSummary,
    CompareChannels,
    ComparisonEntry,
    ComparisonResult,
    DeleteView,
    ExportAnalysis,
    ExportDocument,
    LimitationCode,
    MAX_IDENTIFIER_LENGTH,
    RunAnalysis,
    SaveView,
    SavedView,
)
from .snapshot import dump_views, load_views


class StateStore(Protocol):
    def load(self) -> str | None: ...

    def save(self, document: str) -> None: ...


EXPORT_COLUMNS = (
    "subscriber_channel_id",
    "title",
    "subscribed_at",
    "subscribed_at_source",
    "segment",
    "comment_count",
    "last_comment_at",
)

_MESSAGES = {
    ErrorCode.INVALID_INPUT: "The request contains an unsupported value",
    ErrorCode.PERMISSION_DENIED: "The current role does not allow this operation",
    ErrorCode.VIEW_NOT_FOUND_OR_FORBIDDEN: "The saved view is unavailable",
    ErrorCode.DATASET_NOT_READY: "The analysis data is not ready yet",
    ErrorCode.IDEMPOTENCY_CONFLICT: "The idempotency key was reused with a different request",
    ErrorCode.INVALID_CURSOR: "The page cursor is unavailable",
    ErrorCode.CURSOR_EXPIRED: "The page cursor is no longer current",
}


def _safe_error(
    code: ErrorCode,
    *,
    field_name: str | None = None,
    reason_code: str | None = None,
) -> AnalysisApiError:
    return AnalysisApiError(
        code, message=_MESSAGES[code], field=field_name, reason_code=reason_code
    )


def _require(context: WorkspaceContext, permission: Permission) -> None:
    if permission not in context.permissions:
        raise _safe_error(ErrorCode.PERMISSION_DENIED, field_name="context.permissions")


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(*parts: str) -> str:
    return "\x1f".join(parts)


def _filter_payload(filters: AnalysisFilterInput) -> dict[str, Any]:
    return {
        "subscribed_within_days": filters.subscribed_within_days,
        "subscribed_since": filters.subscribed_since,
        "subscribed_until": filters.subscribed_until,
        "never_commented": filters.never_commented,
        "no_comment_within_days": filters.no_comment_within_days,
        "include_not_seen_latest": filters.include_not_seen_latest,
        "segments": sorted(filters.segments),
    }


@dataclass(frozen=True, slots=True)
class _CursorRecord:
    workspace_id: str
    query_fingerprint: str
    generation: str
    offset: int


@dataclass(slots=True)
class _IdempotencyRecord:
    fingerprint: str
    result: object


@dataclass(slots=True)
class _State:
    views: dict[str, SavedView] = field(default_factory=dict)
    cursors: dict[str, _CursorRecord] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str, str], _IdempotencyRecord] = field(
        default_factory=dict
    )


class AnalysisApiService:
    """In-memory reference implementation of the analysis surface."""

    def __init__(
        self,
        *,
        clock: Any,
        tokens: Any,
        channel_data: Any,
        state_store: StateStore | None = None,
    ) -> None:
        self._clock = clock
        self._tokens = tokens
        self._channel_data = channel_data
        self._lock = RLock()
        self._state = _State()
        self._state_store = state_store
        if state_store is not None:
            document = state_store.load()
            if document is not None:
                self._state.views = {
                    _key(view.workspace_id, view.view_id): view
                    for view in load_views(document)
                }

    def _flush_views(self) -> None:
        if self._state_store is not None:
            self._state_store.save(dump_views(self._state.views.values()))

    # Analysis

    def run_analysis(
        self,
        context: WorkspaceContext,
        command: RunAnalysis,
        page: AnalysisPageRequest = AnalysisPageRequest(),
    ) -> AnalysisPage:
        _require(context, Permission.ANALYSIS_READ)
        with self._lock:
            now = self._now()
            dataset = self._dataset(context, command.channel_id)
            result = self._analyze(dataset, command.filters, now)
            generation = f"{dataset.snapshot_id}:{dataset.inventory_id}"
            query = _fingerprint(
                {
                    "channel_id": command.channel_id,
                    "limit": page.limit,
                    **_filter_payload(command.filters),
                }
            )

            offset = 0
            if page.cursor is not None:
                record = self._state.cursors.get(page.cursor)
                if (
                    record is None
                    or record.workspace_id != context.workspace_id
                    or record.query_fingerprint != query
                ):
                    raise _safe_error(ErrorCode.INVALID_CURSOR, field_name="cursor")
                if record.generation != generation:
                    raise _safe_error(ErrorCode.CURSOR_EXPIRED, field_name="cursor")
                offset = record.offset
                del self._state.cursors[page.cursor]

            rows = tuple(
                AnalysisRowView(
                    subscriber_channel_id=row.channel_id,
                    title=row.title,
                    subscribed_at=row.subscribed_at,
                    subscribed_at_source=row.subscribed_at_source,
                    segment=row.segment.value,
                    comment_count=row.comment_count,
                    last_comment_at=row.last_comment_at,
                )
                for row in result.rows[offset : offset + page.limit]
            )
            next_cursor = None
            if offset + page.limit < len(result.rows):
                next_cursor = f"cursor_{self._tokens.new_token()}"
                self._state.cursors[next_cursor] = _CursorRecord(
                    workspace_id=context.workspace_id,
                    query_fingerprint=query,
                    generation=generation,
                    offset=offset + page.limit,
                )
            return AnalysisPage(
                summary=self._summary(command.channel_id, dataset, result, now),
                rows=rows,
                next_cursor=next_cursor,
            )

    def compare_channels(
        self, context: WorkspaceContext, command: CompareChannels
    ) -> ComparisonResult:
        _require(context, Permission.ANALYSIS_READ)
        with self._lock:
            now = self._now()
            entries = []
            for channel_id in command.channel_ids:
                try:
                    dataset = self._dataset(context, channel_id)
                except AnalysisApiError as error:
                    if error.code != ErrorCode.DATASET_NOT_READY.value:
                        raise
                    entries.append(
                        ComparisonEntry(
                            channel_id=channel_id,
                            summary=None,
                            not_ready_reason=error.reason_code or "NOT_READY",
                        )
                    )
                    continue
                result = self._analyze(dataset, command.filters, now)
                entries.append(
                    ComparisonEntry(
                        channel_id=channel_id,
                        summary=self._summary(channel_id, dataset, result, now),
                        not_ready_reason=None,
                    )
                )
            return ComparisonResult(entries=tuple(entries), reference_time=now)

    def export_analysis(
        self, context: WorkspaceContext, command: ExportAnalysis
    ) -> ExportDocument:
        _require(context, Permission.ANALYSIS_EXPORT)
        with self._lock:
            now = self._now()
            dataset = self._dataset(context, command.channel_id)
            result = self._analyze(dataset, command.filters, now)

            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(EXPORT_COLUMNS)
            for row in result.rows:
                writer.writerow(
                    (
                        row.channel_id,
                        row.title,
                        row.subscribed_at.isoformat(),
                        row.subscribed_at_source.value,
                        row.segment.value,
                        row.comment_count,
                        "" if row.last_comment_at is None else row.last_comment_at.isoformat(),
                    )
                )
            stamp = now.strftime("%Y%m%dT%H%M%SZ")
            return ExportDocument(
                filename=f"analysis_{command.channel_id}_{stamp}.csv",
                content_type="text/csv;charset=utf-8",
                content=b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"),
                row_count=len(result.rows),
            )

    # Views

    def save_view(self, context: WorkspaceContext, command: SaveView) -> SavedView:
        _require(context, Permission.ANALYSIS_READ)
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                "save_view",
                command.idempotency_key,
            )
            payload = {
                "name": command.name,
                "channel_id": command.channel_id,
                **_filter_payload(command.filters),
            }
            replayed = self._replay(record_key, payload)
            if replayed is not None:
                return replayed

            view = SavedView(
                view_id=f"view_{self._tokens.new_token()}",
                workspace_id=context.workspace_id,
                name=command.name,
                channel_id=command.channel_id,
                filters=command.filters,
                created_at=now,
                updated_at=now,
            )
            view_key = _key(context.workspace_id, view.view_id)
            self._state.views[view_key] = view
            try:
                self._flush_views()
            except Exception:
                del self._state.views[view_key]
                raise
            self._remember(record_key, payload, view)
            return view

    def list_views(self, context: WorkspaceContext) -> tuple[SavedView, ...]:
        _require(context, Permission.ANALYSIS_READ)
        with self._lock:
            views = [
                view
                for view in self._state.views.values()
                if view.workspace_id == context.workspace_id
            ]
            views.sort(key=lambda view: (view.name, view.view_id))
            return tuple(views)

    def get_view(self, context: WorkspaceContext, view_id: str) -> SavedView:
        _require(context, Permission.ANALYSIS_READ)
        with self._lock:
            return self._view(context.workspace_id, view_id)

    def delete_view(self, context: WorkspaceContext, command: DeleteView) -> None:
        _require(context, Permission.ANALYSIS_READ)
        with self._lock:
            record_key = (
                context.workspace_id,
                context.user_id,
                "delete_view",
                command.idempotency_key,
            )
            payload = {"view_id": command.view_id}
            if self._replay(record_key, payload) is not None:
                return None
            view = self._view(context.workspace_id, command.view_id)
            view_key = _key(context.workspace_id, view.view_id)
            del self._state.views[view_key]
            try:
                self._flush_views()
            except Exception:
                self._state.views[view_key] = view
                raise
            self._remember(record_key, payload, view.view_id)
            return None

    def delete_workspace_views(self, context: WorkspaceContext) -> None:
        _require(context, Permission.WORKSPACE_DELETE)
        with self._lock:
            previous_views = self._state.views
            self._state.views = {
                key: view
                for key, view in self._state.views.items()
                if view.workspace_id != context.workspace_id
            }
            try:
                self._flush_views()
            except Exception:
                self._state.views = previous_views
                raise
            self._state.cursors = {
                token: record
                for token, record in self._state.cursors.items()
                if record.workspace_id != context.workspace_id
            }
            return None

    # Internals

    def _dataset(self, context: WorkspaceContext, channel_id: str) -> Any:
        try:
            return self._channel_data.load_silent_analysis_dataset(context, channel_id)
        except ChannelDataError as error:
            if error.code == "DATASET_NOT_READY":
                raise _safe_error(
                    ErrorCode.DATASET_NOT_READY, reason_code=error.reason_code
                ) from None
            if error.code == "PERMISSION_DENIED":
                raise _safe_error(ErrorCode.PERMISSION_DENIED) from None
            raise _safe_error(
                ErrorCode.DATASET_NOT_READY, reason_code="UNAVAILABLE"
            ) from None

    def _analyze(
        self, dataset: Any, filters: AnalysisFilterInput, now: datetime
    ) -> Any:
        return analyze(
            tuple(
                SubscriberRecord(
                    channel_id=entry.subscriber_channel_id,
                    title=entry.title,
                    api_published_at=entry.api_published_at,
                    first_seen_at=entry.first_seen_at,
                    last_seen_at=entry.last_seen_at,
                )
                for entry in dataset.subscriber_registry
            ),
            tuple(
                CommentActivity(
                    author_channel_id=row.author_channel_id,
                    comment_count=row.comment_count,
                    last_comment_at=row.last_comment_at,
                )
                for row in dataset.author_activity
            ),
            AnalysisRequest(reference_time=now, filters=filters.to_core()),
        )

    def _summary(
        self, channel_id: str, dataset: Any, result: Any, now: datetime
    ) -> AnalysisSummary:
        return AnalysisSummary(
            channel_id=channel_id,
            reference_time=now,
            scope_count=result.scope_count,
            filtered_count=result.filtered_count,
            scope_silent_count=result.scope_silent_count,
            filtered_silent_count=result.filtered_silent_count,
            segment_counts=tuple(
                (segment.value, count)
                for segment, count in sorted(
                    result.filtered_segment_counts.items(), key=lambda item: item[0].value
                )
            ),
            limitations=(
                LimitationCode.PUBLIC_SUBSCRIPTIONS_ONLY,
                LimitationCode.PROVIDER_RESULT_CAP_POSSIBLE,
            ),
            snapshot_id=dataset.snapshot_id,
            inventory_id=dataset.inventory_id,
        )

    def _view(self, workspace_id: str, view_id: object) -> SavedView:
        if (
            not isinstance(view_id, str)
            or not view_id
            or len(view_id) > MAX_IDENTIFIER_LENGTH
        ):
            raise _safe_error(ErrorCode.VIEW_NOT_FOUND_OR_FORBIDDEN)
        view = self._state.views.get(_key(workspace_id, view_id))
        if view is None:
            raise _safe_error(ErrorCode.VIEW_NOT_FOUND_OR_FORBIDDEN)
        return view

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise _safe_error(ErrorCode.INVALID_INPUT, field_name="clock")
        return now

    def _replay(
        self, record_key: tuple[str, str, str, str], payload: dict[str, Any]
    ) -> Any:
        record = self._state.idempotency.get(record_key)
        if record is None:
            return None
        if record.fingerprint != _fingerprint(payload):
            raise _safe_error(
                ErrorCode.IDEMPOTENCY_CONFLICT, field_name="idempotency_key"
            )
        return record.result

    def _remember(
        self,
        record_key: tuple[str, str, str, str],
        payload: dict[str, Any],
        result: Any,
    ) -> None:
        self._state.idempotency[record_key] = _IdempotencyRecord(
            fingerprint=_fingerprint(payload), result=result
        )
