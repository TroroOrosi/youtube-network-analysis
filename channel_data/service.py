"""Authorized in-memory facade for tenant-scoped channel data."""

from __future__ import annotations

import hashlib
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from typing import Any

from workspace_access.models import Permission, WorkspaceContext

from .errors import ChannelDataError, ErrorCode
from .memory import IdempotencyRecord, MemoryState
from .models import (
    ChannelDataFreshness,
    CollectionFreshness,
    CollectionHistoryQuery,
    CollectionKind,
    CollectionState,
    CollectionStatus,
    FinishCollection,
    Page,
    PageRequest,
    StartCollection,
)


def _safe_error(code: ErrorCode, *, retryable: bool = False) -> ChannelDataError:
    messages = {
        ErrorCode.PERMISSION_DENIED: "Permission denied",
        ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN: "Resource not found or forbidden",
        ErrorCode.IDEMPOTENCY_CONFLICT: "Idempotency key conflicts with another operation",
        ErrorCode.OPERATION_IN_PROGRESS: "Operation is already in progress",
        ErrorCode.INVALID_COLLECTION_TRANSITION: "Invalid collection transition",
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


class ChannelDataService:
    """Standard-library reference implementation with one atomic lock."""

    def __init__(self, state: MemoryState | None = None) -> None:
        self._state = state or MemoryState()
        self._lock = RLock()

    def start_collection(
        self,
        context: WorkspaceContext,
        command: StartCollection,
    ) -> CollectionState:
        self._require(context, Permission.COLLECTION_RUN)
        with self._lock:
            replay = self._replay(context, "start_collection", command)
            if replay is not None:
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
            if replay is not None:
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
            self._state.collections[key] = finished
            self._remember(
                context,
                "finish_collection",
                command,
                finished,
                command.completed_at,
            )
            return finished

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
            return Page(tuple(rows[: page.limit]), None)

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
        # Candidate stores are introduced by the subscriber/video slices.
        return None

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
            return None
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
            operation=operation,
            payload_fingerprint=_fingerprint(command),
            result=result,
            completed_at=completed_at,
        )
