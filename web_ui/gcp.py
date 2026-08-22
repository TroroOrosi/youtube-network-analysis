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
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
STATE_COLLECTION = "state"
DOCUMENT_FIELD = "document"
PARTS_FIELD = "parts"
MAX_DOCUMENT_BYTES = 900_000
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

        for name in self._enabled_versions():
            if name == keep:
                continue
            destroyed, _ = self._transport(
                "POST",
                f"{SECRET_MANAGER_ROOT}/{name}:destroy",
                headers=self._headers(sending=True),
                body=b"{}",
            )
            if destroyed != 200:
                _LOG.warning("destroying a superseded version answered %s", destroyed)

    def _enabled_versions(self) -> list[str]:
        """Every enabled version, read to the end before anything is destroyed.

        The listing is paged, and destroying versions as each page arrived
        would shorten the filtered result under the cursor, so the page after
        it would begin past entries never looked at. The whole list is cheap
        here — one save is meant to leave one version behind — and a page that
        is refused ends the walk with what it has, which really is superseded.
        """

        names: list[str] = []
        page_token: str | None = None
        while True:
            query = {"filter": "state:ENABLED"}
            if page_token is not None:
                query["pageToken"] = page_token
            status, body = self._transport(
                "GET",
                f"{SECRET_MANAGER_ROOT}/{self._secret}/versions"
                f"?{urllib.parse.urlencode(query)}",
                headers=self._headers(),
                body=None,
            )
            if status != 200:
                _LOG.warning("listing credential secret versions answered %s", status)
                return names
            payload = _json(body)
            versions = payload.get("versions")
            for version in versions if isinstance(versions, list) else []:
                name = version.get("name") if isinstance(version, Mapping) else None
                if isinstance(name, str):
                    names.append(name)
            following = payload.get("nextPageToken")
            if not isinstance(following, str) or not following:
                return names
            page_token = following


