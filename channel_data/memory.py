"""Private in-memory state for the deterministic reference repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from .models import (
    CollectionState,
    SubscriberObservation,
    SubscriberRegistryEntry,
    SubscriberSnapshot,
)


@dataclass(slots=True)
class IdempotencyRecord:
    actor_user_id: str
    operation: str
    payload_fingerprint: str
    result: object
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriberCandidate:
    snapshot: SubscriberSnapshot
    observations: tuple[SubscriberObservation, ...]


@dataclass(slots=True)
class MemoryState:
    collections: dict[tuple[str, str], CollectionState] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)
    subscriber_candidates: dict[tuple[str, str], SubscriberCandidate] = field(
        default_factory=dict
    )
    subscriber_snapshots: dict[tuple[str, str, str], SubscriberSnapshot] = field(
        default_factory=dict
    )
    subscriber_observations: dict[
        tuple[str, str, str], tuple[SubscriberObservation, ...]
    ] = field(default_factory=dict)
    subscriber_registry: dict[
        tuple[str, str, str], SubscriberRegistryEntry
    ] = field(default_factory=dict)
    accepted_subscriber_snapshot: dict[tuple[str, str], str] = field(
        default_factory=dict
    )
