"""Private in-memory state for collection jobs.

This is a deterministic reference store, not a queue, database, or worker
platform. Execution is caller driven and synchronous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import CollectionRun, CollectionSchedule


@dataclass(slots=True)
class IdempotencyRecord:
    fingerprint: str
    result: object
    recorded_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CursorRecord:
    workspace_id: str
    query_fingerprint: str
    revision: int
    offset: int
    created_at: datetime


@dataclass(slots=True)
class QuotaLedgerEntry:
    used_units: int
    first_used_at: datetime


@dataclass(slots=True)
class MemoryState:
    runs: dict[str, CollectionRun] = field(default_factory=dict)
    schedules: dict[str, CollectionSchedule] = field(default_factory=dict)
    quota: dict[tuple[str, str], QuotaLedgerEntry] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str, str], IdempotencyRecord] = field(
        default_factory=dict
    )
    cursors: dict[str, CursorRecord] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)
