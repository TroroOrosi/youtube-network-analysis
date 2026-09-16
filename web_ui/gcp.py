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
import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
# Leave room below Firestore's 10 MiB API request limit, including JSON escapes.
MAX_COMMIT_BYTES = 8_000_000
MAX_COMMIT_WRITES = 200
GENERATION_FIELD = "generation"
DIGEST_FIELD = "sha256"
REQUEST_TIMEOUT_SECONDS = 20.0
TOKEN_REFRESH_MARGIN_SECONDS = 60

Transport = Callable[..., tuple[int, bytes]]


class GcpUnavailable(RuntimeError):
    """The platform did not answer. Never carries a credential."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    """Atomic state snapshots, including modules larger than one API request.

    Small legacy snapshots remain a single commit. Large snapshots stage
    immutable generation chunks in bounded commits, then publish ONE head.
    A failed stage cannot alter the previous head. A failed read never starts
    an empty application. Readers understand both the original layout and
    the generation manifest, and validate the latter's whole-text digest.

    Superseded chunks are reclaimed only after publication. A reader racing
    reclamation retries from the new head, never joins different generations.
    This is still a single-writer store, not multi-instance coordination.
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
        self._generation: str | None = None

    def load(self) -> str | None:
        for _ in range(3):
            head = self._read_fields(self._name(0))
            if head is None:
                return None
            parts = _parts_held(head.get(PARTS_FIELD))
            generation = self._generation_held(head)
            try:
                if generation is None:
                    pieces = [self._text(head)]
                    for index in range(1, parts):
                        pieces.append(self._text(self._read_fields(self._name(index))))
                else:
                    pieces = [
                        self._text(self._read_fields(self._name(index, generation)))
                        for index in range(parts)
                    ]
                document = "".join(pieces)
                if generation is not None:
                    digest = head.get(DIGEST_FIELD, {}).get("stringValue")
                    if digest != hashlib.sha256(document.encode()).hexdigest():
                        raise GcpUnavailable("state generation checksum mismatch")
            except GcpUnavailable:
                # A writer may have published and reclaimed the generation
                # we were reading. Only a CHANGED head justifies a retry.
                if generation is not None and self._read_fields(self._name(0)) != head:
                    continue
                raise
            self._parts, self._generation = parts, generation
            return document
        raise GcpUnavailable("state changed repeatedly during read")

    def save(self, document: str) -> None:
        pieces = _split(document, MAX_DOCUMENT_BYTES)
        legacy = [
            {"update": {"name": self._name(index),
                        "fields": self._fields(piece, index, len(pieces))}}
            for index, piece in enumerate(pieces)
        ]
        if self._generation is None:
            legacy += [{"delete": self._name(index)}
                       for index in range(len(pieces), self._parts)]
        old_names = self._old_names()
        if len(legacy) <= MAX_COMMIT_WRITES and len(self._body(legacy)) <= MAX_COMMIT_BYTES:
            self._publish(legacy)
            if self._generation is not None:
                self._cleanup(old_names)
            self._parts, self._generation = len(pieces), None
            return

        generation = uuid.uuid4().hex
        staged = [
            {"update": {"name": self._name(index, generation),
                        "fields": {DOCUMENT_FIELD: {"stringValue": piece}}}}
            for index, piece in enumerate(pieces)
        ]
        try:
            self._commit_batches(staged)
        except GcpUnavailable:
            # The head has NOT been attempted, so these new names cannot be
            # live, even if a staging response was lost after its commit.
            self._cleanup([self._name(index, generation) for index in range(len(pieces))])
            raise
        fields = {
            GENERATION_FIELD: {"stringValue": generation},
            PARTS_FIELD: {"integerValue": str(len(pieces))},
            DIGEST_FIELD: {"stringValue": hashlib.sha256(document.encode()).hexdigest()},
        }
        # Intentionally omit the legacy document field: old binaries must
        # fail closed, not mistake a manifest for an empty/partial dataset.
        # On an ambiguous publish failure, retain BOTH generations. Deleting
        # either could destroy the state a reader is actually using.
        self._publish([{"update": {"name": self._name(0), "fields": fields}}])
        self._parts, self._generation = len(pieces), generation
        self._cleanup(old_names)

    def _publish(self, writes: list[dict]) -> None:
        try:
            self._commit(writes)
        except GcpUnavailable as error:
            if error.status_code is not None and error.status_code < 500:
                # A definitive refusal (including authorization and quota)
                # cannot have published our snapshot. Do not issue more reads.
                raise
            # A timeout/503 can arrive AFTER an atomic commit. Confirm all
            # intended values before telling a domain service to roll back.
            # If confirmation itself fails, retain both generations and fail.
            for write in writes:
                update = write.get("update")
                name = update["name"] if update is not None else write["delete"]
                wanted = update["fields"] if update is not None else None
                if self._read_fields(name) != wanted:
                    raise

    def _old_names(self) -> list[str]:
        start = 0 if self._generation is not None else 1
        return [self._name(index, self._generation) for index in range(start, self._parts)]

    def _cleanup(self, names: list[str]) -> None:
        try:
            self._commit_batches([{"delete": name} for name in names])
        except GcpUnavailable:
            # Publication already succeeded, or these are unreferenced stage
            # files. Housekeeping must never turn success into a rollback.
            _LOG.warning("state chunk cleanup deferred for module %s", self._module)

    @staticmethod
    def _body(writes: list[dict]) -> bytes:
        return json.dumps({"writes": writes}, ensure_ascii=False, separators=(",", ":")).encode()

    def _commit_batches(self, writes: list[dict]) -> None:
        batch: list[dict] = []
        size = len(self._body([]))
        for write in writes:
            added = len(json.dumps(write, ensure_ascii=False, separators=(",", ":")).encode())
            if batch and (len(batch) >= MAX_COMMIT_WRITES or size + added + 1 > MAX_COMMIT_BYTES):
                self._commit(batch)
                batch, size = [], len(self._body([]))
            size += added + (1 if batch else 0)
            batch.append(write)
        if batch:
            self._commit(batch)

    def _commit(self, writes: list[dict]) -> None:
        body = self._body(writes)
        if len(body) > MAX_COMMIT_BYTES or len(writes) > MAX_COMMIT_WRITES:
            raise GcpUnavailable("state commit exceeds the bounded request size")
        status, _ = self._transport(
            "POST", f"{FIRESTORE_ROOT}/{self._documents}:commit",
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            body=body,
        )
        if status != 200:
            raise GcpUnavailable(
                f"writing {self._module} ({len(body)} bytes) answered {status}",
                status_code=status,
            )

    def _name(self, index: int, generation: str | None = None) -> str:
        if generation is not None:
            tail = f"{self._module}~g{generation}~{index}"
        else:
            tail = self._module if index == 0 else f"{self._module}~{index}"
        return f"{self._documents}/{STATE_COLLECTION}/{tail}"

    def _fields(self, piece: str, index: int, parts: int) -> dict[str, object]:
        fields: dict[str, object] = {DOCUMENT_FIELD: {"stringValue": piece}}
        if index == 0:
            fields[PARTS_FIELD] = {"integerValue": str(parts)}
        return fields

    @staticmethod
    def _generation_held(fields: Mapping) -> str | None:
        if GENERATION_FIELD not in fields:
            return None
        field = fields[GENERATION_FIELD]
        value = field.get("stringValue") if isinstance(field, Mapping) else None
        if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
            raise GcpUnavailable("state generation is malformed")
        digest_field = fields.get(DIGEST_FIELD)
        if not isinstance(digest_field, Mapping):
            raise GcpUnavailable("state generation has no checksum")
        return value

    def _text(self, fields: Mapping | None) -> str:
        field = fields.get(DOCUMENT_FIELD) if fields is not None else None
        text = field.get("stringValue") if isinstance(field, Mapping) else None
        if not isinstance(text, str):
            raise GcpUnavailable(f"{self._module} holds no complete document this wrote")
        return text

    def _read_fields(self, name: str) -> Mapping | None:
        status, body = self._transport(
            "GET", f"{FIRESTORE_ROOT}/{name}",
            headers={"Authorization": f"Bearer {self._token()}"}, body=None,
        )
        if status == 404:
            return None
        if status != 200:
            raise GcpUnavailable(f"reading {self._module} answered {status}")
        fields = _json(body).get("fields")
        if not isinstance(fields, Mapping):
            raise GcpUnavailable("state document fields are malformed")
        return fields


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
