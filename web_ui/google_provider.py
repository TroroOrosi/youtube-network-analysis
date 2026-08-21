"""Real Google OAuth and YouTube Data API adapters behind the module ports.

The adapters own the endpoints, the transport, and the response validation, so
`channel-connections` never sees a URL, an HTTP status, or credential material
it did not ask for. Nothing here starts on its own: a deployment must supply a
registered client through `GoogleOAuthConfig`, and every call still runs under
an owner-authorized connection.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from urllib.parse import urlencode

from channel_connections.errors import ChannelConnectionsError
from channel_connections.models import (
    APPROVED_SCOPES,
    CommentAuthorRow,
    ConnectionProvider,
    ProviderCredential,
    ProviderPage,
    RedactedSecret,
    RevocationOutcome,
    SubscriberRow,
    VerifiedProviderGrant,
    VideoRow,
)
from channel_connections.ports import (
    ProviderAuthorizationExpired,
    ProviderRejected,
    ProviderUnavailable,
)
from channel_connections.models import AuthorizationFailureReason as Reason


AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
API_ROOT = "https://www.googleapis.com/youtube/v3"

REQUEST_TIMEOUT_SECONDS = 20.0
REFRESH_MARGIN = timedelta(seconds=60)

Transport = Callable[..., tuple[int, bytes]]
_Row = TypeVar("_Row", SubscriberRow, VideoRow, CommentAuthorRow)


@dataclass(frozen=True, slots=True)
class GoogleOAuthConfig:
    """A registered OAuth client. Redirect URIs are keyed by the port's id."""

    client_id: str
    client_secret: RedactedSecret
    redirect_uris: Mapping[str, str]


def http_transport(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """The default transport: stdlib HTTPS with a bounded timeout."""

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderUnavailable("transport failed") from error


class GoogleCredentialStore:
    """Credential custody shared by the adapters in this module only.

    It satisfies the write-only `CredentialVault` port; the read side is
    internal so no service or route can reach credential material through it.
    """

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], ProviderCredential] = {}

    def put(
        self, workspace_id: str, slot_id: str, credential: ProviderCredential
    ) -> None:
        self._slots[(workspace_id, slot_id)] = credential

    def delete(self, workspace_id: str, slot_id: str) -> None:
        self._slots.pop((workspace_id, slot_id), None)

    def slot_ids(self, workspace_id: str) -> tuple[str, ...]:
        return tuple(
            slot_id for stored, slot_id in self._slots if stored == workspace_id
        )

    def _read(self, workspace_id: str, slot_id: str) -> ProviderCredential | None:
        return self._slots.get((workspace_id, slot_id))


def _rejected(reason: Reason) -> ProviderRejected:
    return ProviderRejected(reason)


def _decode(status: int, body: bytes) -> dict[str, object]:
    """Read a provider JSON body, mapping every failure to a safe reason."""

    if status >= 500:
        raise ProviderUnavailable("provider returned a server error")
    if status >= 400:
        raise _rejected(Reason.PROVIDER_DENIED)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise _rejected(Reason.INVALID_PROVIDER_RESPONSE) from error
    if not isinstance(payload, dict):
        raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
    return payload


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
    return value


class _GoogleClient:
    """Shared transport and token handling for both gateways."""

    def __init__(
        self,
        config: GoogleOAuthConfig,
        store: GoogleCredentialStore,
        *,
        transport: Transport = http_transport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._store = store
        self._transport = transport
        self._now = now

    def _post_form(self, url: str, form: dict[str, str]) -> tuple[int, bytes]:
        return self._transport(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(form).encode(),
        )

    def _get_api(
        self, path: str, params: dict[str, str], access_token: str
    ) -> tuple[int, bytes]:
        return self._transport(
            "GET",
            f"{API_ROOT}/{path}?{urlencode(params)}",
            headers={"Authorization": f"Bearer {access_token}"},
            body=None,
        )

    def _redirect_uri(self, redirect_uri_id: str) -> str:
        uri = self._config.redirect_uris.get(redirect_uri_id)
        if uri is None:
            raise ProviderUnavailable("redirect uri is not registered")
        return uri

    def _credential_from(
        self, payload: Mapping[str, object], previous: ProviderCredential | None = None
    ) -> ProviderCredential:
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int):
            raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
        refresh = payload.get("refresh_token")
        refresh_token = (
            RedactedSecret(refresh)
            if isinstance(refresh, str) and refresh
            else (previous.refresh_token if previous else None)
        )
        granted = payload.get("scope")
        if isinstance(granted, str) and set(granted.split()) != set(APPROVED_SCOPES):
            raise _rejected(Reason.SCOPE_NOT_GRANTED)
        return ProviderCredential(
            access_token=RedactedSecret(_text(payload, "access_token")),
            refresh_token=refresh_token,
            expires_at=self._now() + timedelta(seconds=expires_in),
            scopes=APPROVED_SCOPES,
        )


