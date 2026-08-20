"""Private in-memory state records and audit collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import AuditCategory, AuditEvent, Membership, Workspace


@dataclass(slots=True)
class UserState:
    user_id: str
    issuer: str | None
    subject: str | None
    verified_email: str | None
    display_name: str | None
    enabled: bool = True
    deleted_at: datetime | None = None


@dataclass(slots=True)
class SessionState:
    session_id: str
    user_id: str
    secret_digest: str
    authenticated_at: datetime
    last_used_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    actor_user_id: str
    operation: str
    payload: tuple[str, ...]
    result: Membership | Workspace | None


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self._events.append(event)

    def for_workspace(self, workspace_id: str) -> tuple[AuditEvent, ...]:
        return self._sorted(
            event for event in self._events if event.workspace_id == workspace_id
        )

    def for_actor(self, user_id: str) -> tuple[AuditEvent, ...]:
        return self._sorted(
            event for event in self._events if event.actor_user_id == user_id
        )

    def purge(self, reference_time: datetime) -> tuple[int, int]:
        purged = {category: 0 for category in AuditCategory}
        retained: list[AuditEvent] = []
        for event in self._events:
            retention = (
                timedelta(days=90)
                if event.category is AuditCategory.SECURITY
                else timedelta(days=365)
            )
            if event.occurred_at + retention <= reference_time:
                purged[event.category] += 1
            else:
                retained.append(event)
        self._events = retained
        return (
            purged[AuditCategory.SECURITY],
            purged[AuditCategory.ADMINISTRATION],
        )

    def counts(self) -> dict[AuditCategory, int]:
        return {
            category: sum(event.category is category for event in self._events)
            for category in AuditCategory
        }

    @staticmethod
    def _sorted(events) -> tuple[AuditEvent, ...]:
        return tuple(sorted(events, key=lambda event: (event.occurred_at, event.event_id)))
