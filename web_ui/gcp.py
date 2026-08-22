"""Google Cloud adapters: an identity, a vault, a place to put documents.

Cloud Run gives a container no disk and no lasting memory, so a deployment that
must survive a restart needs both stores below. They speak REST over the same
transport the provider adapters use, which keeps the dependency list where it
is. Both are named by their full resource path, so nothing here has to ask the
platform which project it is running in.

Both also sit inside Google's always-free allowances, which is why they are
these two and not a key ring and a bucket: a key version and a bucket in the
wrong region are a monthly bill and a cross-region round trip for a deployment
that otherwise costs nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

_LOG = logging.getLogger(__name__)

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
SECRET_MANAGER_ROOT = "https://secretmanager.googleapis.com/v1"
FIRESTORE_ROOT = "https://firestore.googleapis.com/v1"
STATE_COLLECTION = "state"
DOCUMENT_FIELD = "document"
REQUEST_TIMEOUT_SECONDS = 20.0
TOKEN_REFRESH_MARGIN_SECONDS = 60

Transport = Callable[..., tuple[int, bytes]]


class GcpUnavailable(RuntimeError):
    """The platform did not answer. Never carries a credential."""


def http_transport(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GcpUnavailable("transport failed") from error


class MetadataToken:
    """The runtime service account's access token, from the metadata server.

    Held until a minute before it expires. Nothing else in this process ever
    holds a Google credential for the deployment itself; the owners' credentials
    are a different thing entirely and are what the vault below is for.
    """

    def __init__(
        self,
        *,
        transport: Transport = http_transport,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0

    def __call__(self) -> str:
        if self._token is not None and self._now() < self._expires_at:
            return self._token
        status, body = self._transport(
            "GET", METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, body=None
        )
        if status != 200:
            raise GcpUnavailable(f"metadata server answered {status}")
        payload = _json(body)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not isinstance(expires_in, int):
            raise GcpUnavailable("metadata server returned no usable token")
        self._token = token
        self._expires_at = self._now() + expires_in - TOKEN_REFRESH_MARGIN_SECONDS
        return token


class SecretManagerStateStore:
    """The credential document, kept as versions of one secret.

    This is the vault the owners' refresh tokens were always supposed to live
    in: encrypted at rest by the platform, reachable only by the principals
    named on this one secret, and every access recorded. The process holds no
    key of its own, so there is no key here to lose.

    `secret` is the full resource name, `projects/<project>/secrets/<secret>`.
    The deployment creates it and grants the roles; this code only adds and
    destroys versions.

    A save adds a version and destroys the ones it replaced. Versions still
    enabled are what the free tier counts, and a superseded refresh token stays
    a live secret for as long as it can be read, so both reasons say destroy
    rather than disable. It stays inside the free tier only because
    `GoogleCredentialStore` does not rewrite a slot whose refresh token has not
    changed — the hourly refresh would otherwise add a version an hour.
    """

    def __init__(
        self,
        secret: str,
        token: Callable[[], str],
        *,
        transport: Transport = http_transport,
    ) -> None:
        self._secret = secret
        self._token = token
        self._transport = transport

    def _headers(self, *, sending: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if sending:
            headers["Content-Type"] = "application/json"
        return headers

    def load(self) -> str | None:
        """The latest enabled version, or nothing before the first save.

        A secret with no readable version answers 404, and a deployment may also
        have created the secret with an empty first version; both mean the same
        thing here and read as nothing. Any other refusal is raised, because a
        version that exists and cannot be read is not an empty vault.
        """

        status, body = self._transport(
            "GET",
            f"{SECRET_MANAGER_ROOT}/{self._secret}/versions/latest:access",
            headers=self._headers(),
            body=None,
        )
        if status == 404:
            return None
        if status != 200:
            raise GcpUnavailable(f"reading the credential secret answered {status}")
        payload = _json(body).get("payload")
        if not isinstance(payload, Mapping):
            raise GcpUnavailable("secret manager returned no payload")
        data = payload.get("data", "")
        if not isinstance(data, str):
            raise GcpUnavailable("secret manager returned an unreadable payload")
        try:
            return base64.b64decode(data).decode("utf-8") or None
        except (ValueError, UnicodeDecodeError) as error:
            raise GcpUnavailable("stored credential document is not text") from error

    def save(self, document: str) -> None:
        status, body = self._transport(
            "POST",
            f"{SECRET_MANAGER_ROOT}/{self._secret}:addVersion",
            headers=self._headers(sending=True),
            body=json.dumps(
                {"payload": {"data": base64.b64encode(document.encode()).decode()}}
            ).encode(),
        )
        if status != 200:
            raise GcpUnavailable(f"writing the credential secret answered {status}")
        added = _json(body).get("name")
        if not isinstance(added, str):
            raise GcpUnavailable("secret manager returned no version name")
        self._destroy_superseded(added)

    def _destroy_superseded(self, keep: str) -> None:
        """Destroy every other enabled version, and never fail the save for it.

        The credential is already stored by the time this runs, so a refusal
        here means one extra readable version, not a lost token — and the next
        save lists again and clears it. Raising instead would fail an owner's
        connection over housekeeping.
        """

        query = urllib.parse.urlencode({"filter": "state:ENABLED"})
        status, body = self._transport(
            "GET",
            f"{SECRET_MANAGER_ROOT}/{self._secret}/versions?{query}",
            headers=self._headers(),
            body=None,
        )
        if status != 200:
            _LOG.warning("listing credential secret versions answered %s", status)
            return
        versions = _json(body).get("versions")
        for version in versions if isinstance(versions, list) else []:
            name = version.get("name") if isinstance(version, Mapping) else None
            if not isinstance(name, str) or name == keep:
                continue
            destroyed, _ = self._transport(
                "POST",
                f"{SECRET_MANAGER_ROOT}/{name}:destroy",
                headers=self._headers(sending=True),
                body=b"{}",
            )
            if destroyed != 200:
                _LOG.warning("destroying a superseded version answered %s", destroyed)


class FirestoreStateStore:
    """One module's state document, kept as one Firestore document.

    It satisfies the same `StateStore` port as the file store and is what a host
    without a disk uses instead. A write replaces the whole document or does not
    happen, so a process killed mid-save leaves the previous text intact — the
    property the file store gets from a rename.

    The document is a single field holding the module's own text, so this store
    never learns what is inside and the modules stay independently extractable.
    Firestore caps one document a little under 1 MiB; `channel_data` is the one
    whose text grows with what was collected, and it is the one that would meet
    that ceiling first.

    A read that fails is raised, not swallowed: starting empty would show a live
    owner an unlinked channel and spend YouTube quota collecting data that is
    already there.
    """

    def __init__(
        self,
        database: str,
        module: str,
        token: Callable[[], str],
        *,
        transport: Transport = http_transport,
    ) -> None:
        self._module = module
        self._url = (
            f"{FIRESTORE_ROOT}/{database}/documents/{STATE_COLLECTION}/{module}"
        )
        self._token = token
        self._transport = transport

    def load(self) -> str | None:
        status, body = self._transport(
            "GET",
            self._url,
            headers={"Authorization": f"Bearer {self._token()}"},
            body=None,
        )
        if status == 404:
            return None
        if status != 200:
            raise GcpUnavailable(f"reading {self._module} answered {status}")
        fields = _json(body).get("fields")
        field = fields.get(DOCUMENT_FIELD) if isinstance(fields, Mapping) else None
        text = field.get("stringValue") if isinstance(field, Mapping) else None
        if not isinstance(text, str):
            raise GcpUnavailable(f"{self._module} holds no document this wrote")
        return text

    def save(self, document: str) -> None:
        status, _ = self._transport(
            "PATCH",
            self._url,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            body=json.dumps(
                {"fields": {DOCUMENT_FIELD: {"stringValue": document}}}
            ).encode(),
        )
        if status != 200:
            raise GcpUnavailable(f"writing {self._module} answered {status}")


def _json(body: bytes) -> Mapping[str, object]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise GcpUnavailable("response was not json") from error
    if not isinstance(payload, Mapping):
        raise GcpUnavailable("response was not an object")
    return payload
