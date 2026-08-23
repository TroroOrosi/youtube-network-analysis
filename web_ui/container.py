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

from .gcp import (
    FirestoreStateStore,
    MetadataToken,
    ScheduledCaller,
    SecretManagerStateStore,
)
from .demo_provider import DemoAuthorizationGateway, DemoDataGateway, DemoDataSet
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
    """Where the module documents rest, and the vault the credentials rest in.

    Firestore and a directory are alternatives, not layers: a host with a disk
    uses the directory, and a host without one — Cloud Run — uses Firestore.
    The credential document goes somewhere else entirely, to one Secret Manager
    secret, because a refresh token is not a session digest: the vault is
    encrypted at rest, granted to one service account on that one secret, and
    records every access. Nothing here is required, and with nothing configured
    the process starts clean.

    Both names are full resource paths — `projects/<project>/databases/<db>`
    and `projects/<project>/secrets/<secret>` — so no code has to ask the
    platform which project it is running in.
    """

    directory: Path | None = None
    database: str | None = None
    credential_secret: str | None = None

    def store(self, module: str) -> FileStateStore | FirestoreStateStore | None:
        if self.database is not None:
            return FirestoreStateStore(self.database, module, _gcp_token())
        if self.directory is not None:
            return FileStateStore(self.directory / f"{module}.json")
        return None

    def credential_store(self) -> SecretManagerStateStore | None:
        if self.credential_secret is None:
            return None
        return SecretManagerStateStore(self.credential_secret, _gcp_token())


@cache
def _gcp_token() -> MetadataToken:
    """One token holder for the process, so its cache is actually shared."""

    return MetadataToken()


def durability_from_env(environment: Mapping[str, str]) -> Durability:
    """Read where documents rest, refusing a shape that would drop the tokens.

    Somewhere to write and no vault is the one combination worth stopping for:
    the credential document would simply not be written, so every restart would
    ask every owner to authorize again while the deployment looked durable. Say
    so at the start rather than let that be discovered a restart at a time.
    """

    keep = Durability(
        directory=state_dir_from_env(environment),
        database=environment.get("YNA_FIRESTORE_DATABASE", "").strip() or None,
        credential_secret=environment.get("YNA_CREDENTIAL_SECRET", "").strip() or None,
    )
    if (keep.database or keep.directory) and keep.credential_secret is None:
        raise RuntimeError(
            "state is persisted but YNA_CREDENTIAL_SECRET is unset, so "
            "credentials would not be kept at all: name the secret, or "
            "persist nothing"
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
    # Absent unless a scheduler was configured, and the drain route refuses
    # every caller while it is: see `drain_caller_from_env`.
    drain_caller: ScheduledCaller | None = None


def drain_caller_from_env(
    environment: Mapping[str, str], base_url: str
) -> ScheduledCaller | None:
    """Read who may wake the drain, or nothing at all to keep it shut.

    Missing configuration is not a permissive default here. Without a service
    account there is nobody this deployment would believe, and the route refuses
    every caller rather than falling back to something easier — an endpoint that
    finishes other people's collections is not one to leave ajar.

    The audience defaults to this deployment's own drain URL, which is the value
    the scheduler job must be created with. Setting it explicitly is only for a
    deployment reached under a different name than it knows itself by.
    """

    account = environment.get("YNA_DRAIN_SERVICE_ACCOUNT", "").strip()
    if not account:
        return None
    audience = (
        environment.get("YNA_DRAIN_AUDIENCE", "").strip()
        or f"{base_url.rstrip('/')}/internal/drain"
    )
    return ScheduledCaller(account, audience)


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
    drain_caller: ScheduledCaller | None = None,
    demo_dataset: DemoDataSet | None = None,
) -> Services:
    """Wire every module, with demo gateways unless a real client is supplied.

    Without `google` the provider gateways are demo fakes: no request leaves
    this process and no real credential exists. With `google` the real adapters
    talk to Google under the same ports, and the credential vault is the one
    `durability` names — in memory unless that is a Secret Manager secret.

    With `state_dir` every module keeps its state across a restart, and with a
    `durability` that names a secret so do the credentials. A document this code
    cannot read raises here rather than starting empty.
    """

    keep = durability if durability is not None else Durability(directory=state_dir)
    clock = SystemClock()
    access = WorkspaceAccessService(
        clock=clock, state_store=_module_store(keep, "workspace_access")
    )
    login = None if google is None else GoogleLogin(google, transport=transport)
    if google is None:
        gateway = DemoAuthorizationGateway(base_url)
        data_gateway = DemoDataGateway(demo_dataset)
        vault = InMemoryCredentialVault()
        authorization_hosts = (urlsplit(base_url).hostname or "localhost",)
    else:
        store = GoogleCredentialStore(state_store=keep.credential_store())
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
        clock=clock,
        tokens=RandomTokens("an_"),
        channel_data=channel_data,
        state_store=_module_store(keep, "analysis_api"),
    )
    return Services(
        access=access,
        connections=connections,
        channel_data=channel_data,
        jobs=jobs,
        analysis=analysis,
        base_url=base_url,
        login=login,
        drain_caller=drain_caller,
    )
