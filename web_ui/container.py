"""Composition root for the reference web application."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
) -> Services:
    """Wire every module, with demo gateways unless a real client is supplied.

    Without `google` the provider gateways are demo fakes: no request leaves
    this process and no real credential exists. With `google` the real adapters
    talk to Google under the same ports; the credential vault is still the
    in-memory reference one, so a deployment must replace it with a KMS.
    """

    clock = SystemClock()
    access = WorkspaceAccessService(clock=clock)
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
    )
    channel_data = ChannelDataService(clock=clock)
    jobs = CollectionJobsService(
        clock=clock,
        tokens=RandomTokens("jb_"),
        broker=connections,
        connections=connections,
        channel_data=channel_data,
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
