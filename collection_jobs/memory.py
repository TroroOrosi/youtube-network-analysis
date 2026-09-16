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

    This records completed videos and the still-open comment candidate.
    PageCheckpoint separately retains minimized rows within the current
    video (and also supports inventory/subscriber time slicing). Completed
    video rows live only in the channel-data candidate, not in this record.
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
    # Days that bought no new page or completed video. Large videos that
    # steadily accumulate pages must not be abandoned as stalled.
    stalled: int = 0


@dataclass(frozen=True, slots=True)
class PageCheckpoint:
    """Minimized rows and the next cursor of an unfinished provider traversal.

    No authority, access token, credential, or comment text is persisted here.
    A checkpoint belongs to exactly one tenant/run/operation/video combination.
    """

    workspace_id: str
    run_id: str
    operation: str
    video_id: str | None
    rows: tuple[object, ...]
    next_page_token: str | None
    visited_tokens: tuple[str | None, ...]
    pages_fetched: int
    quota_spent: int


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
    page_checkpoints: dict[str, PageCheckpoint] = field(default_factory=dict)
