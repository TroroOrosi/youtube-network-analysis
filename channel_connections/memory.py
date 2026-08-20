"""Private in-memory state and deterministic fakes for channel connections.

The stores in this module are synthetic fixtures for tests and local reference
runs. They are not encryption, not durable, and not a credential vault. A
production deployment must supply an approved managed secret store or KMS-backed
repository behind the same ports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    AuthorizationOperation,
    ChannelConnection,
    ConnectionAuditEvent,
    ConnectionProvider,
    ProviderCredential,
    RedactedSecret,
)


@dataclass(slots=True)
class IdempotencyRecord:
    fingerprint: str
    result: object
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class StartedIntentRef:
    """Idempotency result for a begin call.

    Only the opaque intent id and expiry are retained. The authorization URL
    carries the one-time state value, so it lives in the ephemeral secret store
    and is deleted with the intent.
    """

    intent_id: str
    expires_at: datetime


@dataclass(slots=True)
class AuthorizationIntent:
    """One in-flight OAuth transaction bound to a single browser session."""

    intent_id: str
    workspace_id: str
    user_id: str
    session_id: str
    operation: AuthorizationOperation
    provider: ConnectionProvider
    redirect_uri_id: str
    target_connection_id: str | None
    state_digest: str
    verifier_slot_id: str
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None = None


@dataclass(slots=True)
class MemoryState:
    intents: dict[str, AuthorizationIntent] = field(default_factory=dict)
    intents_by_state: dict[str, str] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str, str], IdempotencyRecord] = field(
        default_factory=dict
    )
    connections: dict[str, ChannelConnection] = field(default_factory=dict)
    active_keys: dict[tuple[str, str, str], str] = field(default_factory=dict)
    audit_events: list[ConnectionAuditEvent] = field(default_factory=list)


class InMemoryEphemeralSecretStore:
    """Synthetic short-lived secret store. Not secure storage."""

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], tuple[RedactedSecret, datetime]] = {}

    def put(
        self,
        workspace_id: str,
        slot_id: str,
        secret: RedactedSecret,
        expires_at: datetime,
    ) -> None:
        self._slots[(workspace_id, slot_id)] = (secret, expires_at)

    def take(self, workspace_id: str, slot_id: str) -> RedactedSecret:
        entry = self._slots.pop((workspace_id, slot_id), None)
        if entry is None:
            raise LookupError("ephemeral secret slot is unavailable")
        return entry[0]

    def peek(self, workspace_id: str, slot_id: str) -> RedactedSecret | None:
        entry = self._slots.get((workspace_id, slot_id))
        return None if entry is None else entry[0]

    def delete(self, workspace_id: str, slot_id: str) -> None:
        self._slots.pop((workspace_id, slot_id), None)

    def slot_ids(self, workspace_id: str) -> tuple[str, ...]:
        return tuple(
            slot_id for stored, slot_id in self._slots if stored == workspace_id
        )

    def expired_slot_ids(self, reference_time: datetime) -> tuple[tuple[str, str], ...]:
        return tuple(
            key for key, entry in self._slots.items() if entry[1] <= reference_time
        )


class InMemoryCredentialVault:
    """Synthetic credential custody. Not encryption and not a KMS."""

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], ProviderCredential] = {}

    def put(
        self,
        workspace_id: str,
        slot_id: str,
        credential: ProviderCredential,
    ) -> None:
        self._slots[(workspace_id, slot_id)] = credential

    def delete(self, workspace_id: str, slot_id: str) -> None:
        self._slots.pop((workspace_id, slot_id), None)

    def contains(self, workspace_id: str, slot_id: str) -> bool:
        return (workspace_id, slot_id) in self._slots

    def slot_ids(self, workspace_id: str) -> tuple[str, ...]:
        return tuple(
            slot_id for stored, slot_id in self._slots if stored == workspace_id
        )
