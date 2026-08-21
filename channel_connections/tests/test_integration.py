from __future__ import annotations

import pathlib
import unittest
from datetime import UTC, datetime

from channel_connections.errors import ChannelConnectionsError
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import (
    BeginAuthorization,
    ConnectionStatus,
    DisconnectConnection,
)
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeGateway,
    FixedClock,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
)
from channel_data.models import CollectionKind, StartCollection
from channel_data.service import ChannelDataService
from workspace_access.models import (
    CreateWorkspace,
    GrantMembership,
    Permission,
    Role,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceSelection,
)
from workspace_access.service import WorkspaceAccessService


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class RealContextIntegrationTests(unittest.TestCase):
    """Resolve genuine workspace contexts, then drive the connection lifecycle."""

    def setUp(self) -> None:
        self.access = WorkspaceAccessService()
        self.clock = FixedClock()
        self.gateway = FakeYouTubeGateway()
        self.vault = InMemoryCredentialVault()
        self.connections = ChannelConnectionsService(
            clock=self.clock,
            tokens=SequenceTokens(),
            gateway=self.gateway,
            ephemeral_secrets=RecordingEphemeralStore(InMemoryEphemeralSecretStore()),
            credential_vault=self.vault,
        )

    def session(self, subject: str):
        issued = self.access.establish_session(
            VerifiedIdentity(
                issuer="https://accounts.example",
                subject=subject,
                authenticated_at=datetime.now(UTC),
            )
        )
        return self.access.authenticate_session(SessionEvidence(secret=issued.secret))

    def owner_context(self, permission: Permission = Permission.CHANNEL_MANAGE_CONNECTION):
        session = self.session("owner-subject")
        created = self.access.create_workspace(session, CreateWorkspace(name="分析基地"))
        return session, self.access.resolve_workspace_context(
            session, WorkspaceSelection(workspace_id=created.workspace_id), permission
        )

    def test_an_owner_context_drives_the_whole_connection_lifecycle(self) -> None:
        _, owner = self.owner_context()

        self.connections.begin_authorization(
            owner, BeginAuthorization(idempotency_key="begin-1")
        )
        connection = self.connections.complete_authorization(
            owner, callback(self.gateway, idempotency_key="callback-1")
        )

        self.assertEqual(connection.workspace_id, owner.workspace_id)
        self.assertEqual(connection.status, ConnectionStatus.ACTIVE)
        self.assertEqual(len(self.vault.slot_ids(owner.workspace_id)), 1)

        self.connections.disconnect(
            owner,
            DisconnectConnection(
                connection_id=connection.connection_id, idempotency_key="disconnect-1"
            ),
        )
        self.assertEqual(self.vault.slot_ids(owner.workspace_id), ())

    def test_a_member_reads_status_but_cannot_manage_the_connection(self) -> None:
        owner_session, owner = self.owner_context()
        member_session = self.session("member-subject")
        manage_context = self.access.resolve_workspace_context(
            owner_session,
            WorkspaceSelection(workspace_id=owner.workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        self.access.grant_membership(
            manage_context,
            GrantMembership(
                user_id=member_session.user_id, role=Role.MEMBER, idempotency_key="m1"
            ),
        )
        member = self.access.resolve_workspace_context(
            member_session,
            WorkspaceSelection(workspace_id=owner.workspace_id),
            Permission.CHANNEL_READ,
        )

        self.connections.begin_authorization(
            owner, BeginAuthorization(idempotency_key="begin-1")
        )
        connection = self.connections.complete_authorization(
            owner, callback(self.gateway, idempotency_key="callback-1")
        )

        listed = self.connections.list_connections(member)
        self.assertEqual(
            [item.connection_id for item in listed.items], [connection.connection_id]
        )
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.connections.begin_authorization(
                member, BeginAuthorization(idempotency_key="begin-2")
            )
        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_a_published_connection_feeds_channel_data_by_opaque_ids_only(self) -> None:
        owner_session, owner = self.owner_context()
        self.connections.begin_authorization(
            owner, BeginAuthorization(idempotency_key="begin-1")
        )
        connection = self.connections.complete_authorization(
            owner, callback(self.gateway, idempotency_key="callback-1")
        )

        collection_context = self.access.resolve_workspace_context(
            owner_session,
            WorkspaceSelection(workspace_id=owner.workspace_id),
            Permission.COLLECTION_RUN,
        )
        data = ChannelDataService()
        state = data.start_collection(
            collection_context,
            StartCollection(
                channel_id=connection.provider_channel_id,
                collection_id="collection-1",
                kind=CollectionKind.SUBSCRIBERS,
                started_at=NOW,
                idempotency_key="collect-1",
            ),
        )

        self.assertEqual(state.channel_id, connection.provider_channel_id)
        self.assertEqual(state.workspace_id, owner.workspace_id)
        exported = repr(connection)
        for forbidden in ("synthetic-refresh-token", "synthetic-access-token", "cred_"):
            self.assertNotIn(forbidden, exported)

    def test_no_local_cli_oauth_file_is_read_by_this_module(self) -> None:
        import channel_connections.memory as memory
        import channel_connections.models as models
        import channel_connections.ports as ports
        import channel_connections.service as service

        for module in (memory, models, ports, service):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for forbidden in ("token.json", "client_secret", "InstalledAppFlow", "requests"):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
