"""Deterministic fixtures for channel-connections tests.

Every value here is synthetic. Nothing in this module performs a network call,
reads a local OAuth file, or provides real credential storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from channel_connections.models import (
    APPROVED_SCOPES,
    CompleteAuthorization,
    ConnectionProvider,
    ProviderCredential,
    RedactedSecret,
    RevocationOutcome,
    VerifiedProviderGrant,
)
from workspace_access.models import Permission, Role, WorkspaceContext


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
OWNER_PERMISSIONS = (
    Permission.CHANNEL_READ,
    Permission.CHANNEL_MANAGE_CONNECTION,
    Permission.WORKSPACE_DELETE,
)


def context(
    workspace_id: str = "workspace-1",
    *permissions: Permission,
    user_id: str = "user-1",
    session_id: str | None = None,
    role: Role = Role.OWNER,
) -> WorkspaceContext:
    granted = permissions or OWNER_PERMISSIONS
    return WorkspaceContext(
        workspace_id=workspace_id,
        user_id=user_id,
        membership_id=f"membership-{workspace_id}-{user_id}",
        role=role,
        permissions=frozenset(granted),
        session_id=session_id or f"session-{workspace_id}-{user_id}",
        authorization_revision=1,
        resolved_at=NOW,
    )


class FixedClock:
    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SequenceTokens:
    """Deterministic high-entropy stand-in with recorded ordering."""

    def __init__(self, prefix: str = "token") -> None:
        self._prefix = prefix
        self._issued = 0

    def new_token(self) -> str:
        self._issued += 1
        return f"{self._prefix}-{self._issued:04d}-{'x' * 32}"


@dataclass
class RecordedPut:
    workspace_id: str
    slot_id: str
    secret: RedactedSecret
    expires_at: datetime


class RecordingEphemeralStore:
    """Wraps the in-memory fake so tests can inspect what was stored."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.puts: list[RecordedPut] = []
        self.deleted: list[tuple[str, str]] = []

    def put(
        self,
        workspace_id: str,
        slot_id: str,
        secret: RedactedSecret,
        expires_at: datetime,
    ) -> None:
        self.puts.append(RecordedPut(workspace_id, slot_id, secret, expires_at))
        self._delegate.put(workspace_id, slot_id, secret, expires_at)

    def take(self, workspace_id: str, slot_id: str) -> RedactedSecret:
        return self._delegate.take(workspace_id, slot_id)

    def peek(self, workspace_id: str, slot_id: str) -> RedactedSecret | None:
        return self._delegate.peek(workspace_id, slot_id)

    def delete(self, workspace_id: str, slot_id: str) -> None:
        self.deleted.append((workspace_id, slot_id))
        self._delegate.delete(workspace_id, slot_id)

    def slot_ids_left(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._delegate._slots))

    def stored_secret(self, workspace_id: str, slot_id: str) -> str | None:
        for put in self.puts:
            if put.workspace_id == workspace_id and put.slot_id == slot_id:
                return put.secret.reveal()
        return None


@dataclass
class AuthorizationCall:
    state: str
    code_challenge: str
    redirect_uri_id: str
    scopes: tuple[str, ...]


@dataclass
class ExchangeCall:
    code: str
    code_verifier: str
    redirect_uri_id: str


class FailingCredentialVault:
    """Vault fake whose put always fails, modelling an unavailable store."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def put(self, workspace_id: str, slot_id: str, credential: object) -> None:
        raise RuntimeError("credential vault is unavailable")

    def delete(self, workspace_id: str, slot_id: str) -> None:
        self.deleted.append((workspace_id, slot_id))

    def contains(self, workspace_id: str, slot_id: str) -> bool:
        return False

    def slot_ids(self, workspace_id: str) -> tuple[str, ...]:
        return ()


class FakeYouTubeGateway:
    """Strict synthetic gateway. It never contacts Google or the network."""

    def __init__(
        self,
        *,
        provider_channel_id: str = "UC_channel_1",
        channel_title: str = "分析チャンネル",
    ) -> None:
        self.authorization_calls: list[AuthorizationCall] = []
        self.exchanges: list[ExchangeCall] = []
        self.revocations: list[tuple[str, str]] = []
        self.authorization_host = "accounts.google.com"
        self.failure: Exception | None = None
        self.revocation_outcome = RevocationOutcome.REVOKED
        self.grant = grant(
            provider_channel_id=provider_channel_id,
            channel_title=channel_title,
        )

    def authorization_url(
        self,
        *,
        state: RedactedSecret,
        code_challenge: str,
        redirect_uri_id: str,
        scopes: tuple[str, ...],
    ) -> str:
        self.authorization_calls.append(
            AuthorizationCall(state.reveal(), code_challenge, redirect_uri_id, scopes)
        )
        return (
            f"https://{self.authorization_host}/o/oauth2/v2/auth"
            f"?client_id=synthetic&response_type=code"
            f"&redirect_uri_id={redirect_uri_id}"
            f"&scope={scopes[0]}"
            f"&state={state.reveal()}"
            f"&code_challenge={code_challenge}&code_challenge_method=S256"
        )

    def exchange_and_verify(
        self,
        *,
        code: RedactedSecret,
        code_verifier: RedactedSecret,
        redirect_uri_id: str,
    ) -> VerifiedProviderGrant:
        self.exchanges.append(
            ExchangeCall(code.reveal(), code_verifier.reveal(), redirect_uri_id)
        )
        if self.failure is not None:
            raise self.failure
        return self.grant

    def revoke(self, workspace_id: str, credential_slot_id: str) -> RevocationOutcome:
        self.revocations.append((workspace_id, credential_slot_id))
        return self.revocation_outcome


def credential(
    *,
    access_token: str = "synthetic-access-token",
    refresh_token: str | None = "synthetic-refresh-token",
    expires_at: datetime | None = None,
) -> ProviderCredential:
    return ProviderCredential(
        access_token=RedactedSecret(access_token),
        refresh_token=RedactedSecret(refresh_token) if refresh_token else None,
        expires_at=expires_at or (NOW + timedelta(hours=1)),
        scopes=APPROVED_SCOPES,
    )


def grant(
    *,
    provider_channel_id: str = "UC_channel_1",
    channel_title: str = "分析チャンネル",
    granted_scopes: tuple[str, ...] = APPROVED_SCOPES,
    subscriber_capability_verified: bool = True,
    provider_credential: ProviderCredential | None = None,
) -> VerifiedProviderGrant:
    return VerifiedProviderGrant(
        provider=ConnectionProvider.YOUTUBE,
        provider_channel_id=provider_channel_id,
        channel_title=channel_title,
        granted_scopes=granted_scopes,
        credential=provider_credential or credential(),
        subscriber_capability_verified=subscriber_capability_verified,
    )


def latest_state(gateway: FakeYouTubeGateway) -> RedactedSecret:
    return RedactedSecret(gateway.authorization_calls[-1].state)


def callback(
    gateway: FakeYouTubeGateway,
    *,
    idempotency_key: str = "callback-1",
    code: str | None = "synthetic-authorization-code",
    provider_error: str | None = None,
) -> CompleteAuthorization:
    return CompleteAuthorization(
        state=latest_state(gateway),
        code=RedactedSecret(code) if code else None,
        provider_error=provider_error,
        idempotency_key=idempotency_key,
    )
