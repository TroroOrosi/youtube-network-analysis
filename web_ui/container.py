"""Composition root for the reference web application."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from analysis_api.service import AnalysisApiService
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.service import (
    DEFAULT_REDIRECT_URI_ID,
    ChannelConnectionsService,
)
from channel_data.service import ChannelDataService
from collection_jobs.service import CollectionJobsService
from workspace_access.models import AccessSecret
from workspace_access.service import WorkspaceAccessService

from .gcp import GcsStateStore, KmsEnvelope, MetadataToken
from .demo_provider import DemoAuthorizationGateway, DemoDataGateway
from .google_login import LOGIN_REDIRECT_URI_ID, GoogleLogin
from .google_provider import (
    AUTHORIZATION_ENDPOINT,
    GoogleAuthorizationGateway,
    GoogleCredentialStore,
    GoogleDataGateway,
    GoogleOAuthConfig,
    Transport,
    http_transport,
)


class FileStateStore:
    """One module's state document, kept as one file.

    The write is a rename over the previous document, so a process killed
    mid-save leaves the old text intact instead of a truncated one that the
    next start would refuse to read.

    The directory and the file are asked for owner-only modes: they carry no
    credential, but they do carry session digests and who may reach which
    workspace. POSIX enforces those modes and Windows does not — there a file
    inherits the directory's ACL, so a Windows host has to restrict the
    directory itself. The deployment target is POSIX; see `web_ui/README.md`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def save(self, document: str) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending = self._path.with_suffix(".writing")
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
        os.replace(pending, self._path)


def state_dir_from_env(environment: Mapping[str, str]) -> Path | None:
    """Where the state documents live, or nothing to keep everything in memory.

    Unset means the reference behaviour a developer expects from a demo run:
    the process starts clean and leaves nothing behind.
    """

    directory = environment.get("YNA_STATE_DIR", "").strip()
    return Path(directory) if directory else None


def youtube_api_key_from_env(environment: Mapping[str, str]) -> str | None:
    """The key that reads public comments, or nothing to leave them uncollected.

    Comments need a key because `commentThreads.list` refuses the read-only
    scope this product asks owners for; see `GoogleDataGateway._list_public`.
    Without one, subscribers and videos still collect and the comment step
    fails on its own without touching the connection.
    """

    key = environment.get("YNA_YOUTUBE_API_KEY", "").strip()
    return key or None


@dataclass(frozen=True, slots=True)
class Durability:
    """Where documents rest, and the key that seals the one holding credentials.

    A bucket and a directory are alternatives, not layers: a host with a disk
    uses the directory, and a host without one — Cloud Run — uses the bucket.
    Neither is required, and with neither the process starts clean.

    `kms_key` is only for the credential document. The others hold session
    digests and who may reach which workspace, which the bucket's own encryption
    covers; a refresh token is worth the second lock, so that whoever can read
    the bucket still cannot use what is in that one file.
    """

    directory: Path | None = None
    bucket: str | None = None
    kms_key: str | None = None

    def store(self, module: str) -> FileStateStore | GcsStateStore | None:
        if self.bucket is not None:
            return GcsStateStore(self.bucket, f"{module}.json", _gcp_token())
        if self.directory is not None:
            return FileStateStore(self.directory / f"{module}.json")
        return None

    def envelope(self) -> KmsEnvelope | None:
        if self.kms_key is None:
            return None
        return KmsEnvelope(self.kms_key, _gcp_token())


@cache
def _gcp_token() -> MetadataToken:
    """One token holder for the process, so its cache is actually shared."""

    return MetadataToken()


def durability_from_env(environment: Mapping[str, str]) -> Durability:
    """Read where documents rest, refusing a shape that would keep tokens bare.

    Somewhere to write and no key is the one combination worth stopping for: the
    credential document would simply not be written, so every restart would ask
    every owner to authorize again while the deployment looked durable. Say so
    at the start rather than let that be discovered a restart at a time.
    """

    keep = Durability(
        directory=state_dir_from_env(environment),
        bucket=environment.get("YNA_STATE_BUCKET", "").strip() or None,
        kms_key=environment.get("YNA_KMS_KEY", "").strip() or None,
    )
    if (keep.bucket or keep.directory) and keep.kms_key is None:
        raise RuntimeError(
            "state is persisted but YNA_KMS_KEY is unset, so credentials would "
            "not be kept at all: set the key, or persist nothing"
        )
    return keep


def _module_store(keep: Durability, module: str):
    """One document per module, so the modules stay independently extractable."""

    return keep.store(module)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RandomTokens:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def new_token(self) -> str:
        return f"{self._prefix}{secrets.token_urlsafe(24)}"


