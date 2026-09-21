"""Deterministic fixtures for channel-connections tests.

Every value here is synthetic. Nothing in this module performs a network call,
reads a local OAuth file, or provides real credential storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from channel_connections.models import (
    APPROVED_SCOPES,
    CommentAuthorRow,
    ChannelSubscriptionRow,
    CompleteAuthorization,
    ConnectionProvider,
    ProviderPage,
    SubscriberRow,
    VideoRow,
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

    def slot_ids(self, workspace_id: str) -> tuple[str, ...]:
        return self._delegate.slot_ids(workspace_id)

    def expired_slot_ids(self, reference_time: datetime) -> tuple[tuple[str, str], ...]:
        return self._delegate.expired_slot_ids(reference_time)

    def slot_ids_left(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._delegate._slots))


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


@dataclass
class DataCall:
    operation: str
    workspace_id: str
    credential_slot_id: str
    page_token: str | None
    video_id: str | None
    channel_id: str | None = None


class FakeYouTubeDataGateway:
    """Strict synthetic data gateway. It never contacts Google or the network."""

    QUOTA_COST = 3

    def __init__(self) -> None:
        self.calls: list[DataCall] = []
        self.failure: Exception | None = None
        self.subscribers = tuple(
            SubscriberRow(
                subscriber_channel_id=f"UC_sub_{index}",
                title=f"視聴者{index}",
                api_published_at=NOW - timedelta(days=age_days),
            )
            for index, age_days in enumerate((1, 5, 10, 200, 400), start=1)
        )
        self.videos = tuple(
            VideoRow(
                video_id=f"video-{index}",
                title=f"動画{index}",
                published_at=NOW - timedelta(days=index * 10),
            )
            for index in range(1, 4)
        )
        self.comment_authors = {
            "video-1": (
                CommentAuthorRow("video-1", "UC_sub_1", 2, NOW - timedelta(days=5)),
                CommentAuthorRow("video-1", "UC_sub_2", 1, NOW - timedelta(days=200)),
            ),
            "video-2": (
                CommentAuthorRow("video-2", "UC_sub_1", 1, NOW - timedelta(days=3)),
            ),
            "video-3": (),
        }
        self.channel_subscriptions: dict[str, tuple[ChannelSubscriptionRow, ...] | None] = {
            "UC_sub_1": (
                ChannelSubscriptionRow("UC_other_1", "登録先1"),
                ChannelSubscriptionRow("UC_shared", "共通チャンネル"),
            ),
            "UC_sub_2": (
                ChannelSubscriptionRow("UC_other_2", "登録先2"),
                ChannelSubscriptionRow("UC_shared", "共通チャンネル"),
            ),
        }

    def list_subscribers(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        self._record("LIST_SUBSCRIBERS", workspace_id, credential_slot_id, page_token, None, None)
        return self._page(self.subscribers, page_token, max_results)

    def list_channel_subscriptions(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        channel_id: str,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        self._record(
            "LIST_CHANNEL_SUBSCRIPTIONS",
            workspace_id,
            credential_slot_id,
            page_token,
            None,
            channel_id,
        )
        rows = self.channel_subscriptions.get(channel_id)
        if rows is None:
            return ProviderPage(
                rows=(), next_page_token=None,
                quota_cost=self.QUOTA_COST, accessible=False,
            )
        return self._page(rows, page_token, max_results)

    def list_videos(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        self._record("LIST_VIDEOS", workspace_id, credential_slot_id, page_token, None, None)
        return self._page(self.videos, page_token, max_results)

    def list_video_comment_authors(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        video_id: str,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        self._record(
            "LIST_VIDEO_COMMENT_AUTHORS",
            workspace_id,
            credential_slot_id,
            page_token,
            video_id,
            None,
        )
        return self._page(self.comment_authors.get(video_id, ()), page_token, max_results)

    def _record(
        self,
        operation: str,
        workspace_id: str,
        credential_slot_id: str,
        page_token: str | None,
        video_id: str | None,
        channel_id: str | None,
    ) -> None:
        self.calls.append(
            DataCall(
                operation, workspace_id, credential_slot_id,
                page_token, video_id, channel_id
            )
        )
        if self.failure is not None:
            raise self.failure

    def _page(
        self, rows: tuple[object, ...], page_token: str | None, max_results: int
    ) -> ProviderPage:
        offset = int(page_token.removeprefix("page-")) if page_token else 0
        window = rows[offset : offset + max_results]
        next_offset = offset + max_results
        return ProviderPage(
            rows=tuple(window),
            next_page_token=f"page-{next_offset}" if next_offset < len(rows) else None,
            quota_cost=self.QUOTA_COST,
        )