class FirestoreStateStore:
    """One module's state text, kept as a Firestore document and its parts.

    It satisfies the same `StateStore` port as the file store and is what a host
    without a disk uses instead. A write replaces the whole text or does not
    happen — every part goes in one commit, which Firestore applies as a unit —
    so a process killed mid-save leaves the previous text intact, the property
    the file store gets from a rename.

    A document is a single field holding the module's own text, so this store
    never learns what is inside and the modules stay independently extractable.
    Firestore caps one document a little under 1 MiB and `channel_data` grows
    with what was collected, so text past `MAX_DOCUMENT_BYTES` is split: the
    head document holds the first piece and how many pieces there are, and
    `module~1`, `module~2` ... hold the rest in order. A piece is cut on a
    character boundary, so each one is text a reader can decode on its own.
    What bounds a module is now the size of one commit, several megabytes,
    rather than the size of one document.

    A read that fails is raised, not swallowed: starting empty would show a live
    owner an unlinked channel and spend YouTube quota collecting data that is
    already there. A part the head counts but the database does not hold is such
    a failure too — text that stops early is not the text this wrote.
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
        self._documents = f"{database}/documents"
        self._token = token
        self._transport = transport
        self._parts = 1

    def load(self) -> str | None:
        head = self._read(0)
        if head is None:
            return None
        text, parts = head
        pieces = [text]
        for index in range(1, parts):
            part = self._read(index)
            if part is None:
                raise GcpUnavailable(f"{self._module} is missing part {index}")
            pieces.append(part[0])
        self._parts = parts
        return "".join(pieces)

    def save(self, document: str) -> None:
        pieces = _split(document, MAX_DOCUMENT_BYTES)
        writes: list[dict[str, object]] = [
            {
                "update": {
                    "name": self._name(index),
                    "fields": self._fields(piece, index, len(pieces)),
                }
            }
            for index, piece in enumerate(pieces)
        ]
        writes += [
            {"delete": self._name(index)}
            for index in range(len(pieces), self._parts)
        ]
        status, _ = self._transport(
            "POST",
            f"{FIRESTORE_ROOT}/{self._documents}:commit",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"writes": writes}, ensure_ascii=False).encode(),
        )
        if status != 200:
            raise GcpUnavailable(f"writing {self._module} answered {status}")
        self._parts = len(pieces)

    def _name(self, index: int) -> str:
        tail = self._module if index == 0 else f"{self._module}~{index}"
        return f"{self._documents}/{STATE_COLLECTION}/{tail}"

    def _fields(self, piece: str, index: int, parts: int) -> dict[str, object]:
        fields: dict[str, object] = {DOCUMENT_FIELD: {"stringValue": piece}}
        if index == 0:
            fields[PARTS_FIELD] = {"integerValue": str(parts)}
        return fields

    def _read(self, index: int) -> tuple[str, int] | None:
        status, body = self._transport(
            "GET",
            f"{FIRESTORE_ROOT}/{self._name(index)}",
            headers={"Authorization": f"Bearer {self._token()}"},
            body=None,
        )
        if status == 404:
            return None
        if status != 200:
            raise GcpUnavailable(f"reading {self._module} answered {status}")
        held = _json(body).get("fields")
        fields = held if isinstance(held, Mapping) else {}
        field = fields.get(DOCUMENT_FIELD)
        text = field.get("stringValue") if isinstance(field, Mapping) else None
        if not isinstance(text, str):
            raise GcpUnavailable(f"{self._module} holds no document this wrote")
        return text, _parts_held(fields.get(PARTS_FIELD))


class ScheduledCaller:
    """Decides whether a request really came from the scheduler job we made.

    Cloud Scheduler signs every call it makes with an OIDC token minted for one
    service account and one audience, and both halves have to be checked. The
    account says who woke us. The audience says the token was minted for this
    URL, so a token this deployment would accept cannot be replayed against
    another service that happens to trust the same account.

    Google checks the signature, not this code, because verifying one means
    fetching Google's keys and rotating them on their schedule, and being
    quietly wrong about that is the whole vulnerability. The token travels in a
    POST body rather than a query string so that it does not come to rest in
    anyone's request log.
    """

    def __init__(
        self,
        service_account: str,
        audience: str,
        *,
        transport: Transport = http_transport,
    ) -> None:
        self._service_account = service_account
        self._audience = audience
        self._transport = transport

    def verify(self, authorization: str | None) -> bool:
        """True only for a live token this deployment asked Google to accept.

        Every refusal looks the same from outside: a malformed header, a token
        for another audience and an unreachable Google all answer False, because
        the difference is only ever useful to someone probing the door.
        """

        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return False
        try:
            status, body = self._transport(
                "POST",
                TOKENINFO_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=urllib.parse.urlencode({"id_token": token}).encode(),
            )
            if status >= 400:
                return False
            claims = _json(body)
        except GcpUnavailable:
            return False
        return (
            claims.get("email") == self._service_account
            and claims.get("email_verified") in {"true", True}
            and claims.get("aud") == self._audience
            and claims.get("iss") in GOOGLE_ISSUERS
            and _expiry(claims) > time.time()
        )


def _expiry(claims: Mapping[str, object]) -> float:
    """Seconds since the epoch, or zero for anything that will not read as one."""

    try:
        return float(claims["exp"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return 0.0


def _split(text: str, limit: int) -> list[str]:
    """The text as pieces of at most `limit` bytes, each one decodable alone.

    Firestore measures a document in UTF-8 bytes and a Japanese title is three
    of them per character, so the cut is made on the encoded form and then
    walked back to a character boundary. Text that already fits is one piece,
    which is what every module's text is until a collection grows past the cap.
    """

    raw = text.encode()
    if len(raw) <= limit:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        pieces.append(raw[start:end].decode())
        start = end
    return pieces


def _parts_held(field: object) -> int:
    """How many documents the text was split across; one, before it ever was.

    A document written before this store could split reads as a single part,
    which is exactly what it is.
    """

    value = field.get("integerValue") if isinstance(field, Mapping) else None
    try:
        return max(1, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _json(body: bytes) -> Mapping[str, object]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise GcpUnavailable("response was not json") from error
    if not isinstance(payload, Mapping):
        raise GcpUnavailable("response was not an object")
    return payload
