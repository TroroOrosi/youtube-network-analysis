"""Google OpenID Connect sign-in for the reference web application.

Signing in is a separate grant from the YouTube one: it asks only for identity
scopes, keeps nothing after the exchange, and hands `workspace-access` a
`VerifiedIdentity` the provider vouched for instead of a name typed into a box.
Pending sign-ins live in this process only, so a restart cancels the ones in
flight rather than honouring a stale state later.
"""

from __future__ import annotations

import json
import secrets
from base64 import urlsafe_b64encode
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock
from urllib.parse import urlencode

from channel_connections.ports import ProviderUnavailable
from workspace_access.models import VerifiedIdentity

from .google_provider import (
    AUTHORIZATION_ENDPOINT,
    TOKEN_ENDPOINT,
    GoogleOAuthConfig,
    Transport,
    http_transport,
)

ISSUER = "https://accounts.google.com"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
LOGIN_REDIRECT_URI_ID = "login"
LOGIN_SCOPES = ("openid", "profile", "email")
STATE_TTL = timedelta(minutes=10)


class LoginFailed(Exception):
    """A sign-in did not complete. The reason is never shown to the browser.

    A refused consent, a replayed state and an unreachable provider all end
    here on purpose: telling the caller which one happened would answer
    questions only an attacker asks.
    """


class GoogleLogin:
    """Runs the authorization code flow that only proves who the owner is."""

    def __init__(
        self,
        config: GoogleOAuthConfig,
        *,
        redirect_uri_id: str = LOGIN_REDIRECT_URI_ID,
        transport: Transport = http_transport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._redirect_uri_id = redirect_uri_id
        self._transport = transport
        self._now = now
        self._pending: dict[str, tuple[str, datetime]] = {}
        self._lock = Lock()

    def start(self) -> tuple[str, str]:
        """Return the consent URL and the state that must come back with it."""

        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(32)
        deadline = self._now() + STATE_TTL
        with self._lock:
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if value[1] > self._now()
            }
            self._pending[state] = (verifier, deadline)
        query = urlencode(
            {
                "client_id": self._config.client_id,
                "redirect_uri": self._redirect_uri(),
                "response_type": "code",
                "scope": " ".join(LOGIN_SCOPES),
                "state": state,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "include_granted_scopes": "false",
                "prompt": "select_account",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}", state

    def complete(
        self, *, state: str, code: str | None, error: str | None
    ) -> VerifiedIdentity:
        """Turn a returned code into the identity Google vouches for."""

        verifier = self._take(state)
        if not code or error:
            raise LoginFailed("consent was not granted")
        tokens = self._json(
            "POST",
            TOKEN_ENDPOINT,
            body=urlencode(
                {
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret.reveal(),
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self._redirect_uri(),
                }
            ).encode(),
        )
        info = self._json(
            "GET", USERINFO_ENDPOINT, access_token=_string(tokens, "access_token")
        )
        email = info.get("email")
        verified = (
            email
            if info.get("email_verified") is True and isinstance(email, str) and email
            else None
        )
        name = info.get("name")
        subject = _string(info, "sub")
        return VerifiedIdentity(
            issuer=ISSUER,
            subject=subject,
            authenticated_at=self._now(),
            verified_email=verified,
            display_name=name if isinstance(name, str) and name else verified or subject,
        )

    def _redirect_uri(self) -> str:
        uri = self._config.redirect_uris.get(self._redirect_uri_id)
        if uri is None:
            raise LoginFailed("login redirect uri is not registered")
        return uri

    def _take(self, state: str) -> str:
        """Consume a pending sign-in. A state is good once and only once."""

        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending[1] <= self._now():
            raise LoginFailed("no sign-in is waiting for this state")
        return pending[0]

    def _json(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        access_token: str | None = None,
    ) -> Mapping[str, object]:
        headers = (
            {"Authorization": f"Bearer {access_token}"}
            if access_token
            else {"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            status, payload = self._transport(method, url, headers=headers, body=body)
        except ProviderUnavailable as failure:
            raise LoginFailed("the provider could not be reached") from failure
        if status >= 400:
            raise LoginFailed("the provider refused the sign-in")
        try:
            decoded = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as failure:
            raise LoginFailed("the provider response was unreadable") from failure
        if not isinstance(decoded, dict):
            raise LoginFailed("the provider response was unreadable")
        return decoded


def _challenge(verifier: str) -> str:
    digest = sha256(verifier.encode()).digest()
    return urlsafe_b64encode(digest).decode().rstrip("=")


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LoginFailed("the provider response was incomplete")
    return value
