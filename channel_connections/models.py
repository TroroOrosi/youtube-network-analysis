"""Immutable public values for tenant-scoped channel connections.

`RedactedSecret`, `ProviderCredential`, and `VerifiedProviderGrant` are internal
credential-boundary values. They stay out of the package root so no consumer can
treat a secret or a raw provider result as public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from workspace_access.models import AccessSecret as RedactedSecret
from workspace_access.models import AuditOutcome

from .errors import ChannelConnectionsError, ErrorCode


MAX_IDENTIFIER_LENGTH = 256
MAX_DISPLAY_TEXT_LENGTH = 500
MAX_CURSOR_LENGTH = 512
MAX_SECRET_LENGTH = 2048
MAX_URL_LENGTH = 2048

YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
APPROVED_SCOPES: tuple[str, ...] = (YOUTUBE_READONLY_SCOPE,)

INTENT_TTL = timedelta(minutes=10)
CALLBACK_REPLAY_TTL = timedelta(hours=24)
IDEMPOTENCY_TTL = timedelta(days=90)


class ConnectionProvider(str, Enum):
    YOUTUBE = "YOUTUBE"


class ConnectionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


class AuthorizationOperation(str, Enum):
    CONNECT = "CONNECT"
    REAUTHORIZE = "REAUTHORIZE"


class RevocationOutcome(str, Enum):
    REVOKED = "REVOKED"
    NOT_CONFIRMED = "NOT_CONFIRMED"


class AuthorizationFailureReason(str, Enum):
    PROVIDER_DENIED = "PROVIDER_DENIED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    SCOPE_NOT_GRANTED = "SCOPE_NOT_GRANTED"
    OFFLINE_CREDENTIAL_MISSING = "OFFLINE_CREDENTIAL_MISSING"
    CHANNEL_NOT_UNIQUE = "CHANNEL_NOT_UNIQUE"


class ConnectionAuditAction(str, Enum):
    AUTHORIZATION_STARTED = "AUTHORIZATION_STARTED"
    CONNECTION_ESTABLISHED = "CONNECTION_ESTABLISHED"
    REAUTHORIZATION_STARTED = "REAUTHORIZATION_STARTED"
    CONNECTION_REAUTHORIZED = "CONNECTION_REAUTHORIZED"
    CONNECTION_REAUTH_REQUIRED = "CONNECTION_REAUTH_REQUIRED"
    CONNECTION_DISCONNECTED = "CONNECTION_DISCONNECTED"
    WORKSPACE_CONNECTIONS_DELETED = "WORKSPACE_CONNECTIONS_DELETED"


def _invalid(field_name: str, message: str) -> ChannelConnectionsError:
    return ChannelConnectionsError(
        ErrorCode.INVALID_INPUT,
        message=message,
        field=field_name,
    )


def _identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_LENGTH
        or "\x00" in value
    ):
        raise _invalid(field_name, f"{field_name} must be a bounded non-empty identifier")
    return value


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _display_text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_DISPLAY_TEXT_LENGTH
        or "\x00" in value
    ):
        raise _invalid(field_name, f"{field_name} must be bounded text")
    return value


def _utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _invalid(field_name, f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _nonnegative_count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(field_name, f"{field_name} must be a non-negative integer")
    return value


def _enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise _invalid(field_name, f"{field_name} has an unsupported value")


def _tuple(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise _invalid(field_name, f"{field_name} must be a tuple")
    return value


def _https_url(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("https://")
        or len(value) > MAX_URL_LENGTH
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise _invalid(field_name, f"{field_name} must be a bounded HTTPS URL")
    return value


def _secret(value: object, field_name: str) -> RedactedSecret:
    if not isinstance(value, RedactedSecret):
        raise _invalid(field_name, f"{field_name} must be a redacted secret")
    revealed = value.reveal()
    if (
        not isinstance(revealed, str)
        or not revealed
        or len(revealed) > MAX_SECRET_LENGTH
        or "\x00" in revealed
    ):
        raise _invalid(field_name, f"{field_name} must hold a bounded non-empty secret")
    return value


def _safe_code(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTIFIER_LENGTH
        or any(character.isspace() for character in value)
        or "\x00" in value
    ):
        raise _invalid(field_name, f"{field_name} must be a bounded machine code")
    return value


def _approved_scopes(value: object, field_name: str) -> tuple[str, ...]:
    scopes = _tuple(value, field_name)
    if tuple(scopes) != APPROVED_SCOPES:
        raise _invalid(field_name, f"{field_name} must be exactly the approved scope set")
    return APPROVED_SCOPES


@dataclass(frozen=True, slots=True)
class ChannelConnection:
    """Safe workspace-owned connection metadata."""

    connection_id: str
    workspace_id: str
    provider: ConnectionProvider
    provider_channel_id: str
    channel_title: str
    status: ConnectionStatus
    connected_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.connection_id, "connection_id")
        _identifier(self.workspace_id, "workspace_id")
        _enum(self.provider, ConnectionProvider, "provider")
        _identifier(self.provider_channel_id, "provider_channel_id")
        _display_text(self.channel_title, "channel_title")
        _enum(self.status, ConnectionStatus, "status")
        object.__setattr__(self, "connected_at", _utc(self.connected_at, "connected_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    """The only authorization values an adapter may hand to a browser."""

    intent_id: str
    authorization_url: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.intent_id, "intent_id")
        _https_url(self.authorization_url, "authorization_url")
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))


@dataclass(frozen=True, slots=True)
class ConnectionPageRequest:
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
class ConnectionPage:
    items: tuple[ChannelConnection, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        items = _tuple(self.items, "items")
        for item in items:
            if not isinstance(item, ChannelConnection):
                raise _invalid("items", "items must hold channel connections")
        if self.next_cursor is not None:
            _identifier(self.next_cursor, "next_cursor")


@dataclass(frozen=True, slots=True)
class ConnectionRetentionReport:
    intents_removed: int
    callback_replays_removed: int
    idempotency_records_removed: int
    ephemeral_slots_removed: int
    credential_slots_removed: int

    def __post_init__(self) -> None:
        _nonnegative_count(self.intents_removed, "intents_removed")
        _nonnegative_count(self.callback_replays_removed, "callback_replays_removed")
        _nonnegative_count(
            self.idempotency_records_removed, "idempotency_records_removed"
        )
        _nonnegative_count(self.ephemeral_slots_removed, "ephemeral_slots_removed")
        _nonnegative_count(self.credential_slots_removed, "credential_slots_removed")


@dataclass(frozen=True, slots=True)
class ConnectionAuditEvent:
    """Secret-free evidence of one administrative connection action."""

    event_id: str
    workspace_id: str
    actor_user_id: str
    action: ConnectionAuditAction
    outcome: AuditOutcome
    occurred_at: datetime
    correlation_id: str
    connection_id: str | None = None
    intent_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.actor_user_id, "actor_user_id")
        _enum(self.action, ConnectionAuditAction, "action")
        _enum(self.outcome, AuditOutcome, "outcome")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        _identifier(self.correlation_id, "correlation_id")
        _optional_identifier(self.connection_id, "connection_id")
        _optional_identifier(self.intent_id, "intent_id")


@dataclass(frozen=True, slots=True)
class BeginAuthorization:
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class BeginReauthorization:
    connection_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.connection_id, "connection_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True, repr=False)
class CompleteAuthorization:
    """Bounded callback values; never a workspace, channel, or redirect claim."""

    state: RedactedSecret
    idempotency_key: str
    code: RedactedSecret | None = None
    provider_error: str | None = None

    def __post_init__(self) -> None:
        _secret(self.state, "state")
        _identifier(self.idempotency_key, "idempotency_key")
        if self.code is not None:
            _secret(self.code, "code")
        if self.provider_error is not None:
            _safe_code(self.provider_error, "provider_error")
        if (self.code is None) == (self.provider_error is None):
            raise _invalid(
                "code",
                "callback must carry either an authorization code or a provider error",
            )

    def __repr__(self) -> str:
        return "CompleteAuthorization(<REDACTED>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class DisconnectConnection:
    connection_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.connection_id, "connection_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class ReportCredentialInvalidation:
    """Records a detected revoked or expired grant without any provider detail."""

    connection_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.connection_id, "connection_id")
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class DeleteWorkspaceConnections:
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCredential:
    """Internal credential bundle handed only to the credential vault."""

    access_token: RedactedSecret
    refresh_token: RedactedSecret | None
    expires_at: datetime
    scopes: tuple[str, ...] = APPROVED_SCOPES

    def __post_init__(self) -> None:
        _secret(self.access_token, "access_token")
        if self.refresh_token is not None:
            _secret(self.refresh_token, "refresh_token")
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "scopes", _approved_scopes(self.scopes, "scopes"))

    def __repr__(self) -> str:
        return "ProviderCredential(<REDACTED>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedProviderGrant:
    """Internal strictly validated result of one provider exchange."""

    provider: ConnectionProvider
    provider_channel_id: str
    channel_title: str
    granted_scopes: tuple[str, ...]
    credential: ProviderCredential
    subscriber_capability_verified: bool

    def __post_init__(self) -> None:
        _enum(self.provider, ConnectionProvider, "provider")
        _identifier(self.provider_channel_id, "provider_channel_id")
        _display_text(self.channel_title, "channel_title")
        _tuple(self.granted_scopes, "granted_scopes")
        if not isinstance(self.credential, ProviderCredential):
            raise _invalid("credential", "credential must be a provider credential")
        if not isinstance(self.subscriber_capability_verified, bool):
            raise _invalid(
                "subscriber_capability_verified",
                "subscriber_capability_verified must be a boolean",
            )

    def __repr__(self) -> str:
        return "VerifiedProviderGrant(<REDACTED>)"

    __str__ = __repr__
