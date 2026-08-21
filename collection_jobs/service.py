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
from types import TracebackType
from typing import Any, Callable

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
    PublishVideoInventory,
    ReplaceVideoCommentActivity,
    StartCollection,
    SubscriberObservationInput,
    SubscriberTraversalStatus,
    VideoCommentActivityInput,
    VideoCoverageScope,
    VideoInput,
)
from workspace_access.models import Permission, WorkspaceContext

from . import snapshot
from .errors import CollectionJobsError, ErrorCode
from .memory import CursorRecord, IdempotencyRecord, MemoryState, QuotaLedgerEntry
from .models import (
    ACTIVE_STATUSES,
    BACKOFF_SCHEDULE,
    CancelRun,
    CollectionRun,
    CollectionSchedule,
    CreateSchedule,
    DEFAULT_DAILY_QUOTA_UNITS,
    DeleteSchedule,
    DeleteWorkspaceJobs,
    EnqueueRun,
    ExecuteRun,
    JobsPage,
    JobsPageRequest,
    JobsRetentionReport,
    MAX_ATTEMPTS,
    MAX_IDENTIFIER_LENGTH,
    RunFailureReason,
    RunKind,
    RunQuery,
    RunStatus,
)
from .ports import StateStore


ESTIMATED_CALL_UNITS = 3
IDEMPOTENCY_TTL = timedelta(days=90)
RETENTION_TTL = timedelta(days=90)

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
        page_size: int = 50,
        state_store: StateStore | None = None,
    ) -> None:
        self._clock = clock
        self._tokens = tokens
        self._broker = broker
        self._connections = connections
        self._channel_data = channel_data
        self._daily_quota_units = daily_quota_units
        self._page_size = page_size
        self._state_store = state_store
        self._document: str | None = None
        self._state = self._restored() or MemoryState()
        self._lock = _StateLock(self._flush)

    def _restored(self) -> MemoryState | None:
        """Load what a previous process queued, or nothing on a fresh start.

        A document this code cannot read is an error, never an empty start: a
        deployment that silently forgot its runs would re-enqueue work that
        already spent provider quota, and forget the ledger saying so.
        """

        if self._state_store is None:
            return None
        document = self._state_store.load()
        if document is None:
            return None
        state = snapshot.load(document)
        self._document = document
        return state

    def _flush(self) -> None:
        """Write the whole state document once a command has finished.

        ponytail: the document is rewritten in full on every command; move to
        per-run rows when a workspace keeps more than a few thousand runs.
        """

        if self._state_store is None:
            return
        document = snapshot.dump(self._state)
        if document == self._document:
            return
        self._state_store.save(document)
        self._document = document

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

    def cancel_run(
        self, context: WorkspaceContext, command: CancelRun
    ) -> CollectionRun:
        _require(context, Permission.COLLECTION_RUN)
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                "cancel",
                command.idempotency_key,
            )
            payload = {"run_id": command.run_id}
            replayed = self._replay(record_key, payload, now)
            if replayed is not None:
                return replayed

            run = self._run(context.workspace_id, command.run_id)
            if run.status not in ACTIVE_STATUSES:
                raise _safe_error(ErrorCode.INVALID_RUN_TRANSITION, field="run_id")
            cancelled = replace(
                run,
                status=RunStatus.CANCELLED,
                finished_at=now,
                failure_reason=RunFailureReason.CANCELLED,
                next_attempt_at=None,
            )
            self._store(cancelled)
            self._remember(record_key, payload, cancelled, now)
            return cancelled

    # Privacy administration

    def delete_workspace_jobs(
        self, context: WorkspaceContext, command: DeleteWorkspaceJobs
    ) -> None:
        _require(context, Permission.WORKSPACE_DELETE)
        with self._lock:
            now = self._now()
            workspace_id = context.workspace_id
            record_key = (
                workspace_id,
                context.user_id,
                "delete_workspace_jobs",
                command.idempotency_key,
            )
            payload = {"workspace_id": workspace_id}
            if self._replay(record_key, payload, now) is not None:
                return None

            self._state.runs = {
                key: run
                for key, run in self._state.runs.items()
                if run.workspace_id != workspace_id
            }
            self._state.schedules = {
                key: schedule
                for key, schedule in self._state.schedules.items()
                if schedule.workspace_id != workspace_id
            }
            self._state.quota = {
                key: entry
                for key, entry in self._state.quota.items()
                if key[0] != workspace_id
            }
            self._state.cursors = {
                token: record
                for token, record in self._state.cursors.items()
                if record.workspace_id != workspace_id
            }
            self._state.idempotency = {
                key: record
                for key, record in self._state.idempotency.items()
                if key[0] != workspace_id
            }
            self._bump_revision(workspace_id)
            self._remember(record_key, payload, command.idempotency_key, now)
            return None

    def purge_retention(
        self, context: WorkspaceContext, reference_time: datetime
    ) -> JobsRetentionReport:
        _require(context, Permission.COLLECTION_RUN)
        if (
            not isinstance(reference_time, datetime)
            or reference_time.utcoffset() is None
        ):
            raise _safe_error(ErrorCode.INVALID_INPUT, field="reference_time")
        with self._lock:
            workspace_id = context.workspace_id
            runs_removed = 0
            for key in [
                key
                for key, run in self._state.runs.items()
                if run.workspace_id == workspace_id
                and run.finished_at is not None
                and run.finished_at + RETENTION_TTL <= reference_time
            ]:
                del self._state.runs[key]
                runs_removed += 1

            quota_removed = 0
            for key in [
                key
                for key, entry in self._state.quota.items()
                if key[0] == workspace_id
                and entry.first_used_at + RETENTION_TTL <= reference_time
            ]:
                del self._state.quota[key]
                quota_removed += 1

            records_removed = 0
            for key in [
                key
                for key, record in self._state.idempotency.items()
                if key[0] == workspace_id and record.expires_at <= reference_time
            ]:
                del self._state.idempotency[key]
                records_removed += 1

            if runs_removed or quota_removed:
                self._bump_revision(workspace_id)
            return JobsRetentionReport(
                runs_removed=runs_removed,
                quota_entries_removed=quota_removed,
                idempotency_records_removed=records_removed,
            )

    # Schedules

    def create_schedule(
        self, context: WorkspaceContext, command: CreateSchedule
    ) -> CollectionSchedule:
        _require(context, Permission.COLLECTION_RUN)
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                "create_schedule",
                command.idempotency_key,
            )
            payload = {
                "connection_id": command.connection_id,
                "kind": command.kind.value,
                "interval": command.interval.total_seconds(),
            }
            replayed = self._replay(record_key, payload, now)
            if replayed is not None:
                return replayed

            connection = self._connections.get_connection(context, command.connection_id)
            schedule = CollectionSchedule(
                schedule_id=f"schedule_{self._tokens.new_token()}",
                workspace_id=context.workspace_id,
                connection_id=connection.connection_id,
                kind=command.kind,
                interval=command.interval,
                enabled=True,
                created_at=now,
                last_enqueued_at=None,
            )
            self._state.schedules[
                _key(context.workspace_id, schedule.schedule_id)
            ] = schedule
            self._bump_revision(context.workspace_id)
            self._remember(record_key, payload, schedule, now)
            return schedule

    def delete_schedule(
        self, context: WorkspaceContext, command: DeleteSchedule
    ) -> None:
        _require(context, Permission.COLLECTION_RUN)
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                "delete_schedule",
                command.idempotency_key,
            )
            payload = {"schedule_id": command.schedule_id}
            if self._replay(record_key, payload, now) is not None:
                return None

            schedule_key = _key(context.workspace_id, command.schedule_id)
            if schedule_key not in self._state.schedules:
                raise _safe_error(ErrorCode.SCHEDULE_NOT_FOUND_OR_FORBIDDEN)
            del self._state.schedules[schedule_key]
            self._bump_revision(context.workspace_id)
            self._remember(record_key, payload, command.schedule_id, now)
            return None

    def enqueue_due_runs(
        self, context: WorkspaceContext, reference_time: datetime
    ) -> tuple[CollectionRun, ...]:
        _require(context, Permission.COLLECTION_RUN)
        if (
            not isinstance(reference_time, datetime)
            or reference_time.utcoffset() is None
        ):
            raise _safe_error(ErrorCode.INVALID_INPUT, field="reference_time")
        with self._lock:
            enqueued: list[CollectionRun] = []
            for schedule_key, schedule in sorted(self._state.schedules.items()):
                if schedule.workspace_id != context.workspace_id or not schedule.enabled:
                    continue
                if (
                    schedule.last_enqueued_at is not None
                    and reference_time < schedule.last_enqueued_at + schedule.interval
                ):
                    continue
                if self._active_run(
                    context.workspace_id, schedule.connection_id, schedule.kind
                ):
                    continue
                connection = self._connections.get_connection(
                    context, schedule.connection_id
                )
                run = CollectionRun(
                    run_id=f"run_{self._tokens.new_token()}",
                    workspace_id=context.workspace_id,
                    connection_id=connection.connection_id,
                    provider_channel_id=connection.provider_channel_id,
                    kind=schedule.kind,
                    status=RunStatus.QUEUED,
                    attempt=1,
                    enqueued_at=reference_time,
                    started_at=None,
                    finished_at=None,
                    pages_fetched=0,
                    quota_spent=0,
                    failure_reason=None,
                    next_attempt_at=None,
                )
                self._store(run)
                self._state.schedules[schedule_key] = replace(
                    schedule, last_enqueued_at=reference_time
                )
                enqueued.append(run)
            return tuple(enqueued)

    # Reads

    def get_run(self, context: WorkspaceContext, run_id: str) -> CollectionRun:
        _require(context, Permission.COLLECTION_READ)
        with self._lock:
            return self._run(context.workspace_id, run_id)

    def list_runs(
        self,
        context: WorkspaceContext,
        query: RunQuery = RunQuery(),
        page: JobsPageRequest = JobsPageRequest(),
    ) -> JobsPage:
        _require(context, Permission.COLLECTION_READ)
        with self._lock:
            rows = [
                run
                for run in self._state.runs.values()
                if run.workspace_id == context.workspace_id
                and (query.connection_id is None or run.connection_id == query.connection_id)
                and (query.kind is None or run.kind is query.kind)
            ]
            rows.sort(key=lambda run: run.run_id)
            rows.sort(key=lambda run: run.enqueued_at, reverse=True)
            return self._page(
                context,
                rows,
                page,
                {
                    "view": "runs",
                    "connection_id": query.connection_id,
                    "kind": None if query.kind is None else query.kind.value,
                },
            )

    def list_schedules(
        self,
        context: WorkspaceContext,
        page: JobsPageRequest = JobsPageRequest(),
    ) -> JobsPage:
        _require(context, Permission.COLLECTION_READ)
        with self._lock:
            rows = [
                schedule
                for schedule in self._state.schedules.values()
                if schedule.workspace_id == context.workspace_id
            ]
            rows.sort(key=lambda schedule: schedule.schedule_id)
            rows.sort(key=lambda schedule: schedule.created_at, reverse=True)
            return self._page(context, rows, page, {"view": "schedules"})

    def _page(
        self,
        context: WorkspaceContext,
        rows: list[Any],
        page: JobsPageRequest,
        query: dict[str, Any],
    ) -> JobsPage:
        workspace_id = context.workspace_id
        revision = self._state.revisions.get(workspace_id, 0)
        fingerprint = _fingerprint({**query, "limit": page.limit})
        offset = 0
        if page.cursor is not None:
            record = self._state.cursors.get(page.cursor)
            if (
                record is None
                or record.workspace_id != workspace_id
                or record.query_fingerprint != fingerprint
            ):
                raise _safe_error(ErrorCode.INVALID_CURSOR, field="cursor")
            if record.revision != revision:
                raise _safe_error(ErrorCode.CURSOR_EXPIRED, field="cursor")
            offset = record.offset
            del self._state.cursors[page.cursor]

        window = rows[offset : offset + page.limit]
        next_cursor = None
        if offset + page.limit < len(rows):
            next_cursor = f"cursor_{self._tokens.new_token()}"
            self._state.cursors[next_cursor] = CursorRecord(
                workspace_id=workspace_id,
                query_fingerprint=fingerprint,
                revision=revision,
                offset=offset + page.limit,
                created_at=self._now(),
            )
        return JobsPage(items=tuple(window), next_cursor=next_cursor)

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
        return self._run_owner_content(context, run, authority, now)

    def _run_subscribers(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
    ) -> CollectionRun:
        collection_id = f"{run.run_id}-a{run.attempt}-subscribers"
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

    def _run_owner_content(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
    ) -> CollectionRun:
        videos_collection = f"{run.run_id}-a{run.attempt}-videos"
        videos = self._collect_inventory(
            context, run, authority, now, videos_collection
        )
        if videos.reason is not None:
            self._finish_collection(context, run, videos_collection, now, videos)
            return self._terminal(
                run, now, videos.reason, pages=videos.pages, spent=videos.quota_spent
            )

        inventory_id = f"{videos_collection}-inventory"
        self._channel_data.publish_video_inventory(
            context,
            PublishVideoInventory(
                channel_id=run.provider_channel_id,
                collection_id=videos_collection,
                inventory_id=inventory_id,
                captured_at=now,
                coverage_scope=VideoCoverageScope.OWNER_VIDEOS,
                videos=tuple(
                    VideoInput(
                        video_id=row.video_id,
                        title=row.title,
                        published_at=row.published_at,
                    )
                    for row in videos.rows
                ),
                idempotency_key=f"{inventory_id}-publish",
            ),
        )
        self._finish_collection(context, run, videos_collection, now, videos)
        return self._cover_comments(context, run, authority, now, videos, inventory_id)

    def _collect_inventory(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        collection_id: str,
    ) -> TraversalOutcome:
        self._channel_data.start_collection(
            context,
            StartCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                kind=CollectionKind.VIDEOS,
                started_at=now,
                idempotency_key=f"{collection_id}-start",
            ),
        )
        return self._traverse(context, authority, ProviderOperation.LIST_VIDEOS, None)

    def _cover_comments(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        videos: TraversalOutcome,
        inventory_id: str,
    ) -> CollectionRun:
        collection_id = f"{run.run_id}-a{run.attempt}-comments"
        self._channel_data.start_collection(
            context,
            StartCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                kind=CollectionKind.COMMENTS,
                started_at=now,
                idempotency_key=f"{collection_id}-start",
            ),
        )
        pages = videos.pages
        spent = videos.quota_spent
        covered = 0
        for video in videos.rows:
            activity = self._traverse(
                context,
                authority,
                ProviderOperation.LIST_VIDEO_COMMENT_AUTHORS,
                video.video_id,
            )
            pages += activity.pages
            spent += activity.quota_spent
            if activity.reason is not None:
                self._finish_partial(
                    context, run, collection_id, now, activity.reason, covered
                )
                return self._terminal(
                    run, now, activity.reason, pages=pages, spent=spent
                )
            self._channel_data.replace_video_comment_activity(
                context,
                ReplaceVideoCommentActivity(
                    channel_id=run.provider_channel_id,
                    collection_id=collection_id,
                    inventory_id=inventory_id,
                    video_id=video.video_id,
                    replaced_at=now,
                    activity=tuple(
                        VideoCommentActivityInput(
                            author_channel_id=row.author_channel_id,
                            comment_count=row.comment_count,
                            last_comment_at=row.latest_comment_at,
                        )
                        for row in activity.rows
                    ),
                    idempotency_key=f"{collection_id}-{video.video_id}",
                ),
            )
            covered += 1

        self._channel_data.finish_collection(
            context,
            FinishCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                status=CollectionStatus.COMPLETE,
                completed_at=now,
                progress_current=covered,
                progress_total=covered,
                failure_code=None,
                idempotency_key=f"{collection_id}-finish",
            ),
        )
        return self._terminal(run, now, None, pages=pages, spent=spent)

    def _finish_partial(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        collection_id: str,
        now: datetime,
        reason: RunFailureReason,
        covered: int,
    ) -> None:
        status = (
            CollectionStatus.FAILED
            if reason
            in {RunFailureReason.REAUTH_REQUIRED, RunFailureReason.UNEXPECTED_FAILURE}
            else CollectionStatus.PARTIAL
        )
        self._channel_data.finish_collection(
            context,
            FinishCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                status=status,
                completed_at=now,
                progress_current=covered,
                progress_total=covered,
                failure_code=(
                    _COLLECTION_FAILURE[reason]
                    if status is CollectionStatus.FAILED
                    else None
                ),
                idempotency_key=f"{collection_id}-finish",
            ),
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
                        operation=operation,
                        page_token=page_token,
                        video_id=video_id,
                        max_results=self._page_size,
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
                    _COLLECTION_FAILURE[outcome.reason]
                    if status is CollectionStatus.FAILED
                    else None
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
        if reason is RunFailureReason.PROVIDER_UNAVAILABLE and run.attempt < MAX_ATTEMPTS:
            return replace(
                run,
                status=RunStatus.QUEUED,
                attempt=run.attempt + 1,
                started_at=None,
                finished_at=None,
                failure_reason=None,
                pages_fetched=pages,
                quota_spent=spent,
                next_attempt_at=now + BACKOFF_SCHEDULE[run.attempt - 1],
            )
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
        self._bump_revision(run.workspace_id)

    def _bump_revision(self, workspace_id: str) -> None:
        self._state.revisions[workspace_id] = (
            self._state.revisions.get(workspace_id, 0) + 1
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
