"""Google Cloud adapters: an identity, a place to put documents, a key.

Cloud Run gives a container no disk and no lasting memory, so a deployment that
must survive a restart needs both of these. They speak REST over the same
transport the provider adapters use, which keeps the dependency list where it
is.
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
STORAGE_ROOT = "https://storage.googleapis.com"
KMS_ROOT = "https://cloudkms.googleapis.com/v1"
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
    are a different thing entirely and are the reason the key below exists.
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


class GcsStateStore:
    """One module's state document, kept as one object in a bucket.

    It satisfies the same `StateStore` port as the file store and is what a
    host without a disk uses instead. An object write replaces the whole object
    or does not happen, so a process killed mid-save leaves the previous
    document intact — the property the file store gets from a rename.

    A bucket read that fails is raised, not swallowed: starting empty would show
    a live owner an unlinked channel and spend YouTube quota collecting data
    that is already there.
    """

    def __init__(
        self,
        bucket: str,
        object_name: str,
        token: Callable[[], str],
        *,
        transport: Transport = http_transport,
    ) -> None:
        self._bucket = bucket
        self._object = object_name
        self._token = token
        self._transport = transport

    def _url(self, *, upload: bool) -> str:
        quoted = urllib.parse.quote(self._object, safe="")
        if upload:
            query = urllib.parse.urlencode(
                {"uploadType": "media", "name": self._object}
            )
            return f"{STORAGE_ROOT}/upload/storage/v1/b/{self._bucket}/o?{query}"
        return f"{STORAGE_ROOT}/storage/v1/b/{self._bucket}/o/{quoted}?alt=media"

    def load(self) -> str | None:
        status, body = self._transport(
            "GET",
            self._url(upload=False),
            headers={"Authorization": f"Bearer {self._token()}"},
            body=None,
        )
        if status == 404:
            return None
        if status != 200:
            raise GcpUnavailable(f"reading {self._object} answered {status}")
        return body.decode("utf-8")

    def save(self, document: str) -> None:
        status, _ = self._transport(
            "POST",
            self._url(upload=True),
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=document.encode("utf-8"),
        )
        if status not in (200, 201):
            raise GcpUnavailable(f"writing {self._object} answered {status}")


class KmsEnvelope:
    """Encrypts with a key this process cannot read and cannot export.

    The credential document is written where a state document is written, so
    whoever can read that bucket can read the document. This is what stops that
    being the same as reading the owners' refresh tokens: the bytes there are
    ciphertext, and turning them back needs `cloudkms.cryptoKeyVersions.useToDecrypt`
    on one key, which is a second permission granted to one service account.

    The key never leaves Google. Rotating it is a KMS operation and needs no
    change here: KMS records which version sealed each message and uses it to
    open it.
    """

    def __init__(
        self,
        key_name: str,
        token: Callable[[], str],
        *,
        transport: Transport = http_transport,
    ) -> None:
        self._key = key_name
        self._token = token
        self._transport = transport

    def _call(self, verb: str, field: str, value: bytes) -> bytes:
        status, body = self._transport(
            "POST",
            f"{KMS_ROOT}/{self._key}:{verb}",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            body=json.dumps({field: base64.b64encode(value).decode()}).encode(),
        )
        if status != 200:
            # The body can quote the request; the status alone says enough.
            _LOG.warning("kms %s answered %s", verb, status)
            raise GcpUnavailable(f"kms {verb} answered {status}")
        answer = _json(body)
        sealed = answer.get("ciphertext" if verb == "encrypt" else "plaintext")
        if not isinstance(sealed, str):
            raise GcpUnavailable(f"kms {verb} returned no usable payload")
        return base64.b64decode(sealed)

    def encrypt(self, plaintext: bytes) -> str:
        return base64.b64encode(self._call("encrypt", "plaintext", plaintext)).decode()

    def decrypt(self, sealed: str) -> bytes:
        try:
            raw = base64.b64decode(sealed, validate=True)
        except (ValueError, TypeError) as error:
            raise GcpUnavailable("stored ciphertext is not readable") from error
        return self._call("decrypt", "ciphertext", raw)


def _json(body: bytes) -> Mapping[str, object]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise GcpUnavailable("response was not json") from error
    if not isinstance(payload, Mapping):
        raise GcpUnavailable("response was not an object")
    return payload