class GoogleAuthorizationGateway(_GoogleClient):
    """Implements `YouTubeAuthorizationGateway` against Google's OAuth server."""

    def authorization_url(
        self,
        *,
        state: RedactedSecret,
        code_challenge: str,
        redirect_uri_id: str,
        scopes: tuple[str, ...],
    ) -> str:
        query = urlencode(
            {
                "client_id": self._config.client_id,
                "redirect_uri": self._redirect_uri(redirect_uri_id),
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state.reveal(),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "include_granted_scopes": "false",
                "prompt": "consent",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    def exchange_and_verify(
        self,
        *,
        code: RedactedSecret,
        code_verifier: RedactedSecret,
        redirect_uri_id: str,
    ) -> VerifiedProviderGrant:
        status, body = self._post_form(
            TOKEN_ENDPOINT,
            {
                "grant_type": "authorization_code",
                "code": code.reveal(),
                "code_verifier": code_verifier.reveal(),
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret.reveal(),
                "redirect_uri": self._redirect_uri(redirect_uri_id),
            },
        )
        credential = self._credential_from(_decode(status, body))
        access_token = credential.access_token.reveal()
        channel_id, channel_title = self._owner_channel(access_token)
        return VerifiedProviderGrant(
            provider=ConnectionProvider.YOUTUBE,
            provider_channel_id=channel_id,
            channel_title=channel_title,
            granted_scopes=credential.scopes,
            credential=credential,
            subscriber_capability_verified=self._subscriber_capability(access_token),
        )

    def _owner_channel(self, access_token: str) -> tuple[str, str]:
        status, body = self._get_api(
            "channels", {"part": "snippet", "mine": "true"}, access_token
        )
        payload = _decode(status, body)
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
        first = items[0]
        if not isinstance(first, dict):
            raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
        snippet = first.get("snippet")
        if not isinstance(snippet, dict):
            raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
        return _text(first, "id"), _text(snippet, "title")

    def _subscriber_capability(self, access_token: str) -> bool:
        """One minimal probe: can this grant read the owner's subscribers?"""

        status, _ = self._get_api(
            "subscriptions",
            {"part": "subscriberSnippet", "myRecentSubscribers": "true", "maxResults": "1"},
            access_token,
        )
        if status >= 500:
            raise ProviderUnavailable("provider returned a server error")
        return status < 400

    def revoke(self, workspace_id: str, credential_slot_id: str) -> RevocationOutcome:
        credential = self._store._read(workspace_id, credential_slot_id)
        if credential is None:
            return RevocationOutcome.NOT_CONFIRMED
        secret = credential.refresh_token or credential.access_token
        try:
            status, _ = self._post_form(REVOKE_ENDPOINT, {"token": secret.reveal()})
        except ProviderUnavailable:
            return RevocationOutcome.NOT_CONFIRMED
        if status >= 400:
            return RevocationOutcome.NOT_CONFIRMED
        self._store.delete(workspace_id, credential_slot_id)
        return RevocationOutcome.REVOKED


def _row(row_type: type[_Row], **fields: object) -> _Row | None:
    """Build a validated row, or nothing when the provider row is unusable."""

    try:
        return row_type(**fields)  # type: ignore[arg-type]
    except ChannelConnectionsError:
        return None


def _moment(value: object) -> datetime | None:
    """Read an RFC 3339 provider timestamp, or nothing it can be trusted for."""

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mapping(value: object, key: str) -> Mapping[str, object]:
    nested = value.get(key) if isinstance(value, Mapping) else None
    return nested if isinstance(nested, Mapping) else {}


def _items(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
    return [item for item in items if isinstance(item, Mapping)]


def _next_page_token(payload: Mapping[str, object]) -> str | None:
    token = payload.get("nextPageToken")
    return token if isinstance(token, str) and token else None


class GoogleDataGateway(_GoogleClient):
    """Implements `YouTubeDataGateway` against the YouTube Data API v3.

    A row the provider returns without the fields this product needs is skipped
    rather than failing the whole page: deleted and hidden channels are normal,
    and a partial page is reported honestly by `collection-jobs`.
    """

    def __init__(
        self,
        config: GoogleOAuthConfig,
        store: GoogleCredentialStore,
        *,
        transport: Transport = http_transport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(config, store, transport=transport, now=now)
        self._uploads_playlists: dict[tuple[str, str], str] = {}

    def list_subscribers(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        payload = self._list(
            workspace_id,
            credential_slot_id,
            "subscriptions",
            {
                "part": "subscriberSnippet,snippet",
                "myRecentSubscribers": "true",
                "maxResults": str(max_results),
            },
            page_token,
        )
        rows = []
        for item in _items(payload):
            subscriber = _mapping(item, "subscriberSnippet")
            row = _row(
                SubscriberRow,
                subscriber_channel_id=subscriber.get("channelId"),
                title=subscriber.get("title"),
                api_published_at=_moment(_mapping(item, "snippet").get("publishedAt")),
            )
            if row is not None:
                rows.append(row)
        return ProviderPage(
            rows=tuple(rows),
            next_page_token=_next_page_token(payload),
            quota_cost=1,
        )

    def list_videos(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        quota_cost = 1
        key = (workspace_id, credential_slot_id)
        if key not in self._uploads_playlists:
            self._uploads_playlists[key] = self._uploads_playlist(*key)
            quota_cost += 1
        payload = self._list(
            workspace_id,
            credential_slot_id,
            "playlistItems",
            {
                "part": "snippet",
                "playlistId": self._uploads_playlists[key],
                "maxResults": str(max_results),
            },
            page_token,
        )
        rows = []
        for item in _items(payload):
            snippet = _mapping(item, "snippet")
            row = _row(
                VideoRow,
                video_id=_mapping(snippet, "resourceId").get("videoId"),
                title=snippet.get("title"),
                published_at=_moment(snippet.get("publishedAt")),
            )
            if row is not None:
                rows.append(row)
        return ProviderPage(
            rows=tuple(rows),
            next_page_token=_next_page_token(payload),
            quota_cost=quota_cost,
        )

    def list_video_comment_authors(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        video_id: str,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        payload = self._list(
            workspace_id,
            credential_slot_id,
            "commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": str(max_results),
                "order": "time",
            },
            page_token,
        )
        counts: dict[str, int] = {}
        latest: dict[str, datetime] = {}
        for item in _items(payload):
            comment = _mapping(_mapping(item, "snippet"), "topLevelComment")
            snippet = _mapping(comment, "snippet")
            author = _mapping(snippet, "authorChannelId").get("value")
            commented_at = _moment(snippet.get("publishedAt"))
            if not isinstance(author, str) or not author or commented_at is None:
                continue
            counts[author] = counts.get(author, 0) + 1
            latest[author] = max(latest.get(author, commented_at), commented_at)
        rows = []
        for author, count in counts.items():
            row = _row(
                CommentAuthorRow,
                video_id=video_id,
                author_channel_id=author,
                comment_count=count,
                latest_comment_at=latest[author],
            )
            if row is not None:
                rows.append(row)
        return ProviderPage(
            rows=tuple(rows),
            next_page_token=_next_page_token(payload),
            quota_cost=1,
        )

    def _uploads_playlist(self, workspace_id: str, credential_slot_id: str) -> str:
        payload = self._list(
            workspace_id,
            credential_slot_id,
            "channels",
            {"part": "contentDetails", "mine": "true"},
            None,
        )
        items = _items(payload)
        if not items:
            raise _rejected(Reason.INVALID_PROVIDER_RESPONSE)
        related = _mapping(_mapping(items[0], "contentDetails"), "relatedPlaylists")
        return _text(related, "uploads")

    def _list(
        self,
        workspace_id: str,
        credential_slot_id: str,
        path: str,
        params: dict[str, str],
        page_token: str | None,
    ) -> Mapping[str, object]:
        if page_token is not None:
            params = {**params, "pageToken": page_token}
        status, body = self._get_api(
            path, params, self._access_token(workspace_id, credential_slot_id)
        )
        if status in (401, 403):
            raise ProviderAuthorizationExpired("the grant no longer permits this call")
        return _decode(status, body)

    def _access_token(self, workspace_id: str, credential_slot_id: str) -> str:
        credential = self._store._read(workspace_id, credential_slot_id)
        if credential is None:
            raise ProviderAuthorizationExpired("no credential is held for this slot")
        if credential.expires_at - REFRESH_MARGIN > self._now():
            return credential.access_token.reveal()
        return self._refresh(workspace_id, credential_slot_id, credential)

    def _refresh(
        self, workspace_id: str, credential_slot_id: str, credential: ProviderCredential
    ) -> str:
        if credential.refresh_token is None:
            raise ProviderAuthorizationExpired("the grant carries no offline access")
        status, body = self._post_form(
            TOKEN_ENDPOINT,
            {
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh_token.reveal(),
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret.reveal(),
            },
        )
        if 400 <= status < 500:
            raise ProviderAuthorizationExpired("the provider refused the refresh")
        refreshed = self._credential_from(_decode(status, body), credential)
        self._store.put(workspace_id, credential_slot_id, refreshed)
        return refreshed.access_token.reveal()
