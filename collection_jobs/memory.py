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


@dataclass(frozen=True, slots=True)
class ResumePoint:
    """Where an owner-content run stopped, so the next slice continues it.

    Only the comment phase is resumable, and only because `channel-data`
    already accepts coverage one video at a time: the candidate collection
    named here stays open between slices and accumulates. The videos still to
    cover are listed rather than looked up, because the accepted inventory has
    no public listing and the identifiers are small; the rows already gathered
    are not here at all, and never can be — they live in the candidate.
    """

    workspace_id: str
    run_id: str
    inventory_id: str
    collection_id: str
    covered: int
    pending_video_ids: tuple[str, ...]
    pages_fetched: int
    quota_spent: int
    saved_at: datetime


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
    resume: dict[str, ResumePoint] = field(default_factory=dict)
