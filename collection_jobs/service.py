"""Tenant-scoped orchestration of collection runs.

Execution is synchronous and caller driven: this module owns no thread, timer,
queue, or worker. Every public method resolves the exact permission on a trusted
`WorkspaceContext`, keys all state under that workspace, and serializes
transitions with one lock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from channel_connections.errors import ChannelConnectionsError
from channel_connections.models import (
    ExecutionAuthority,
    IssueExecutionAuthority,
    ProviderOperation,
    ProviderOperationRequest,
)
from channel_data.models import (
    CollectionKind,
    CollectionFailureCode,
    CollectionStatus,
    CoverageLimitation,
    FinishCollection,
    PublishSubscriberSnapshot,
    StartCollection,
    SubscriberObservationInput,
    SubscriberTraversalStatus,
)
from workspace_access.models import Permission, WorkspaceContext

from .errors import CollectionJobsError, ErrorCode
from .memory import IdempotencyRecord, MemoryState, QuotaLedgerEntry
from .models import (
    ACTIVE_STATUSES,
    CollectionRun,
    DEFAULT_DAILY_QUOTA_UNITS,
    EnqueueRun,
    ExecuteRun,
    MAX_IDENTIFIER_LENGTH,
    RunFailureReason,
    RunKind,
    RunStatus,
)


ESTIMATED_CALL_UNITS = 3
IDEMPOTENCY_TTL = timedelta(days=90)

_MESSAGES = {
    ErrorCode.INVALID_INPUT: "The request contains an unsupported value",
    ErrorCode.PERMISSION_DENIED: "The current role does not allow this operation",
    ErrorCode.RUN_NOT_FOUND_OR_FORBIDDEN: "The run is unavailable",
    ErrorCode.SCHEDULE_NOT_FOUND_OR_FORBIDDEN: "The schedule is unavailable",
    ErrorCode.RUN_ALREADY_ACTIVE: "A run is already active for this channel",
    ErrorCode.INVALID_RUN_TRANSITION: "The run is not in a state that allows this",
    ErrorCode.IDEMPOTENCY_CONFLICT: "The idempotency key was reused with a different request",
    ErrorCode.INVALID_CURSOR: "The page cursor is unavailable",
    ErrorCode.CURSOR_EXPIRED: "The page cursor is no longer current",
}

_SUBSCRIBER_LIMITATIONS = (
    CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
    CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
)


def _safe_error(
    code: ErrorCode,
    *,
    field: str | None = None,
    retryable: bool = False,
    reason_code: str | None = None,
) -> CollectionJobsError:
    return CollectionJobsError(
        code,
        message=_MESSAGES[code],
        field=field,
        retryable=retryable,
        reason_code=reason_code,
    )


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(*parts: str) -> str:
    return "\x1f".join(parts)


def _require(context: WorkspaceContext, permission: Permission) -> None:
    if permission not in context.permissions:
        raise _safe_error(ErrorCode.PERMISSION_DENIED, field="context.permissions")


def _failure_reason(error: ChannelConnectionsError) -> RunFailureReason:
    if error.code in {"CONNECTION_REAUTH_REQUIRED", "AUTHORITY_NOT_FOUND_OR_EXPIRED"}:
        return RunFailureReason.REAUTH_REQUIRED
    if error.retryable:
        return RunFailureReason.PROVIDER_UNAVAILABLE
    return RunFailureReason.UNEXPECTED_FAILURE


_COLLECTION_FAILURE = {
    RunFailureReason.QUOTA_EXHAUSTED: CollectionFailureCode.QUOTA_EXHAUSTED,
    RunFailureReason.PROVIDER_UNAVAILABLE: CollectionFailureCode.PROVIDER_UNAVAILABLE,
    RunFailureReason.REAUTH_REQUIRED: CollectionFailureCode.PROVIDER_UNAVAILABLE,
    RunFailureReason.CANCELLED: CollectionFailureCode.CANCELLED,
    RunFailureReason.UNEXPECTED_FAILURE: CollectionFailureCode.UNEXPECTED_FAILURE,
}


class TraversalOutcome:
    """Rows gathered before a run stopped, with why it stopped."""

    __slots__ = ("rows", "pages", "quota_spent", "reason")

    def __init__(
        self,
        rows: tuple[object, ...],
        pages: int,
        quota_spent: int,
        reason: RunFailureReason | None,
    ) -> None:
        self.rows = rows
        self.pages = pages
        self.quota_spent = quota_spent
        self.reason = reason


class CollectionJobsService:
    """In-memory reference implementation of the collection run lifecycle."""

    def __init__(
        self,
        *,
        clock: Any,
        tokens: Any,
        broker: Any,
        connections: Any,
        channel_data: Any,
        daily_quota_units: int = DEFAULT_DAILY_QUOTA_UNITS,
    ) -> None:
        self._clock = clock
        self._tokens = tokens
        self._broker = broker
        self._connections = connections
        self._channel_data = channel_data
        self._daily_quota_units = daily_quota_units
        self._lock = RLock()
        self._state = MemoryState()

    # Runs

    def enqueue_run(
        self, context: WorkspaceContext, command: EnqueueRun
    ) -> CollectionRun:
        _require(context, Permission.COLLECTION_RUN)
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                "enqueue",
                command.idempotency_key,
            )
            payload = {
                "connection_id": command.connection_id,
                "kind": command.kind.value,
            }
            replayed = self._replay(record_key, payload, now)
            if replayed is not None:
                return replayed

            connection = self._connections.get_connection(context, command.connection_id)
            if self._active_run(context.workspace_id, connection.connection_id, command.kind):
                raise _safe_error(ErrorCode.RUN_ALREADY_ACTIVE, field="kind")

            run = CollectionRun(
                run_id=f"run_{self._tokens.new_token()}",
                workspace_id=context.workspace_id,
                connection_id=connection.connection_id,
                provider_channel_id=connection.provider_channel_id,
                kind=command.kind,
                status=RunStatus.QUEUED,
                attempt=1,
                enqueued_at=now,
                started_at=None,
                finished_at=None,
                pages_fetched=0,
                quota_spent=0,
                failure_reason=None,
                next_attempt_at=None,
            )
            self._store(run)
            self._remember(record_key, payload, run, now)
            return run

    def execute_run(
        self, context: WorkspaceContext, command: ExecuteRun
    ) -> CollectionRun:
        _require(context, Permission.COLLECTION_RUN)
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                "execute",
                command.idempotency_key,
            )
            payload = {"run_id": command.run_id}
            replayed = self._replay(record_key, payload, now)
            if replayed is not None:
                return replayed

            run = self._run(context.workspace_id, command.run_id)
            if run.status is not RunStatus.QUEUED:
                raise _safe_error(ErrorCode.INVALID_RUN_TRANSITION, field="run_id")
            if run.next_attempt_at is not None and now < run.next_attempt_at:
                raise _safe_error(
                    ErrorCode.INVALID_RUN_TRANSITION, field="run_id", retryable=True
                )

            running = replace(run, status=RunStatus.RUNNING, started_at=now)
            self._store(running)
            finished = self._execute(context, running, now)
            self._store(finished)
            self._remember(record_key, payload, finished, now)
            return finished

    # Internal execution

    def _execute(
        self, context: WorkspaceContext, run: CollectionRun, now: datetime
    ) -> CollectionRun:
        try:
            authority = self._broker.issue_execution_authority(
                context, IssueExecutionAuthority(connection_id=run.connection_id)
            )
        except ChannelConnectionsError as error:
            return self._terminal(run, now, _failure_reason(error), pages=0, spent=0)

        if run.kind is RunKind.SUBSCRIBERS:
            return self._run_subscribers(context, run, authority, now)
        raise _safe_error(ErrorCode.INVALID_INPUT, field="kind")

    def _run_subscribers(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
    ) -> CollectionRun:
        collection_id = f"{run.run_id}-subscribers"
        self._channel_data.start_collection(
            context,
            StartCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                kind=CollectionKind.SUBSCRIBERS,
                started_at=now,
                idempotency_key=f"{collection_id}-start",
            ),
        )
        outcome = self._traverse(
            context, authority, ProviderOperation.LIST_SUBSCRIBERS, None
        )
        if outcome.reason is not None:
            self._finish_collection(context, run, collection_id, now, outcome)
            return self._terminal(
                run, now, outcome.reason, pages=outcome.pages, spent=outcome.quota_spent
            )

        observations = tuple(
            SubscriberObservationInput(
                subscriber_channel_id=row.subscriber_channel_id,
                title=row.title,
                api_published_at=row.api_published_at,
            )
            for row in outcome.rows
        )
        self._channel_data.publish_subscriber_snapshot(
            context,
            PublishSubscriberSnapshot(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                snapshot_id=f"{collection_id}-snapshot",
                captured_at=now,
                traversal_status=SubscriberTraversalStatus.COMPLETE,
                limitations=_SUBSCRIBER_LIMITATIONS,
                observations=observations,
                idempotency_key=f"{collection_id}-snapshot",
            ),
        )
        self._finish_collection(context, run, collection_id, now, outcome)
        return self._terminal(
            run, now, None, pages=outcome.pages, spent=outcome.quota_spent
        )

    def _traverse(
        self,
        context: WorkspaceContext,
        authority: ExecutionAuthority,
        operation: ProviderOperation,
        video_id: str | None,
    ) -> TraversalOutcome:
        rows: list[object] = []
        pages = 0
        spent = 0
        page_token: str | None = None
        while True:
            if self._quota_remaining(context.workspace_id) < ESTIMATED_CALL_UNITS:
                return TraversalOutcome(
                    tuple(rows), pages, spent, RunFailureReason.QUOTA_EXHAUSTED
                )
            try:
                result = self._broker.run_provider_operation(
                    authority,
                    ProviderOperationRequest(
                        operation=operation, page_token=page_token, video_id=video_id
                    ),
                )
            except ChannelConnectionsError as error:
                return TraversalOutcome(
                    tuple(rows), pages, spent, _failure_reason(error)
                )
            except BaseException:
                return TraversalOutcome(
                    tuple(rows), pages, spent, RunFailureReason.UNEXPECTED_FAILURE
                )

            self._spend(context.workspace_id, result.quota_cost)
            rows.extend(result.rows)
            pages += 1
            spent += result.quota_cost
            page_token = result.next_page_token
            if page_token is None:
                return TraversalOutcome(tuple(rows), pages, spent, None)

    def _finish_collection(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        collection_id: str,
        now: datetime,
        outcome: TraversalOutcome,
    ) -> None:
        succeeded = outcome.reason is None
        status = CollectionStatus.COMPLETE if succeeded else CollectionStatus.PARTIAL
        if outcome.reason in {
            RunFailureReason.REAUTH_REQUIRED,
            RunFailureReason.UNEXPECTED_FAILURE,
        }:
            status = CollectionStatus.FAILED
        self._channel_data.finish_collection(
            context,
            FinishCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                status=status,
                completed_at=now,
                progress_current=len(outcome.rows),
                progress_total=len(outcome.rows),
                failure_code=(
                    None if succeeded else _COLLECTION_FAILURE[outcome.reason]
                ),
                idempotency_key=f"{collection_id}-finish",
            ),
        )

    def _terminal(
        self,
        run: CollectionRun,
        now: datetime,
        reason: RunFailureReason | None,
        *,
        pages: int,
        spent: int,
    ) -> CollectionRun:
        if reason is None:
            status = RunStatus.SUCCEEDED
        elif reason is RunFailureReason.QUOTA_EXHAUSTED:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.FAILED
        return replace(
            run,
            status=status,
            finished_at=now,
            failure_reason=reason,
            pages_fetched=pages,
            quota_spent=spent,
            next_attempt_at=None,
        )

    # Quota

    def _quota_key(self, workspace_id: str) -> tuple[str, str]:
        return (workspace_id, self._now().date().isoformat())

    def _quota_remaining(self, workspace_id: str) -> int:
        entry = self._state.quota.get(self._quota_key(workspace_id))
        used = 0 if entry is None else entry.used_units
        return self._daily_quota_units - used

    def _spend(self, workspace_id: str, units: int) -> None:
        key = self._quota_key(workspace_id)
        entry = self._state.quota.get(key)
        if entry is None:
            self._state.quota[key] = QuotaLedgerEntry(
                used_units=units, first_used_at=self._now()
            )
            return
        entry.used_units += units

    # State helpers

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise _safe_error(ErrorCode.INVALID_INPUT, field="clock")
        return now

    def _store(self, run: CollectionRun) -> None:
        self._state.runs[_key(run.workspace_id, run.run_id)] = run
        self._state.revisions[run.workspace_id] = (
            self._state.revisions.get(run.workspace_id, 0) + 1
        )

    def _run(self, workspace_id: str, run_id: object) -> CollectionRun:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > MAX_IDENTIFIER_LENGTH
        ):
            raise _safe_error(ErrorCode.RUN_NOT_FOUND_OR_FORBIDDEN)
        run = self._state.runs.get(_key(workspace_id, run_id))
        if run is None:
            raise _safe_error(ErrorCode.RUN_NOT_FOUND_OR_FORBIDDEN)
        return run

    def _active_run(
        self, workspace_id: str, connection_id: str, kind: RunKind
    ) -> CollectionRun | None:
        for run in self._state.runs.values():
            if (
                run.workspace_id == workspace_id
                and run.connection_id == connection_id
                and run.kind is kind
                and run.status in ACTIVE_STATUSES
            ):
                return run
        return None

    def _replay(
        self,
        record_key: tuple[str, str, str, str],
        payload: dict[str, Any],
        now: datetime,
    ) -> Any:
        record = self._state.idempotency.get(record_key)
        if record is None:
            return None
        if record.fingerprint != _fingerprint(payload):
            raise _safe_error(ErrorCode.IDEMPOTENCY_CONFLICT, field="idempotency_key")
        if record.expires_at <= now:
            del self._state.idempotency[record_key]
            return None
        return record.result

    def _remember(
        self,
        record_key: tuple[str, str, str, str],
        payload: dict[str, Any],
        result: Any,
        now: datetime,
    ) -> None:
        self._state.idempotency[record_key] = IdempotencyRecord(
            fingerprint=_fingerprint(payload),
            result=result,
            recorded_at=now,
            expires_at=now + IDEMPOTENCY_TTL,
        )
