"""Tenant-scoped orchestration of collection runs.

Execution is synchronous and caller driven: this module owns no thread, timer,
queue, or worker. Every public method resolves the exact permission on a trusted
`WorkspaceContext`, keys all state under that workspace, and serializes
transitions with one lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from threading import RLock
from types import TracebackType
from zoneinfo import ZoneInfo
from typing import Any, Callable

from channel_connections.errors import ChannelConnectionsError
from channel_connections.models import (
    ChannelSubscriptionRow,
    CommentAuthorRow,
    ExecutionAuthority,
    SubscriberRow,
    VideoRow,
    IssueExecutionAuthority,
    ProviderOperation,
    ProviderOperationRequest,
)
from channel_connections.ports import CollectionTargetResolver, ConnectionExecutionBroker
from channel_data.errors import ChannelDataError
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
from .memory import (
    AudienceResumePoint,
    CursorRecord,
    IdempotencyRecord,
    MemoryState,
    PageCheckpoint,
    QuotaLedgerEntry,
    ResumePoint,
)
from .models import (
    ACTIVE_STATUSES,
    AudienceChannel,
    AudienceNetworkSnapshot,
    AudienceViewerSubscriptions,
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


_LOG = logging.getLogger(__name__)

ESTIMATED_CALL_UNITS = 3
IDEMPOTENCY_TTL = timedelta(days=90)
RETENTION_TTL = timedelta(days=90)
# The longest one run may hold this module's lock in a single call. A scheduled
# caller has minutes to spend, but the same process serves pages with the same
# lock, so the work is handed back at short intervals rather than held for the
# whole slice. The caller comes straight back for the rest.
MAX_LOCK_SLICE_SECONDS = 20

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


def _next_utc_day(moment: datetime) -> datetime:
    """The first moment the daily quota budget is a fresh one.

    The ledger is keyed by UTC date, so a run that ran out of units cannot
    afford another call until midnight UTC, however many hours away that is.
    Waking it earlier would spend a request to learn nothing.
    """

    return datetime.combine(moment.date() + timedelta(days=1), time.min, tzinfo=UTC)


def _next_youtube_day(moment: datetime) -> datetime:
    """YouTube daily quota resets at Pacific midnight, including DST changes.

    The local workspace ledger deliberately remains UTC for backward-compatible
    accounting. It is an application budget, not the provider project ledger.
    """
    pacific = ZoneInfo("America/Los_Angeles")
    tomorrow = moment.astimezone(pacific).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=pacific).astimezone(UTC)


def _failure_reason(error: ChannelConnectionsError) -> RunFailureReason:
    if error.reason_code == "QUOTA_EXHAUSTED":
        return RunFailureReason.QUOTA_EXHAUSTED
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


def _merge_row(rows: dict[str, object], row: object) -> None:
    """Merge a page into the minimized traversal, never duplicating an author.

    Counts belong to distinct comment pages. For inventory/subscriber overlap,
    retain the first observation of each identity; subscriber order is arbitrary.
    """
    if isinstance(row, CommentAuthorRow):
        previous = rows.get(row.author_channel_id)
        if isinstance(previous, CommentAuthorRow):
            row = replace(
                row, comment_count=previous.comment_count + row.comment_count,
                latest_comment_at=max(previous.latest_comment_at, row.latest_comment_at),
            )
        rows[row.author_channel_id] = row
    elif isinstance(row, SubscriberRow):
        rows.setdefault(row.subscriber_channel_id, row)
    elif isinstance(row, ChannelSubscriptionRow):
        rows.setdefault(row.channel_id, row)
    elif isinstance(row, VideoRow):
        rows.setdefault(row.video_id, row)
    else:
        raise TypeError("unsupported provider checkpoint row")


class TraversalOutcome:
    """Rows gathered before a run stopped, with why it stopped."""

    __slots__ = (
        "rows", "pages", "quota_spent", "reason", "paused",
        "new_pages", "new_spent", "retry_at", "accessible",
    )

    def __init__(
        self,
        rows: tuple[object, ...],
        pages: int,
        quota_spent: int,
        reason: RunFailureReason | None,
        *,
        paused: bool = False,
        new_pages: int | None = None,
        new_spent: int | None = None,
        retry_at: datetime | None = None,
        accessible: bool = True,
    ) -> None:
        self.retry_at = retry_at
        self.accessible = accessible
        self.paused = paused
        self.new_pages = pages if new_pages is None else new_pages
        self.new_spent = quota_spent if new_spent is None else new_spent
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
        broker: ConnectionExecutionBroker,
        targets: CollectionTargetResolver,
        channel_data: Any,
        daily_quota_units: int = DEFAULT_DAILY_QUOTA_UNITS,
        page_size: int = 50,
        state_store: StateStore | None = None,
    ) -> None:
        self._clock = clock
        self._tokens = tokens
        self._broker = broker
        self._targets = targets
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
        # A previous process could persist RUNNING when publication raised.
        # Its cross-module commit status is uncertain: do not replay it blindly.
        # Fail closed, preserve accepted data, and let the owner start a new run.
        for key, run in tuple(state.runs.items()):
            if run.status is RunStatus.RUNNING:
                state.runs[key] = replace(
                    run, status=RunStatus.FAILED, finished_at=self._now(),
                    failure_reason=RunFailureReason.UNEXPECTED_FAILURE,
                    next_attempt_at=None,
                )
                state.resume.pop(key, None)
                state.page_checkpoints.pop(key, None)
                state.revisions[run.workspace_id] = state.revisions.get(run.workspace_id, 0) + 1
        return state

    def _flush(self) -> None:
        """Write the whole state document once a command has finished.

        A write that fails takes its change with it. Without that, memory holds
        a run the document has never heard of, the caller is told the write
        failed, and the next restart quietly reinstates the older truth — the
        one shape of data loss nobody goes looking for. Putting the state back
        to what the store still holds keeps the two readings of the world the
        same, and the error is raised so the caller knows nothing was kept.

        ponytail: the document is rewritten in full on every command; move to
        per-run rows when a workspace keeps more than a few thousand runs.
        """

        if self._state_store is None:
            return
        document = snapshot.dump(self._state)
        if document == self._document:
            return
        try:
            self._state_store.save(document)
        except Exception:
            self._state = (
                snapshot.load(self._document)
                if self._document is not None
                else MemoryState()
            )
            raise
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

            connection = self._targets.resolve_collection_target(
                context, command.connection_id
            )
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

            finished = self._work_one(context, command.run_id, command.slice_seconds, now)
            self._remember(record_key, payload, finished, now)
            return finished

    def _work_one(
        self,
        context: WorkspaceContext,
        run_id: str,
        slice_seconds: int | None,
        now: datetime,
    ) -> CollectionRun:
        """One slice of one run, with no ledger entry of its own.

        `execute_run` records what it did against the caller's idempotency key,
        because a client that retries a command must get the first answer back
        rather than a second collection. A driver working the queue has no such
        key and needs none: it never repeats a request, it asks what is due and
        does it. Giving each slice a synthetic key would write a record per
        slice into a document that is rewritten whole, which is how a state
        document grows until it cannot be written at all.
        """

        run = self._run(context.workspace_id, run_id)
        if run.status is not RunStatus.QUEUED:
            raise _safe_error(ErrorCode.INVALID_RUN_TRANSITION, field="run_id")
        if run.next_attempt_at is not None and now < run.next_attempt_at:
            raise _safe_error(
                ErrorCode.INVALID_RUN_TRANSITION, field="run_id", retryable=True
            )

        deadline = (
            now + timedelta(seconds=slice_seconds)
            if slice_seconds is not None
            else None
        )
        running = replace(
            run, status=RunStatus.RUNNING, started_at=run.started_at or now,
            pages_fetched=run.pages_fetched if run.started_at is not None else 0,
            quota_spent=run.quota_spent if run.started_at is not None else 0,
        )
        self._store(running)
        try:
            finished = self._execute(context, running, now, deadline)
        except Exception:
            # Do not persist a permanently unexecutable RUNNING record on error.
            # Re-raise so a storage/publication failure is never reported as success.
            self._forget_resume(running)
            progress = self._run(run.workspace_id, run.run_id)
            self._store(self._terminal(
                progress, now, RunFailureReason.UNEXPECTED_FAILURE,
                pages=progress.pages_fetched, spent=progress.quota_spent,
            ))
            raise
        self._store(finished)
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
            self._forget_resume(run)
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
            self._state.resume = {
                key: point
                for key, point in self._state.resume.items()
                if point.workspace_id != workspace_id
            }
            self._state.audience_resume = {
                key: point
                for key, point in self._state.audience_resume.items()
                if point.workspace_id != workspace_id
            }
            self._state.audience_snapshots = {
                key: value
                for key, value in self._state.audience_snapshots.items()
                if value.workspace_id != workspace_id
            }
            self._state.page_checkpoints = {
                key: point for key, point in self._state.page_checkpoints.items()
                if point.workspace_id != workspace_id
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
                removed = self._state.runs.pop(key)
                self._state.audience_snapshots.pop(
                    _key(removed.workspace_id, removed.run_id), None
                )
                self._state.audience_resume.pop(
                    _key(removed.workspace_id, removed.run_id), None
                )
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

            connection = self._targets.resolve_collection_target(
                context, command.connection_id
            )
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
                try:
                    connection = self._targets.resolve_collection_target(
                        context, schedule.connection_id
                    )
                except ChannelConnectionsError as error:
                    if error.code != "CONNECTION_NOT_FOUND_OR_FORBIDDEN":
                        raise
                    del self._state.schedules[schedule_key]
                    self._bump_revision(context.workspace_id)
                    continue
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

    def execute_due_runs(
        self,
        context: WorkspaceContext,
        reference_time: datetime,
        slice_seconds: int,
    ) -> tuple[CollectionRun, ...]:
        """Work every queued run that is due, until the slice is spent.

        This is the whole of what a driver has to know. A browser calls it with
        the seconds it dares hold a request open, a scheduler with the seconds
        it is willing to pay for, and neither decides anything else: what is due
        and what a run does next are decided here, once.

        A run whose `next_attempt_at` is still ahead is skipped rather than
        waited for, because the caller is a request that has to answer. Each run
        is offered at most one slice per call, so a run that suspends
        immediately cannot spin this loop.

        The lock is taken once per run and not once for the whole call. Holding
        it across the slice would be simpler, and would also stop every other
        request touching this module for as long as the slice lasts — two
        minutes, on a deployment that is one process. Between runs another
        caller may take a run this list still names, so what is due is checked
        again under the lock that works it.
        """

        _require(context, Permission.COLLECTION_RUN)
        if (
            not isinstance(reference_time, datetime)
            or reference_time.utcoffset() is None
        ):
            raise _safe_error(ErrorCode.INVALID_INPUT, field="reference_time")
        if isinstance(slice_seconds, bool) or not isinstance(slice_seconds, int):
            raise _safe_error(ErrorCode.INVALID_INPUT, field="slice_seconds")
        with self._lock:
            due = [
                run.run_id
                for _, run in sorted(self._state.runs.items())
                if run.workspace_id == context.workspace_id
                and run.status is RunStatus.QUEUED
                and (
                    run.next_attempt_at is None
                    or reference_time >= run.next_attempt_at
                )
            ]

        # One clock decides both halves: the budget is spent in the same time
        # the runs are judged by, so a caller whose `reference_time` has drifted
        # cannot be given a longer slice than it asked for.
        deadline = self._now() + timedelta(seconds=slice_seconds)
        worked: list[CollectionRun] = []
        for run_id in due:
            with self._lock:
                moment = self._now()
                remaining = int((deadline - moment).total_seconds())
                if remaining < 1:
                    break
                run = self._state.runs.get(_key(context.workspace_id, run_id))
                if run is None or run.status is not RunStatus.QUEUED:
                    continue
                if run.next_attempt_at is not None and moment < run.next_attempt_at:
                    continue
                worked.append(
                    self._work_one(
                        context, run_id, min(remaining, MAX_LOCK_SLICE_SECONDS), moment
                    )
                )
        return tuple(worked)

    def due_workspace_ids(self, reference_time: datetime) -> tuple[str, ...]:
        """Which workspaces have work waiting, for a driver that has no session.

        A scheduler wakes with no membership, no session, and nothing to scope
        itself by, so it has to be told where to ask. This is the one thing it
        may ask without a context, and it answers with nothing but workspace
        identifiers: never a run, a schedule, a count, or a time. The caller
        turns each identifier into its own least-privilege authority before it
        may do anything with it.
        """

        if (
            not isinstance(reference_time, datetime)
            or reference_time.utcoffset() is None
        ):
            raise _safe_error(ErrorCode.INVALID_INPUT, field="reference_time")
        with self._lock:
            workspaces = {
                run.workspace_id
                for run in self._state.runs.values()
                if run.status is RunStatus.QUEUED
                and (
                    run.next_attempt_at is None
                    or run.next_attempt_at <= reference_time
                )
            }
            workspaces.update(
                schedule.workspace_id
                for schedule in self._state.schedules.values()
                if schedule.enabled
                and (
                    schedule.last_enqueued_at is None
                    or schedule.last_enqueued_at + schedule.interval <= reference_time
                )
            )
            return tuple(sorted(workspaces))

    # Reads

    def active_runs(
        self, context: WorkspaceContext, connection_id: str | None = None
    ) -> tuple[CollectionRun, ...]:
        """Unfinished work, independent of the paginated terminal history.

        Used to resume collection and determine completion. History display
        limits must never hide an old quota-suspended run. No provider calls.
        """
        _require(context, Permission.COLLECTION_READ)
        query = RunQuery(connection_id=connection_id)
        with self._lock:
            return tuple(sorted(
                (run for run in self._state.runs.values()
                 if run.workspace_id == context.workspace_id
                 and run.status in ACTIVE_STATUSES
                 and (query.connection_id is None
                      or run.connection_id == query.connection_id)),
                key=lambda run: (run.enqueued_at, run.run_id), reverse=True,
            ))

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

    def latest_audience_network(
        self,
        context: WorkspaceContext,
        channel_id: str,
    ) -> AudienceNetworkSnapshot | None:
        """Newest completed aggregate source for one connected channel."""

        _require(context, Permission.ANALYSIS_READ)
        if (
            not isinstance(channel_id, str)
            or not channel_id
            or len(channel_id) > MAX_IDENTIFIER_LENGTH
        ):
            raise _safe_error(ErrorCode.INVALID_INPUT, field="channel_id")
        with self._lock:
            rows = [
                snapshot
                for snapshot in self._state.audience_snapshots.values()
                if snapshot.workspace_id == context.workspace_id
                and snapshot.channel_id == channel_id
            ]
            return max(
                rows,
                key=lambda item: (item.captured_at, item.run_id),
                default=None,
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
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        now: datetime,
        deadline: datetime | None,
    ) -> CollectionRun:
        try:
            authority = self._broker.issue_execution_authority(
                context, IssueExecutionAuthority(connection_id=run.connection_id)
            )
        except ChannelConnectionsError as error:
            return self._terminal(
                run, now, _failure_reason(error),
                pages=run.pages_fetched, spent=run.quota_spent,
            )

        if run.kind is RunKind.SUBSCRIBERS:
            return self._run_subscribers(context, run, authority, now, deadline)
        if run.kind is RunKind.AUDIENCE_NETWORK:
            return self._run_audience_network(context, run, authority, now, deadline)
        return self._run_owner_content(context, run, authority, now, deadline)

    def _run_subscribers(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        deadline: datetime | None,
    ) -> CollectionRun:
        collection_id = f"{run.run_id}-a{run.attempt}-subscribers"
        self._channel_data.start_collection(
            context,
            StartCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                kind=CollectionKind.SUBSCRIBERS,
                started_at=run.started_at or now,
                idempotency_key=f"{collection_id}-start",
            ),
        )
        outcome = self._traverse(
            context, authority, ProviderOperation.LIST_SUBSCRIBERS, None, run, deadline
        )
        if outcome.paused or outcome.reason is RunFailureReason.QUOTA_EXHAUSTED:
            return self._pause_traversal(run, now, outcome)
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

    def _run_audience_network(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        deadline: datetime | None,
    ) -> CollectionRun:
        """Collect public subscription lists for the latest comment-author panel."""

        resume_key = _key(run.workspace_id, run.run_id)
        resuming = self._state.audience_resume.get(resume_key)
        if resuming is None:
            try:
                pending = self._channel_data.load_audience_network_viewers(
                    context, run.provider_channel_id
                )
            except ChannelDataError as error:
                if error.code == "DATASET_NOT_READY":
                    return replace(
                        run,
                        status=RunStatus.QUEUED,
                        finished_at=None,
                        failure_reason=None,
                        next_attempt_at=now + timedelta(minutes=5),
                    )
                raise
            viewers: tuple[AudienceViewerSubscriptions, ...] = ()
        else:
            pending = resuming.pending_viewer_ids
            viewers = resuming.viewers

        completed = list(viewers)
        for index, viewer_channel_id in enumerate(pending):
            if deadline is not None and self._now() >= deadline:
                progress = self._run(run.workspace_id, run.run_id)
                self._state.audience_resume[resume_key] = AudienceResumePoint(
                    workspace_id=run.workspace_id,
                    run_id=run.run_id,
                    pending_viewer_ids=pending[index:],
                    viewers=tuple(completed),
                    pages_fetched=progress.pages_fetched,
                    quota_spent=progress.quota_spent,
                    saved_at=self._now(),
                )
                return replace(
                    progress,
                    status=RunStatus.QUEUED,
                    finished_at=None,
                    failure_reason=None,
                    next_attempt_at=now,
                )

            outcome = self._traverse(
                context,
                authority,
                ProviderOperation.LIST_CHANNEL_SUBSCRIPTIONS,
                None,
                run,
                deadline,
                channel_id=viewer_channel_id,
            )
            progress = self._run(run.workspace_id, run.run_id)
            if outcome.paused or outcome.reason is RunFailureReason.QUOTA_EXHAUSTED:
                self._state.audience_resume[resume_key] = AudienceResumePoint(
                    workspace_id=run.workspace_id,
                    run_id=run.run_id,
                    pending_viewer_ids=pending[index:],
                    viewers=tuple(completed),
                    pages_fetched=progress.pages_fetched,
                    quota_spent=progress.quota_spent,
                    saved_at=self._now(),
                )
                return replace(
                    progress,
                    status=RunStatus.QUEUED,
                    finished_at=None,
                    failure_reason=outcome.reason,
                    next_attempt_at=outcome.retry_at or now,
                )
            if outcome.reason is not None:
                self._state.audience_resume[resume_key] = AudienceResumePoint(
                    workspace_id=run.workspace_id,
                    run_id=run.run_id,
                    pending_viewer_ids=pending[index:],
                    viewers=tuple(completed),
                    pages_fetched=progress.pages_fetched,
                    quota_spent=progress.quota_spent,
                    saved_at=self._now(),
                )
                if (
                    outcome.reason is RunFailureReason.PROVIDER_UNAVAILABLE
                    and run.attempt < MAX_ATTEMPTS
                ):
                    return replace(
                        progress,
                        status=RunStatus.QUEUED,
                        attempt=run.attempt + 1,
                        started_at=run.started_at,
                        finished_at=None,
                        failure_reason=None,
                        next_attempt_at=now + BACKOFF_SCHEDULE[run.attempt - 1],
                    )
                self._forget_resume(run)
                return self._terminal(
                    progress,
                    now,
                    outcome.reason,
                    pages=progress.pages_fetched,
                    spent=progress.quota_spent,
                )

            subscriptions = tuple(
                AudienceChannel(row.channel_id, row.title)
                for row in outcome.rows
                if isinstance(row, ChannelSubscriptionRow)
            )
            completed.append(
                AudienceViewerSubscriptions(
                    viewer_channel_id=viewer_channel_id,
                    public=outcome.accessible,
                    subscriptions=subscriptions if outcome.accessible else (),
                )
            )

        progress = self._run(run.workspace_id, run.run_id)
        self._state.audience_snapshots[resume_key] = AudienceNetworkSnapshot(
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            channel_id=run.provider_channel_id,
            captured_at=self._now(),
            viewers=tuple(completed),
        )
        self._state.audience_resume.pop(resume_key, None)
        return self._terminal(
            progress,
            now,
            None,
            pages=progress.pages_fetched,
            spent=progress.quota_spent,
        )

    def _run_owner_content(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        deadline: datetime | None,
    ) -> CollectionRun:
        resuming = self._state.resume.get(_key(run.workspace_id, run.run_id))
        if resuming is not None:
            # The inventory phase is finished and promoted; the comment
            # collection named in the resume point is still open and already
            # holds every video covered so far. None of that is fetched again.
            return self._cover_comments(
                context,
                run,
                authority,
                now,
                deadline,
                collection_id=resuming.collection_id,
                inventory_id=resuming.inventory_id,
                pending=resuming.pending_video_ids,
                covered=resuming.covered,
                pages=resuming.pages_fetched,
                spent=resuming.quota_spent,
                opened=True,
                stalled=resuming.stalled,
            )

        videos_collection = f"{run.run_id}-a{run.attempt}-videos"
        videos = self._collect_inventory(
            context, run, authority, now, videos_collection, deadline
        )
        if videos.paused or videos.reason is RunFailureReason.QUOTA_EXHAUSTED:
            return self._pause_traversal(run, now, videos)
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
        return self._cover_comments(
            context,
            run,
            authority,
            now,
            deadline,
            collection_id=f"{run.run_id}-a{run.attempt}-comments",
            inventory_id=inventory_id,
            pending=tuple(row.video_id for row in videos.rows),
            covered=0,
            pages=videos.pages,
            spent=videos.quota_spent,
            opened=False,
            stalled=0,
        )

    def _collect_inventory(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        collection_id: str,
        deadline: datetime | None,
    ) -> TraversalOutcome:
        self._channel_data.start_collection(
            context,
            StartCollection(
                channel_id=run.provider_channel_id,
                collection_id=collection_id,
                kind=CollectionKind.VIDEOS,
                started_at=run.started_at or now,
                idempotency_key=f"{collection_id}-start",
            ),
        )
        return self._traverse(context, authority, ProviderOperation.LIST_VIDEOS, None, run, deadline)

    def _cover_comments(
        self,
        context: WorkspaceContext,
        run: CollectionRun,
        authority: ExecutionAuthority,
        now: datetime,
        deadline: datetime | None,
        *,
        collection_id: str,
        inventory_id: str,
        pending: tuple[str, ...],
        covered: int,
        pages: int,
        spent: int,
        opened: bool,
        stalled: int,
    ) -> CollectionRun:
        """Cover every video in the accepted inventory, over as many slices as it takes.

        Each video and each comment page can be continued, so even a single
        video's comment traversal may span multiple days without refetching
        its first page. It can be continued because `channel-data` accepts coverage
        one video at a time and keeps the candidate open until it is told the
        coverage is complete — so a slice that stops leaves behind work that
        counts, and nothing is promoted until the last video is covered.
        """

        if not opened:
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
        started_with = covered
        started_pages = pages
        for index, video_id in enumerate(pending):
            if deadline is not None and self._now() >= deadline:
                # A slice that covered nothing is not a stalled run: it is a
                # slice that was short. Quota stalls are only diagnostic;
                # neither a short slice nor a daily wait terminates this run.
                return self._suspend(
                    run,
                    now,
                    collection_id=collection_id,
                    inventory_id=inventory_id,
                    pending=pending[index:],
                    covered=covered,
                    pages=pages,
                    spent=spent,
                    until=now,
                    stalled=stalled,
                )
            activity = self._traverse(
                context,
                authority,
                ProviderOperation.LIST_VIDEO_COMMENT_AUTHORS,
                video_id,
                run,
                deadline,
            )
            pages += activity.new_pages
            spent += activity.new_spent
            if activity.paused:
                return self._suspend(
                    run, now, collection_id=collection_id, inventory_id=inventory_id,
                    pending=pending[index:], covered=covered, pages=pages, spent=spent,
                    until=now, stalled=stalled,
                )
            if activity.reason is RunFailureReason.QUOTA_EXHAUSTED:
                # A daily limit is a wait condition, not a three-day failure.
                # Retain the failed page even on days another run used the budget.
                idle = covered == started_with and pages == started_pages
                return self._suspend(
                    run,
                    now,
                    collection_id=collection_id,
                    inventory_id=inventory_id,
                    pending=pending[index:],
                    covered=covered,
                    pages=pages,
                    spent=spent,
                    until=activity.retry_at or _next_utc_day(self._now()),
                    reason=RunFailureReason.QUOTA_EXHAUSTED,
                    stalled=stalled + 1 if idle else 0,
                )
            if activity.reason is not None:
                self._forget_resume(run)
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
                    video_id=video_id,
                    replaced_at=now,
                    activity=tuple(
                        VideoCommentActivityInput(
                            author_channel_id=row.author_channel_id,
                            comment_count=row.comment_count,
                            last_comment_at=row.latest_comment_at,
                        )
                        for row in activity.rows
                    ),
                    idempotency_key=f"{collection_id}-{video_id}",
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
        self._forget_resume(run)
        return self._terminal(run, now, None, pages=pages, spent=spent)

    def _suspend(
        self,
        run: CollectionRun,
        now: datetime,
        *,
        collection_id: str,
        inventory_id: str,
        pending: tuple[str, ...],
        covered: int,
        pages: int,
        spent: int,
        until: datetime,
        stalled: int,
        reason: RunFailureReason | None = None,
    ) -> CollectionRun:
        """Remember where a run stopped and put it back in the queue.

        A suspended run is not finished and has not failed: it is queued again
        with the moment it may next be picked up, which is now when a slice ran
        out of wall clock and tomorrow when the day's units ran out. What it
        already collected stays in an open candidate that nothing reads, so the
        previously accepted dataset is still the one on show until this run
        covers its last video.
        """

        self._state.resume[_key(run.workspace_id, run.run_id)] = ResumePoint(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            inventory_id=inventory_id,
            collection_id=collection_id,
            covered=covered,
            pending_video_ids=tuple(pending),
            pages_fetched=pages,
            quota_spent=spent,
            saved_at=now,
            stalled=stalled,
        )
        return replace(
            run,
            status=RunStatus.QUEUED,
            finished_at=None,
            failure_reason=reason,
            pages_fetched=pages,
            quota_spent=spent,
            next_attempt_at=until,
        )

    def _forget_resume(self, run: CollectionRun) -> None:
        key = _key(run.workspace_id, run.run_id)
        self._state.resume.pop(key, None)
        self._state.audience_resume.pop(key, None)
        self._state.page_checkpoints.pop(key, None)

    def _pause_traversal(
        self, run: CollectionRun, now: datetime, outcome: TraversalOutcome
    ) -> CollectionRun:
        return replace(
            run, status=RunStatus.QUEUED, finished_at=None, failure_reason=outcome.reason,
            pages_fetched=outcome.pages, quota_spent=outcome.quota_spent,
            next_attempt_at=outcome.retry_at or now,
        )

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
        run: CollectionRun,
        deadline: datetime | None,
        *,
        channel_id: str | None = None,
    ) -> TraversalOutcome:
        checkpoint_key = _key(run.workspace_id, run.run_id)
        point = self._state.page_checkpoints.get(checkpoint_key)
        if point is not None and (
            point.operation != operation.value
            or point.video_id != video_id
            or point.channel_id != channel_id
        ):
            raise ValueError("provider checkpoint does not match the active traversal")
        rows: dict[str, object] = {}
        for row in point.rows if point is not None else ():
            _merge_row(rows, row)
        pages = point.pages_fetched if point is not None else 0
        spent = point.quota_spent if point is not None else 0
        initial_pages, initial_spent = pages, spent
        page_token = point.next_page_token if point is not None else None
        visited = list(point.visited_tokens) if point is not None else []
        seen = set(visited)
        accessible = True

        def outcome(reason=None, *, pause=False, keep=False, retry_at=None):
            if keep:
                self._state.page_checkpoints[checkpoint_key] = PageCheckpoint(
                    workspace_id=run.workspace_id, run_id=run.run_id,
                    operation=operation.value, video_id=video_id,
                    rows=tuple(rows.values()), next_page_token=page_token,
                    channel_id=channel_id,
                    visited_tokens=tuple(visited), pages_fetched=pages, quota_spent=spent,
                )
            else:
                self._state.page_checkpoints.pop(checkpoint_key, None)
            return TraversalOutcome(
                tuple(rows.values()), pages, spent, reason, paused=pause,
                new_pages=pages - initial_pages, new_spent=spent - initial_spent,
                retry_at=retry_at,
                accessible=accessible,
            )

        while True:
            # A single video may have thousands of pages. Check BEFORE every
            # provider request, not just at video boundaries. One in-flight
            # provider call can still outlast this cooperative deadline.
            if deadline is not None and self._now() >= deadline:
                return outcome(pause=True, keep=True)
            if self._quota_remaining(context.workspace_id) < ESTIMATED_CALL_UNITS:
                return outcome(
                    RunFailureReason.QUOTA_EXHAUSTED,
                    keep=True, retry_at=_next_utc_day(self._now()),
                )
            if page_token in seen:
                return outcome(RunFailureReason.UNEXPECTED_FAILURE)
            try:
                result = self._broker.run_provider_operation(
                    authority,
                    ProviderOperationRequest(
                        operation=operation,
                        page_token=page_token,
                        video_id=video_id,
                        channel_id=channel_id,
                        max_results=self._page_size,
                    ),
                )
            except ChannelConnectionsError as error:
                # code and reason_code are the machine-readable fields that
                # error carries for exactly this: stable, non-enumerating, and
                # free of provider text. The class name alone said nothing,
                # because every refusal arrives as the same class.
                _LOG.warning(
                    "provider operation %s refused: code=%s reason=%s "
                    "retryable=%s correlation=%s",
                    operation,
                    error.code,
                    error.reason_code,
                    error.retryable,
                    error.correlation_id,
                )
                reason = _failure_reason(error)
                if reason is RunFailureReason.QUOTA_EXHAUSTED:
                    # Rejected API calls also cost units. Never count them as
                    # successfully fetched pages or invalidate their page cursor.
                    cost = error.quota_cost
                    self._spend(context.workspace_id, cost)
                    spent += cost
                    return outcome(reason, keep=True, retry_at=_next_youtube_day(self._now()))
                return outcome(reason)
            except BaseException:
                # The owner is told one careful sentence and never a provider
                # response. The operator needs the opposite, and without this
                # line got nothing at all: every fault in the provider path,
                # including a plain bug in this process, became one
                # indistinguishable UNEXPECTED_FAILURE with no record of what
                # happened. The traceback names types and lines, not values, so
                # it carries no credential; the access token travels in a header
                # this code never formats into a message.
                _LOG.exception("provider operation %s raised", operation)
                return outcome(RunFailureReason.UNEXPECTED_FAILURE)

            self._spend(context.workspace_id, result.quota_cost)
            accessible = accessible and result.accessible
            progress = self._run(run.workspace_id, run.run_id)
            self._store(replace(
                progress, pages_fetched=progress.pages_fetched + 1,
                quota_spent=progress.quota_spent + result.quota_cost,
            ))
            for row in result.rows:
                _merge_row(rows, row)
            pages += 1
            spent += result.quota_cost
            visited.append(page_token)
            seen.add(page_token)
            page_token = result.next_page_token
            if page_token is None:
                return outcome()

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
        self._forget_resume(run)
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
