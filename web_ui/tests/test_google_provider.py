from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from channel_connections.models import (
    APPROVED_SCOPES,
    ProviderCredential,
    RedactedSecret,
    RevocationOutcome,
)
from channel_connections.ports import (
    ProviderAuthorizationExpired,
    ProviderRejected,
    ProviderUnavailable,
)

from web_ui.google_provider import (
    API_ROOT,
    TOKEN_ENDPOINT,
    GoogleAuthorizationGateway,
    GoogleCredentialStore,
    GoogleDataGateway,
    GoogleOAuthConfig,
    _error_reasons,
    _is_grant_failure,
)


NOW = datetime(2026, 6, 1, tzinfo=UTC)

API_KEY = "test-api-key"

CONFIG = GoogleOAuthConfig(
    client_id="client-123.apps.googleusercontent.com",
    client_secret=RedactedSecret("client-secret"),
    redirect_uris={"primary": "https://app.example/oauth/callback"},
)


def token_response(**overrides: object) -> tuple[int, bytes]:
    body = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": APPROVED_SCOPES[0],
        "token_type": "Bearer",
    }
    body.update(overrides)
    return 200, json.dumps(body).encode()


def channel_response() -> tuple[int, bytes]:
    body = {
        "items": [
            {
                "id": "UC_real",
                "snippet": {"title": "本物チャンネル"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU_real"}},
            }
        ]
    }
    return 200, json.dumps(body).encode()


class FakeTransport:
    """Records requests and replies from a queue keyed by URL prefix."""

    def __init__(self, replies: dict[str, tuple[int, bytes]]) -> None:
        self._replies = replies
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self, method: str, url: str, *, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.requests.append((method, url, headers, body))
        for prefix, reply in self._replies.items():
            if url.startswith(prefix):
                return reply
        raise AssertionError(f"unexpected request: {url}")

    def sent_form(self, index: int) -> dict[str, list[str]]:
        return parse_qs((self.requests[index][3] or b"").decode())


def default_replies() -> dict[str, tuple[int, bytes]]:
    """A whole fake Google: enough for authorization and one collection run."""

    root = "https://www.googleapis.com/youtube/v3"
    subscriptions = {
        "items": [
            {
                "snippet": {"publishedAt": "2026-01-02T03:04:05Z"},
                "subscriberSnippet": {"channelId": "UC_sub_1", "title": "視聴者1"},
            }
        ]
    }
    playlist_items = {
        "items": [
            {
                "snippet": {
                    "title": "動画1",
                    "publishedAt": "2026-02-03T00:00:00Z",
                    "resourceId": {"videoId": "vid-1"},
                }
            }
        ]
    }
    comment_threads = {
        "items": [
            {
                "snippet": {
                    "topLevelComment": {
                        "snippet": {
                            "publishedAt": "2026-03-01T00:00:00Z",
                            "authorChannelId": {"value": "UC_sub_1"},
                        }
                    }
                }
            }
        ]
    }
    identity = {
        "sub": "1234567890",
        "name": "運用担当",
        "email": "owner@example.com",
        "email_verified": True,
    }
    return {
        "https://oauth2.googleapis.com/token": token_response(),
        "https://openidconnect.googleapis.com/v1/userinfo": (
            200,
            json.dumps(identity).encode(),
        ),
        f"{root}/channels": channel_response(),
        f"{root}/subscriptions": (200, json.dumps(subscriptions).encode()),
        f"{root}/playlistItems": (200, json.dumps(playlist_items).encode()),
        f"{root}/commentThreads": (200, json.dumps(comment_threads).encode()),
    }


class FakeStateStore:
    def __init__(self, document: str | None = None) -> None:
        self.document = document
        self.saves = 0

    def load(self) -> str | None:
        return self.document

    def save(self, document: str) -> None:
        self.document = document
        self.saves += 1


class SlowStateStore(FakeStateStore):
    """A vault that takes long enough for a second thread to reach it."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def save(self, document: str) -> None:
        self.events.append("enter")
        time.sleep(0.02)
        super().save(document)
        self.events.append("leave")


def credential(refresh: str | None = "rt-1", **overrides: object) -> ProviderCredential:
    fields: dict[str, object] = {
        "access_token": RedactedSecret("at-1"),
        "refresh_token": None if refresh is None else RedactedSecret(refresh),
        "expires_at": NOW + timedelta(hours=1),
    }
    fields.update(overrides)
    return ProviderCredential(**fields)  # type: ignore[arg-type]


class CredentialPersistenceTests(unittest.TestCase):
    """The vault outlives the process, holding only what it must hold."""

    def build(self, document: str | None = None):
        state = FakeStateStore(document)
        return GoogleCredentialStore(state_store=state, now=lambda: NOW), state

    def test_only_the_refresh_material_is_written(self) -> None:
        """The access token expires in an hour; keeping it buys nothing."""

        store, state = self.build()
        store.put("ws-1", "slot-1", credential("rt-secret"))
        self.assertIn("rt-secret", state.document or "")
        self.assertNotIn("at-1", state.document or "")

    def test_a_restart_brings_the_slot_back(self) -> None:
        store, state = self.build()
        store.put("ws-1", "slot-1", credential("rt-secret"))

        restored = GoogleCredentialStore(
            state_store=FakeStateStore(state.document), now=lambda: NOW
        )
        self.assertEqual(restored.slot_ids("ws-1"), ("slot-1",))
        back = restored._read("ws-1", "slot-1")
        assert back is not None
        self.assertEqual(back.refresh_token.reveal(), "rt-secret")

    def test_the_restored_slot_is_expired_so_the_first_call_refreshes(self) -> None:
        store, state = self.build()
        store.put("ws-1", "slot-1", credential())
        restored = GoogleCredentialStore(
            state_store=FakeStateStore(state.document), now=lambda: NOW
        )
        back = restored._read("ws-1", "slot-1")
        assert back is not None
        self.assertLess(back.expires_at, NOW)

    def test_an_unchanged_refresh_token_is_not_rewritten(self) -> None:
        """A refresh happens hourly and usually returns the same token."""

        store, state = self.build()
        store.put("ws-1", "slot-1", credential("rt-same"))
        after_first = state.saves
        store.put(
            "ws-1", "slot-1", credential("rt-same", expires_at=NOW + timedelta(hours=2))
        )
        self.assertEqual(state.saves, after_first)

    def test_a_rotated_refresh_token_is_written(self) -> None:
        store, state = self.build()
        store.put("ws-1", "slot-1", credential("rt-old"))
        before = state.saves
        store.put("ws-1", "slot-1", credential("rt-new"))
        self.assertEqual(state.saves, before + 1)

    def test_deleting_a_slot_removes_it_from_the_document(self) -> None:
        store, state = self.build()
        store.put("ws-1", "slot-1", credential("rt-gone"))
        store.delete("ws-1", "slot-1")
        self.assertEqual(json.loads(state.document or ""), {})

    def test_a_grant_without_offline_access_is_never_written(self) -> None:
        store, state = self.build()
        store.put("ws-1", "slot-1", credential(None))
        self.assertEqual(state.saves, 0)

    def test_a_document_it_cannot_read_stops_the_start(self) -> None:
        with self.assertRaises(ValueError):
            self.build("this is not the document")

    def test_two_threads_do_not_interleave_a_save(self) -> None:
        """Route handlers run in a thread pool, so two owners can finish at once.

        Interleaved, both saves would add a version to the vault and destroy
        the one the other added, and the document left readable could be the
        earlier of the two: a connection silently missing from a store nobody
        will be asked to authorize again.
        """

        state = SlowStateStore()
        store = GoogleCredentialStore(state_store=state, now=lambda: NOW)
        threads = [
            threading.Thread(
                target=store.put, args=("ws-1", f"slot-{index}", credential(f"rt-{index}"))
            )
            for index in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(state.events, ["enter", "leave", "enter", "leave"])
        self.assertEqual(len(json.loads(state.document or "{}")), 2)

    def test_without_a_store_it_keeps_the_slots_in_memory_only(self) -> None:
        store = GoogleCredentialStore(now=lambda: NOW)
        store.put("ws-1", "slot-1", credential("rt-secret"))
        self.assertEqual(store.slot_ids("ws-1"), ("slot-1",))


class GrantFailureTests(unittest.TestCase):
    """Which refusals mean the grant is gone, and which only mean "not this".

    Getting this wrong is not a cosmetic error: a grant judged gone has its
    credential deleted, and the owner is asked to authorize again. When the
    cause is a video with comments switched off, authorizing again meets the
    same refusal and deletes the new credential too.
    """

    @staticmethod
    def _body(*reasons: str) -> bytes:
        return json.dumps(
            {"error": {"errors": [{"reason": reason} for reason in reasons]}}
        ).encode()

    def test_401_is_always_the_grant(self) -> None:
        self.assertTrue(_is_grant_failure(401, self._body("authError")))
        self.assertTrue(_is_grant_failure(401, b""))

    def test_403_about_authorization_is_the_grant(self) -> None:
        for reason in ("authError", "insufficientPermissions"):
            with self.subTest(reason=reason):
                self.assertTrue(_is_grant_failure(403, self._body(reason)))

    def test_403_about_the_resource_is_not_the_grant(self) -> None:
        for reason in ("commentsDisabled", "quotaExceeded", "forbidden"):
            with self.subTest(reason=reason):
                self.assertFalse(_is_grant_failure(403, self._body(reason)))

    def test_an_unreadable_403_is_not_the_grant(self) -> None:
        self.assertFalse(_is_grant_failure(403, b"<html>gateway</html>"))

    def test_other_statuses_are_never_the_grant(self) -> None:
        for status in (200, 400, 404, 429, 500):
            with self.subTest(status=status):
                self.assertFalse(_is_grant_failure(status, self._body("authError")))


class ErrorReasonTests(unittest.TestCase):
    """What the operator log is allowed to keep from a refusal body."""

    def test_it_keeps_googles_reason_keywords(self) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": 403,
                    "message": "The video identified by the <code>videoId</code>...",
                    "errors": [
                        {"reason": "commentsDisabled", "domain": "youtube.commentThread"}
                    ],
                }
            }
        ).encode()
        self.assertEqual(_error_reasons(body), ("commentsDisabled",))

    def test_it_keeps_nothing_else_from_the_body(self) -> None:
        body = json.dumps(
            {"error": {"errors": [{"reason": "quotaExceeded", "message": "secret"}]}}
        ).encode()
        self.assertNotIn("secret", str(_error_reasons(body)))

    def test_an_unreadable_body_yields_nothing_instead_of_raising(self) -> None:
        for body in (b"", b"not json", b"[]", json.dumps({"error": {}}).encode()):
            with self.subTest(body=body):
                self.assertEqual(_error_reasons(body), ())


class AuthorizationUrlTests(unittest.TestCase):
    def build(self) -> GoogleAuthorizationGateway:
        return GoogleAuthorizationGateway(
            CONFIG, GoogleCredentialStore(), transport=FakeTransport({})
        )

    def test_the_url_targets_the_google_consent_endpoint(self) -> None:
        url = self.build().authorization_url(
            state=RedactedSecret("state-1"),
            code_challenge="challenge-1",
            redirect_uri_id="primary",
            scopes=APPROVED_SCOPES,
        )

        parts = urlsplit(url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.hostname, "accounts.google.com")
        self.assertIsNone(parts.port)

    def test_the_url_requests_an_offline_pkce_code_grant(self) -> None:
        url = self.build().authorization_url(
            state=RedactedSecret("state-1"),
            code_challenge="challenge-1",
            redirect_uri_id="primary",
            scopes=APPROVED_SCOPES,
        )

        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["challenge-1"])
        self.assertEqual(query["state"], ["state-1"])
        self.assertEqual(query["scope"], [APPROVED_SCOPES[0]])
        self.assertEqual(query["client_id"], [CONFIG.client_id])
        self.assertEqual(query["redirect_uri"], ["https://app.example/oauth/callback"])

    def test_an_unknown_redirect_uri_is_refused(self) -> None:
        with self.assertRaises(ProviderUnavailable):
            self.build().authorization_url(
                state=RedactedSecret("state-1"),
                code_challenge="challenge-1",
                redirect_uri_id="unregistered",
                scopes=APPROVED_SCOPES,
            )


class ExchangeTests(unittest.TestCase):
    def build(
        self, replies: dict[str, tuple[int, bytes]] | None = None
    ) -> tuple[GoogleAuthorizationGateway, FakeTransport, GoogleCredentialStore]:
        transport = FakeTransport(replies or default_replies())
        store = GoogleCredentialStore()
        return GoogleAuthorizationGateway(CONFIG, store, transport=transport), transport, store

    def exchange(self, gateway: GoogleAuthorizationGateway):
        return gateway.exchange_and_verify(
            code=RedactedSecret("auth-code"),
            code_verifier=RedactedSecret("verifier"),
            redirect_uri_id="primary",
        )

    def test_the_code_is_exchanged_with_the_verifier_and_client_secret(self) -> None:
        gateway, transport, _ = self.build()

        self.exchange(gateway)

        method, url, headers, _ = transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://oauth2.googleapis.com/token")
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        form = transport.sent_form(0)
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["auth-code"])
        self.assertEqual(form["code_verifier"], ["verifier"])
        self.assertEqual(form["client_secret"], ["client-secret"])
        self.assertEqual(form["redirect_uri"], ["https://app.example/oauth/callback"])

    def test_the_grant_carries_the_owner_channel_and_granted_scopes(self) -> None:
        gateway, _, _ = self.build()

        grant = self.exchange(gateway)

        self.assertEqual(grant.provider_channel_id, "UC_real")
        self.assertEqual(grant.channel_title, "本物チャンネル")
        self.assertEqual(grant.granted_scopes, APPROVED_SCOPES)
        self.assertIsNotNone(grant.credential.refresh_token)
        self.assertTrue(grant.subscriber_capability_verified)

    def test_a_denied_authorization_is_reported_as_provider_denied(self) -> None:
        replies = default_replies()
        replies["https://oauth2.googleapis.com/token"] = (
            400,
            b'{"error": "invalid_grant"}',
        )
        gateway, _, _ = self.build(replies)

        with self.assertRaises(ProviderRejected) as raised:
            self.exchange(gateway)

        self.assertEqual(raised.exception.reason.value, "PROVIDER_DENIED")

    def test_a_server_error_is_reported_as_unavailable(self) -> None:
        replies = default_replies()
        replies["https://oauth2.googleapis.com/token"] = (503, b"busy")
        gateway, _, _ = self.build(replies)

        with self.assertRaises(ProviderUnavailable):
            self.exchange(gateway)

    def test_an_unreadable_token_body_is_an_invalid_provider_response(self) -> None:
        replies = default_replies()
        replies["https://oauth2.googleapis.com/token"] = (200, b"not json")
        gateway, _, _ = self.build(replies)

        with self.assertRaises(ProviderRejected) as raised:
            self.exchange(gateway)

        self.assertEqual(raised.exception.reason.value, "INVALID_PROVIDER_RESPONSE")

    def test_a_missing_owner_channel_is_an_invalid_provider_response(self) -> None:
        replies = default_replies()
        replies["https://www.googleapis.com/youtube/v3/channels"] = (
            200,
            b'{"items": []}',
        )
        gateway, _, _ = self.build(replies)

        with self.assertRaises(ProviderRejected) as raised:
            self.exchange(gateway)

        self.assertEqual(raised.exception.reason.value, "INVALID_PROVIDER_RESPONSE")

    def test_a_forbidden_subscriber_list_clears_the_capability_flag(self) -> None:
        replies = default_replies()
        replies["https://www.googleapis.com/youtube/v3/subscriptions"] = (403, b"{}")
        gateway, _, _ = self.build(replies)

        grant = self.exchange(gateway)

        self.assertFalse(grant.subscriber_capability_verified)

    def test_a_grant_missing_the_approved_scope_is_refused(self) -> None:
        replies = default_replies()
        replies["https://oauth2.googleapis.com/token"] = token_response(
            scope="https://www.googleapis.com/auth/userinfo.profile"
        )
        gateway, _, _ = self.build(replies)

        with self.assertRaises(ProviderRejected) as raised:
            self.exchange(gateway)

        self.assertEqual(raised.exception.reason.value, "SCOPE_NOT_GRANTED")

    def test_a_grant_without_offline_access_reaches_the_service_unchanged(self) -> None:
        replies = default_replies()
        body = json.loads(token_response()[1])
        del body["refresh_token"]
        replies["https://oauth2.googleapis.com/token"] = (200, json.dumps(body).encode())
        gateway, _, _ = self.build(replies)

        grant = self.exchange(gateway)

        self.assertIsNone(grant.credential.refresh_token)


class RevocationTests(unittest.TestCase):
    def build(self, status: int) -> tuple[GoogleAuthorizationGateway, GoogleCredentialStore]:
        transport = FakeTransport({"https://oauth2.googleapis.com/revoke": (status, b"")})
        store = GoogleCredentialStore()
        store.put(
            "ws-1",
            "slot-1",
            ProviderCredential(
                access_token=RedactedSecret("at-1"),
                refresh_token=RedactedSecret("rt-1"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=APPROVED_SCOPES,
            ),
        )
        return GoogleAuthorizationGateway(CONFIG, store, transport=transport), store

    def test_an_accepted_revocation_is_confirmed(self) -> None:
        gateway, _ = self.build(200)

        self.assertIs(gateway.revoke("ws-1", "slot-1"), RevocationOutcome.REVOKED)

    def test_a_refused_revocation_is_not_confirmed(self) -> None:
        gateway, _ = self.build(500)

        self.assertIs(gateway.revoke("ws-1", "slot-1"), RevocationOutcome.NOT_CONFIRMED)

    def test_revoking_an_unknown_slot_is_not_confirmed(self) -> None:
        gateway, _ = self.build(200)

        self.assertIs(gateway.revoke("ws-1", "absent"), RevocationOutcome.NOT_CONFIRMED)

    def test_a_revoked_slot_keeps_no_credential(self) -> None:
        gateway, store = self.build(200)

        gateway.revoke("ws-1", "slot-1")

        self.assertEqual(store.slot_ids("ws-1"), ())


def stored_credential(expires_in_seconds: int = 3600) -> ProviderCredential:
    return ProviderCredential(
        access_token=RedactedSecret("at-1"),
        refresh_token=RedactedSecret("rt-1"),
        expires_at=NOW + timedelta(seconds=expires_in_seconds),
        scopes=APPROVED_SCOPES,
    )


class DataGatewayFixture(unittest.TestCase):
    def build(
        self,
        replies: dict[str, tuple[int, bytes]],
        credential: ProviderCredential | None = None,
    ) -> tuple[GoogleDataGateway, FakeTransport, GoogleCredentialStore]:
        transport = FakeTransport(replies)
        store = GoogleCredentialStore()
        if credential is not None:
            store.put("ws-1", "slot-1", credential)
        gateway = GoogleDataGateway(
            CONFIG, store, transport=transport, now=lambda: NOW, api_key=API_KEY
        )
        return gateway, transport, store


class PublicReadTests(DataGatewayFixture):
    """Comments are read with a key, not with the owner's grant.

    `commentThreads.list` refuses the read-only scope and wants
    `youtube.force-ssl`, which can also delete comments and manage the account.
    The owner is not asked for that to read what any visitor can read.
    """

    REPLY = (
        200,
        json.dumps(
            {
                "items": [
                    {
                        "snippet": {
                            "topLevelComment": {
                                "snippet": {
                                    "publishedAt": "2026-03-01T00:00:00Z",
                                    "authorChannelId": {"value": "UC_a"},
                                }
                            }
                        }
                    }
                ]
            }
        ).encode(),
    )

    def _read(self, gateway: GoogleDataGateway):
        return gateway.list_video_comment_authors(
            "ws-1", "slot-1", video_id="vid-1", page_token=None, max_results=50
        )

    def test_the_request_carries_the_key_and_not_the_owners_token(self) -> None:
        gateway, transport, _ = self.build({f"{API_ROOT}/commentThreads": self.REPLY})
        self._read(gateway)
        _, url, headers, _ = transport.requests[-1]
        self.assertEqual(parse_qs(urlsplit(url).query)["key"], [API_KEY])
        self.assertNotIn("Authorization", headers)

    def test_it_needs_no_credential_in_the_vault(self) -> None:
        gateway, _, store = self.build({f"{API_ROOT}/commentThreads": self.REPLY})
        self.assertIsNone(store._read("ws-1", "slot-1"))
        self.assertEqual(len(self._read(gateway).rows), 1)

    def test_a_refusal_here_never_reads_as_an_expired_grant(self) -> None:
        """A channel with comments off must not cost the owner the connection."""

        body = json.dumps(
            {"error": {"errors": [{"reason": "commentsDisabled"}]}}
        ).encode()
        gateway, _, _ = self.build({f"{API_ROOT}/commentThreads": (403, body)})
        with self.assertRaises(ProviderRejected):
            self._read(gateway)

    def test_without_a_key_it_refuses_instead_of_using_the_grant(self) -> None:
        transport = FakeTransport({f"{API_ROOT}/commentThreads": self.REPLY})
        gateway = GoogleDataGateway(
            CONFIG, GoogleCredentialStore(), transport=transport, now=lambda: NOW
        )
        with self.assertRaises(ProviderUnavailable):
            self._read(gateway)
        self.assertEqual(transport.requests, [])


class SubscriberListingTests(DataGatewayFixture):
    REPLY = (
        200,
        json.dumps(
            {
                "nextPageToken": "next-1",
                "items": [
                    {
                        "snippet": {"publishedAt": "2026-01-02T03:04:05Z"},
                        "subscriberSnippet": {
                            "channelId": "UC_sub_1",
                            "title": "視聴者1",
                        },
                    },
                    {"snippet": {}, "subscriberSnippet": {}},
                ],
            }
        ).encode(),
    )

    def test_the_owners_recent_subscribers_are_requested(self) -> None:
        gateway, transport, _ = self.build(
            {f"{API_ROOT}/subscriptions": self.REPLY}, stored_credential()
        )

        gateway.list_subscribers("ws-1", "slot-1", page_token="page-2", max_results=50)

        query = parse_qs(urlsplit(transport.requests[0][1]).query)
        self.assertEqual(query["myRecentSubscribers"], ["true"])
        self.assertEqual(query["maxResults"], ["50"])
        self.assertEqual(query["pageToken"], ["page-2"])
        self.assertEqual(transport.requests[0][2]["Authorization"], "Bearer at-1")

    def test_a_subscriber_row_carries_the_channel_title_and_date(self) -> None:
        gateway, _, _ = self.build(
            {f"{API_ROOT}/subscriptions": self.REPLY}, stored_credential()
        )

        page = gateway.list_subscribers(
            "ws-1", "slot-1", page_token=None, max_results=50
        )

        self.assertEqual(page.rows[0].subscriber_channel_id, "UC_sub_1")
        self.assertEqual(page.rows[0].title, "視聴者1")
        self.assertEqual(
            page.rows[0].api_published_at, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        )
        self.assertEqual(page.next_page_token, "next-1")

    def test_an_unreadable_subscriber_row_is_skipped(self) -> None:
        gateway, _, _ = self.build(
            {f"{API_ROOT}/subscriptions": self.REPLY}, stored_credential()
        )

        page = gateway.list_subscribers(
            "ws-1", "slot-1", page_token=None, max_results=50
        )

        self.assertEqual(len(page.rows), 1)


class VideoListingTests(DataGatewayFixture):
    CHANNEL_REPLY = (
        200,
        json.dumps(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU_real"}}}]}
        ).encode(),
    )
    ITEMS_REPLY = (
        200,
        json.dumps(
            {
                "items": [
                    {
                        "snippet": {
                            "title": "動画1",
                            "publishedAt": "2026-02-03T00:00:00Z",
                            "resourceId": {"videoId": "vid-1"},
                        }
                    }
                ]
            }
        ).encode(),
    )

    def replies(self) -> dict[str, tuple[int, bytes]]:
        return {
            f"{API_ROOT}/channels": self.CHANNEL_REPLY,
            f"{API_ROOT}/playlistItems": self.ITEMS_REPLY,
        }

    def test_videos_come_from_the_uploads_playlist(self) -> None:
        gateway, transport, _ = self.build(self.replies(), stored_credential())

        page = gateway.list_videos("ws-1", "slot-1", page_token=None, max_results=50)

        self.assertEqual(
            parse_qs(urlsplit(transport.requests[1][1]).query)["playlistId"],
            ["UU_real"],
        )
        self.assertEqual(page.rows[0].video_id, "vid-1")
        self.assertEqual(page.rows[0].title, "動画1")

    def test_the_uploads_playlist_is_resolved_once_per_slot(self) -> None:
        gateway, transport, _ = self.build(self.replies(), stored_credential())

        gateway.list_videos("ws-1", "slot-1", page_token=None, max_results=50)
        gateway.list_videos("ws-1", "slot-1", page_token="p2", max_results=50)

        channel_calls = [
            request for request in transport.requests if "/channels?" in request[1]
        ]
        self.assertEqual(len(channel_calls), 1)


class CommentAuthorTests(DataGatewayFixture):
    def reply(self) -> dict[str, tuple[int, bytes]]:
        def thread(author: str | None, published: str) -> dict[str, object]:
            snippet: dict[str, object] = {"publishedAt": published}
            if author is not None:
                snippet["authorChannelId"] = {"value": author}
            return {"snippet": {"topLevelComment": {"snippet": snippet}}}

        body = {
            "items": [
                thread("UC_sub_1", "2026-01-01T00:00:00Z"),
                thread("UC_sub_1", "2026-03-01T00:00:00Z"),
                thread(None, "2026-01-01T00:00:00Z"),
            ]
        }
        return {f"{API_ROOT}/commentThreads": (200, json.dumps(body).encode())}

    def test_repeat_comments_by_one_author_are_counted_once(self) -> None:
        gateway, _, _ = self.build(self.reply(), stored_credential())

        page = gateway.list_video_comment_authors(
            "ws-1", "slot-1", video_id="vid-1", page_token=None, max_results=50
        )

        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0].author_channel_id, "UC_sub_1")
        self.assertEqual(page.rows[0].comment_count, 2)

    def test_the_latest_comment_time_wins(self) -> None:
        gateway, _, _ = self.build(self.reply(), stored_credential())

        page = gateway.list_video_comment_authors(
            "ws-1", "slot-1", video_id="vid-1", page_token=None, max_results=50
        )

        self.assertEqual(
            page.rows[0].latest_comment_at, datetime(2026, 3, 1, tzinfo=UTC)
        )

    def test_an_anonymous_comment_author_is_skipped(self) -> None:
        gateway, _, _ = self.build(self.reply(), stored_credential())

        page = gateway.list_video_comment_authors(
            "ws-1", "slot-1", video_id="vid-1", page_token=None, max_results=50
        )

        self.assertEqual([row.author_channel_id for row in page.rows], ["UC_sub_1"])


class CredentialLifecycleTests(DataGatewayFixture):
    def replies(self) -> dict[str, tuple[int, bytes]]:
        return {
            TOKEN_ENDPOINT: token_response(access_token="at-2"),
            f"{API_ROOT}/subscriptions": (200, b'{"items": []}'),
        }

    def collect(self, gateway: GoogleDataGateway) -> None:
        gateway.list_subscribers("ws-1", "slot-1", page_token=None, max_results=50)

    def test_an_expiring_access_token_is_refreshed_before_the_call(self) -> None:
        gateway, transport, _ = self.build(self.replies(), stored_credential(10))

        self.collect(gateway)

        self.assertEqual(transport.requests[0][1], TOKEN_ENDPOINT)
        self.assertEqual(transport.sent_form(0)["grant_type"], ["refresh_token"])
        self.assertEqual(transport.requests[1][2]["Authorization"], "Bearer at-2")

    def test_a_refreshed_token_is_kept_for_the_next_call(self) -> None:
        gateway, transport, _ = self.build(self.replies(), stored_credential(10))

        self.collect(gateway)
        self.collect(gateway)

        refreshes = [
            request for request in transport.requests if request[1] == TOKEN_ENDPOINT
        ]
        self.assertEqual(len(refreshes), 1)

    def test_a_rejected_refresh_requires_reauthorization(self) -> None:
        replies = self.replies()
        replies[TOKEN_ENDPOINT] = (400, b'{"error": "invalid_grant"}')
        gateway, _, _ = self.build(replies, stored_credential(10))

        with self.assertRaises(ProviderAuthorizationExpired):
            self.collect(gateway)

    def test_a_rejected_access_token_requires_reauthorization(self) -> None:
        replies = self.replies()
        replies[f"{API_ROOT}/subscriptions"] = (401, b"{}")
        gateway, _, _ = self.build(replies, stored_credential())

        with self.assertRaises(ProviderAuthorizationExpired):
            self.collect(gateway)

    def test_a_missing_credential_requires_reauthorization(self) -> None:
        gateway, _, _ = self.build(self.replies())

        with self.assertRaises(ProviderAuthorizationExpired):
            self.collect(gateway)


if __name__ == "__main__":
    unittest.main()
