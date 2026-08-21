"""Composition root for the reference web application."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
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


def _state_store(directory: Path | None, module: str) -> FileStateStore | None:
    """One document per module, so the modules stay independently extractable."""

    return None if directory is None else FileStateStore(directory / f"{module}.json")


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
    """

    client_id = environment.get("YNA_GOOGLE_CLIENT_ID", "").strip()
    client_secret = environment.get("YNA_GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
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

    clock = SystemClock()
    access = WorkspaceAccessService(
        clock=clock, state_store=_state_store(state_dir, "workspace_access")
    )
    login = None if google is None else GoogleLogin(google, transport=transport)
    if google is None:
        gateway = DemoAuthorizationGateway(base_url)
        data_gateway = DemoDataGateway()
        vault = InMemoryCredentialVault()
        authorization_hosts = (urlsplit(base_url).hostname or "localhost",)
    else:
        store = GoogleCredentialStore()
        gateway = GoogleAuthorizationGateway(google, store, transport=transport)
        data_gateway = GoogleDataGateway(google, store, transport=transport)
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
        state_store=_state_store(state_dir, "channel_connections"),
    )
    channel_data = ChannelDataService(
        clock=clock, state_store=_state_store(state_dir, "channel_data")
    )
    jobs = CollectionJobsService(
        clock=clock,
        tokens=RandomTokens("jb_"),
        broker=connections,
        connections=connections,
        channel_data=channel_data,
        state_store=_state_store(state_dir, "collection_jobs"),
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
