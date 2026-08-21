from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from web_ui.google_login import GoogleLogin, LoginFailed
from channel_connections.models import RedactedSecret

from web_ui.google_provider import TOKEN_ENDPOINT, GoogleOAuthConfig
from web_ui.tests.test_google_provider import FakeTransport, token_response


NOW = datetime(2026, 6, 1, tzinfo=UTC)

CONFIG = GoogleOAuthConfig(
    client_id="client-123.apps.googleusercontent.com",
    client_secret=RedactedSecret("client-secret"),
    redirect_uris={"login": "https://app.example/login/callback"},
)

USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


def userinfo(**overrides: object) -> tuple[int, bytes]:
    body = {
        "sub": "1234567890",
        "name": "運用担当",
        "email": "owner@example.com",
        "email_verified": True,
    }
    body.update(overrides)
    return 200, json.dumps(body).encode()


def login_replies(**overrides: tuple[int, bytes]) -> dict[str, tuple[int, bytes]]:
    replies = {TOKEN_ENDPOINT: token_response(scope=None), USERINFO_ENDPOINT: userinfo()}
    replies.update(overrides)
    return replies


class LoginFixture(unittest.TestCase):
    def build(
        self, replies: dict[str, tuple[int, bytes]] | None = None, now: datetime = NOW
    ) -> tuple[GoogleLogin, FakeTransport]:
        transport = FakeTransport(replies or login_replies())
        self.clock = now
        login = GoogleLogin(
            CONFIG, redirect_uri_id="login", transport=transport, now=lambda: self.clock
        )
        return login, transport


class LoginStartTests(LoginFixture):
    def test_the_owner_is_sent_to_google_with_an_identity_scope(self) -> None:
        login, _ = self.build()

        url, _ = login.start()

        query = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).hostname, "accounts.google.com")
        self.assertIn("openid", query["scope"][0].split())
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(
            query["redirect_uri"], ["https://app.example/login/callback"]
        )

    def test_no_youtube_scope_is_requested_to_sign_in(self) -> None:
        login, _ = self.build()

        url, _ = login.start()

        self.assertNotIn("youtube", parse_qs(urlsplit(url).query)["scope"][0])

    def test_each_start_issues_a_different_state(self) -> None:
        login, _ = self.build()

        first, second = login.start()[1], login.start()[1]

        self.assertNotEqual(first, second)


class LoginCompletionTests(LoginFixture):
    def test_the_signed_in_identity_comes_from_google(self) -> None:
        login, _ = self.build()
        _, state = login.start()

        identity = login.complete(state=state, code="auth-code", error=None)

        self.assertEqual(identity.issuer, "https://accounts.google.com")
        self.assertEqual(identity.subject, "1234567890")
        self.assertEqual(identity.display_name, "運用担当")
        self.assertEqual(identity.verified_email, "owner@example.com")
        self.assertEqual(identity.authenticated_at, NOW)

    def test_an_unverified_email_is_not_carried(self) -> None:
        login, _ = self.build(login_replies(**{USERINFO_ENDPOINT: userinfo(email_verified=False)}))
        _, state = login.start()

        identity = login.complete(state=state, code="auth-code", error=None)

        self.assertIsNone(identity.verified_email)

    def test_the_code_is_exchanged_with_the_stored_verifier(self) -> None:
        login, transport = self.build()
        url, state = login.start()

        login.complete(state=state, code="auth-code", error=None)

        form = transport.sent_form(0)
        challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["auth-code"])
        self.assertNotEqual(form["code_verifier"], [challenge])

    def test_a_state_cannot_be_used_twice(self) -> None:
        login, _ = self.build()
        _, state = login.start()
        login.complete(state=state, code="auth-code", error=None)

        with self.assertRaises(LoginFailed):
            login.complete(state=state, code="auth-code", error=None)

    def test_an_unknown_state_is_refused(self) -> None:
        login, _ = self.build()
        login.start()

        with self.assertRaises(LoginFailed):
            login.complete(state="invented", code="auth-code", error=None)

    def test_an_expired_state_is_refused(self) -> None:
        login, _ = self.build()
        _, state = login.start()
        self.clock = NOW + timedelta(minutes=11)

        with self.assertRaises(LoginFailed):
            login.complete(state=state, code="auth-code", error=None)

    def test_a_refused_consent_is_refused(self) -> None:
        login, _ = self.build()
        _, state = login.start()

        with self.assertRaises(LoginFailed):
            login.complete(state=state, code=None, error="access_denied")

    def test_a_provider_failure_is_refused(self) -> None:
        login, _ = self.build(login_replies(**{TOKEN_ENDPOINT: (503, b"busy")}))
        _, state = login.start()

        with self.assertRaises(LoginFailed):
            login.complete(state=state, code="auth-code", error=None)

    def test_an_identity_without_a_subject_is_refused(self) -> None:
        login, _ = self.build(login_replies(**{USERINFO_ENDPOINT: userinfo(sub="")}))
        _, state = login.start()

        with self.assertRaises(LoginFailed):
            login.complete(state=state, code="auth-code", error=None)

    def test_a_failed_attempt_does_not_keep_the_state_alive(self) -> None:
        login, _ = self.build(login_replies(**{TOKEN_ENDPOINT: (503, b"busy")}))
        _, state = login.start()
        with self.assertRaises(LoginFailed):
            login.complete(state=state, code="auth-code", error=None)

        with self.assertRaises(LoginFailed):
            login.complete(state=state, code="auth-code", error=None)


if __name__ == "__main__":
    unittest.main()