@dataclass(frozen=True, slots=True)
class Services:
    access: WorkspaceAccessService
    connections: ChannelConnectionsService
    channel_data: ChannelDataService
    jobs: CollectionJobsService
    analysis: AnalysisApiService
    base_url: str
    login: GoogleLogin | None = None


def google_config_from_env(
    environment: Mapping[str, str], base_url: str
) -> GoogleOAuthConfig | None:
    """Read a registered OAuth client, or nothing to stay on the demo provider.

    Both halves must be present: a half-configured client would send owners to a
    real consent screen that cannot complete.

    `YNA_REQUIRE_GOOGLE=1` stops the fall-back instead of taking it. A URL that
    is closed to a list of Google test users is only closed while the client is
    configured; without one the demo door opens to whoever has the URL, and it
    opens without saying anything. Set it wherever the URL is reachable.
    """

    client_id = environment.get("YNA_GOOGLE_CLIENT_ID", "").strip()
    client_secret = environment.get("YNA_GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        if environment.get("YNA_REQUIRE_GOOGLE", "").strip() == "1":
            raise RuntimeError(
                "YNA_REQUIRE_GOOGLE is set, so the demo provider is refused: "
                "set both YNA_GOOGLE_CLIENT_ID and YNA_GOOGLE_CLIENT_SECRET"
            )
        return None
    return GoogleOAuthConfig(
        client_id=client_id,
        client_secret=AccessSecret(client_secret),
        redirect_uris={
            DEFAULT_REDIRECT_URI_ID: f"{base_url.rstrip('/')}/oauth/callback",
            LOGIN_REDIRECT_URI_ID: f"{base_url.rstrip('/')}/login/callback",
        },
    )


def build_services(
    base_url: str = "https://localhost",
    google: GoogleOAuthConfig | None = None,
    transport: Transport = http_transport,
    state_dir: Path | None = None,
    youtube_api_key: str | None = None,
    durability: Durability | None = None,
) -> Services:
    """Wire every module, with demo gateways unless a real client is supplied.

    Without `google` the provider gateways are demo fakes: no request leaves
    this process and no real credential exists. With `google` the real adapters
    talk to Google under the same ports; the credential vault is still the
    in-memory reference one, so a deployment must replace it with a KMS.

    With `state_dir` every module keeps its state across a restart. The
    credentials do not: the vault is still in memory, so a restored connection
    is listed but must be authorized again before it can collect. A document
    this code cannot read raises here rather than starting empty.
    """

    keep = durability if durability is not None else Durability(directory=state_dir)
    clock = SystemClock()
    access = WorkspaceAccessService(
        clock=clock, state_store=_module_store(keep, "workspace_access")
    )
    login = None if google is None else GoogleLogin(google, transport=transport)
    if google is None:
        gateway = DemoAuthorizationGateway(base_url)
        data_gateway = DemoDataGateway()
        vault = InMemoryCredentialVault()
        authorization_hosts = (urlsplit(base_url).hostname or "localhost",)
    else:
        store = GoogleCredentialStore(
            state_store=keep.store("google_credentials"),
            envelope=keep.envelope(),
        )
        gateway = GoogleAuthorizationGateway(google, store, transport=transport)
        data_gateway = GoogleDataGateway(
            google, store, transport=transport, api_key=youtube_api_key
        )
        vault = store
        authorization_hosts = (urlsplit(AUTHORIZATION_ENDPOINT).hostname,)
    connections = ChannelConnectionsService(
        clock=clock,
        tokens=RandomTokens("cx_"),
        gateway=gateway,
        ephemeral_secrets=InMemoryEphemeralSecretStore(),
        credential_vault=vault,
        data_gateway=data_gateway,
        authorization_hosts=authorization_hosts,
        state_store=_module_store(keep, "channel_connections"),
    )
    channel_data = ChannelDataService(
        clock=clock, state_store=_module_store(keep, "channel_data")
    )
    jobs = CollectionJobsService(
        clock=clock,
        tokens=RandomTokens("jb_"),
        broker=connections,
        connections=connections,
        channel_data=channel_data,
        state_store=_module_store(keep, "collection_jobs"),
    )
    analysis = AnalysisApiService(
        clock=clock, tokens=RandomTokens("an_"), channel_data=channel_data
    )
    return Services(
        access=access,
        connections=connections,
        channel_data=channel_data,
        jobs=jobs,
        analysis=analysis,
        base_url=base_url,
        login=login,
    )
