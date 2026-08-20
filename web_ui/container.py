"""Composition root for the reference web application."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from analysis_api.service import AnalysisApiService
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.service import ChannelConnectionsService
from channel_data.service import ChannelDataService
from collection_jobs.service import CollectionJobsService
from workspace_access.service import WorkspaceAccessService

from .demo_provider import DemoAuthorizationGateway, DemoDataGateway


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


def build_services(base_url: str = "https://localhost") -> Services:
    """Wire every module with the synthetic provider gateways.

    The provider gateways are demo fakes: no request leaves this process and no
    real credential exists. Production requires approved provider adapters and a
    managed credential vault behind the same ports.
    """

    clock = SystemClock()
    host = urlsplit(base_url).hostname or "localhost"
    access = WorkspaceAccessService(clock=clock)
    connections = ChannelConnectionsService(
        clock=clock,
        tokens=RandomTokens("cx_"),
        gateway=DemoAuthorizationGateway(base_url),
        ephemeral_secrets=InMemoryEphemeralSecretStore(),
        credential_vault=InMemoryCredentialVault(),
        data_gateway=DemoDataGateway(),
        authorization_hosts=(host,),
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
    )
