"""Immutable public values for tenant-scoped collection jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from .errors import CollectionJobsError, ErrorCode


MAX_IDENTIFIER_LENGTH = 256
MAX_CURSOR_LENGTH = 512

DEFAULT_DAILY_QUOTA_UNITS = 10000
MINIMUM_SCHEDULE_INTERVAL = timedelta(hours=1)
BACKOFF_SCHEDULE = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=25),
)
MAX_ATTEMPTS = 3


class RunKind(str, Enum):
    SUBSCRIBERS = "SUBSCRIBERS"
    OWNER_CONTENT = "OWNER_CONTENT"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunFailureReason(str, Enum):
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    CANCELLED = "CANCELLED"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)
TERMINAL_STATUSES = (
    RunStatus.SUCCEEDED,
    RunStatus.PARTIAL,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
)


def _invalid(field_name: str, message: str) -> CollectionJobsError:
    return CollectionJobsError(
        ErrorCode.INVALID_INPUT, message=message, field=field_name
    )


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


def _utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _invalid(field_name, f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _optional_utc(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _utc(value, field_name)


def _count(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid(field_name, f"{field_name} must be an integer >= {minimum}")
    return value


def _enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise _invalid(field_name, f"{field_name} has an unsupported value")


@dataclass(frozen=True, slots=True)
class CollectionRun:
    """One execution of a collection for a connected channel."""

    run_id: str
    workspace_id: str
    connection_id: str
    provider_channel_id: str
    kind: RunKind
    status: RunStatus
    attempt: int
    enqueued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    pages_fetched: int
    quota_spent: int
    failure_reason: RunFailureReason | None
    next_attempt_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.connection_id, "connection_id")
        _identifier(self.provider_channel_id, "provider_channel_id")
        _enum(self.kind, RunKind, "kind")
        _enum(self.status, RunStatus, "status")
        _count(self.attempt, "attempt", minimum=1)
        object.__setattr__(self, "enqueued_at", _utc(self.enqueued_at, "enqueued_at"))
        object.__setattr__(self, "started_at", _optional_utc(self.started_at, "started_at"))
        object.__setattr__(
            self, "finished_at", _optional_utc(self.finished_at, "finished_at")
        )
        object.__setattr__(
            self, "next_attempt_at", _optional_utc(self.next_attempt_at, "next_attempt_at")
        )
        _count(self.pages_fetched, "pages_fetched")
        _count(self.quota_spent, "quota_spent")
        if self.failure_reason is not None:
            _enum(self.failure_reason, RunFailureReason, "failure_reason")
        if self.status in TERMINAL_STATUSES:
            if self.finished_at is None:
                raise _invalid("finished_at", "a terminal run requires finished_at")
            if self.status is RunStatus.SUCCEEDED and self.failure_reason is not None:
                raise _invalid(
                    "failure_reason", "a succeeded run cannot carry a failure reason"
                )
            if self.status is not RunStatus.SUCCEEDED and self.failure_reason is None:
                raise _invalid("failure_reason", "this outcome requires a reason")
        elif self.finished_at is not None:
            raise _invalid("finished_at", "an active run cannot be finished")


@dataclass(frozen=True, slots=True)
class CollectionSchedule:
    schedule_id: str
    workspace_id: str
    connection_id: str
    kind: RunKind
    interval: timedelta
    enabled: bool
    created_at: datetime
    last_enqueued_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.schedule_id, "schedule_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.connection_id, "connection_id")
        _enum(self.kind, RunKind, "kind")
        _interval(self.interval, "interval")
        if not isinstance(self.enabled, bool):
            raise _invalid("enabled", "enabled must be a boolean")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(
            self,
            "last_enqueued_at",
            _optional_utc(self.last_enqueued_at, "last_enqueued_at"),
        )


def _interval(value: object, field_name: str) -> timedelta:
    if not isinstance(value, timedelta) or value < MINIMUM_SCHEDULE_INTERVAL:
        raise _invalid(field_name, f"{field_name} must be at least one hour")
    return value


@dataclass(frozen=True, slots=True)
class QuotaBudget:
    workspace_id: str
    daily_units: int = DEFAULT_DAILY_QUOTA_UNITS

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "workspace_id")
        _count(self.daily_units, "daily_units", minimum=1)


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    workspace_id: str
    usage_date: str
    used_units: int
    daily_units: int

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.usage_date, "usage_date")
        _count(self.used_units, "used_units")
        _count(self.daily_units, "daily_units", minimum=1)


@dataclass(frozen=True, slots=True)
class JobsRetentionReport:
    runs_removed: int
    quota_entries_removed: int
    idempotency_records_removed: int

    def __post_init__(self) -> None:
        _count(self.runs_removed, "runs_removed")
        _count(self.quota_entries_removed, "quota_entries_removed")
        _count(self.idempotency_records_removed, "idempotency_records_removed")


@dataclass(frozen=True, slots=True)
class JobsPageRequest:
    cursor: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if self.cursor is not None:
            if (
                not isinstance(self.cursor, str)
                or not self.cursor
                or self.cursor != self.cursor.strip()
                or len(self.cursor) > MAX_CURSOR_LENGTH
            ):
                raise _invalid("cursor", "cursor must be a bounded opaque token")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise _invalid("limit", "limit must be an integer from 1 to 100")


@dataclass(frozen=True, slots=True)
class JobsPage:
    items: tuple[object, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise _invalid("items", "items must be a tuple")
        if self.next_cursor is not None:
            _identifier(self.next_cursor, "next_cursor")


@dataclass(frozen=True, slots=True)
class RunQuery:
    connection_id: str | None = None
    kind: RunKind | None = None

    def __post_init__(self) -> None:
        if self.connection_id is not None:
            _identifier(self.connection_id, "connection_id")
        if self.kind is not None:
            _enum(self.kind, RunKind, "kind")


@dataclass(frozen=True, slots=True)
class EnqueueRun:
    connection_id: str
    kind: RunKind
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.connection_id, "connection_id")
        _enum(self.kind, RunKind, "kind")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class ExecuteRun:
    run_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class CancelRun:
    run_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class CreateSchedule:
    connection_id: str
    kind: RunKind
    interval: timedelta
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.connection_id, "connection_id")
        _enum(self.kind, RunKind, "kind")
        _interval(self.interval, "interval")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class DeleteSchedule:
    schedule_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.schedule_id, "schedule_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class DeleteWorkspaceJobs:
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.idempotency_key, "idempotency_key")
