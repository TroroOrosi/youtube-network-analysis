"""Private in-memory state for the deterministic reference repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from .models import CollectionState


@dataclass(slots=True)
class IdempotencyRecord:
    actor_user_id: str
    operation: str
    payload_fingerprint: str
    result: object
    completed_at: datetime


@dataclass(slots=True)
class MemoryState:
    collections: dict[tuple[str, str], CollectionState] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)
