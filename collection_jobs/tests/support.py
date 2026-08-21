"""Deterministic full-stack fixtures for collection-jobs tests.

The stack is real: workspace contexts, the channel-connections service driven by
its synthetic gateways, the channel-data service, and the jobs service. Nothing
here contacts a network or holds a real credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import BeginAuthorization, ChannelConnection
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeDataGateway,
    FakeYouTubeGateway,
    FixedClock,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
    grant,
)
from channel_data.service import ChannelDataService
from collection_jobs.service import CollectionJobsService
from workspace_access.models import Permission, Role, WorkspaceContext


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
RUNNER_PERMISSIONS = (
    Permission.CHANNEL_READ,
    Permission.CHANNEL_MANAGE_CONNECTION,
    Permission.COLLECTION_RUN,
    Permission.COLLECTION_READ,
    Permission.ANALYSIS_READ,
    Permission.WORKSPACE_DELETE,
)


def context(
    workspace_id: str = "workspace-1",
    *permissions: Permission,
    user_id: str = "user-1",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        user_id=user_id,
        membership_id=f"membership-{workspace_id}-{user_id}",
        role=Role.OWNER,
        permissions=frozenset(permissions or RUNNER_PERMISSIONS),
        session_id=f"session-{workspace_id}-{user_id}",
        authorization_revision=1,
        resolved_at=NOW,
    )


@dataclass
class Stack:
    clock: FixedClock
    gateway: FakeYouTubeGateway
    data_gateway: FakeYouTubeDataGateway
    vault: InMemoryCredentialVault
    connections: ChannelConnectionsService
    channel_data: ChannelDataService
    jobs: CollectionJobsService

    def connect(
        self,
        actor: WorkspaceContext,
        *,
        provider_channel_id: str = "UC_channel_1",
        key: str = "connect-1",
    ) -> ChannelConnection:
        self.gateway.grant = grant(provider_channel_id=provider_channel_id)
        self.connections.begin_authorization(actor, BeginAuthorization(idempotency_key=key))
        return self.connections.complete_authorization(
            actor, callback(self.gateway, idempotency_key=key)
        )


def build_stack(
    *, daily_quota_units: int | None = None, page_size: int = 2
) -> Stack:
    clock = FixedClock(NOW)
    gateway = FakeYouTubeGateway()
    data_gateway = FakeYouTubeDataGateway()
    vault = InMemoryCredentialVault()
    connections = ChannelConnectionsService(
        clock=clock,
        tokens=SequenceTokens("conn"),
        gateway=gateway,
        ephemeral_secrets=RecordingEphemeralStore(InMemoryEphemeralSecretStore()),
        credential_vault=vault,
        data_gateway=data_gateway,
    )
    channel_data = ChannelDataService(clock=clock)
    jobs_kwargs: dict[str, int] = {"page_size": page_size}
    if daily_quota_units is not None:
        jobs_kwargs["daily_quota_units"] = daily_quota_units
    jobs = CollectionJobsService(
        clock=clock,
        tokens=SequenceTokens("job"),
        broker=connections,
        connections=connections,
        channel_data=channel_data,
        **jobs_kwargs,
    )
    return Stack(
        clock=clock,
        gateway=gateway,
        data_gateway=data_gateway,
        vault=vault,
        connections=connections,
        channel_data=channel_data,
        jobs=jobs,
    )
