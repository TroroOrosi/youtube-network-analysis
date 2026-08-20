from __future__ import annotations

import unittest
from datetime import timedelta

from channel_connections.errors import ChannelConnectionsError
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import (
    BeginAuthorization,
    BeginReauthorization,
    ConnectionPageRequest,
    DisconnectConnection,
    ReportCredentialInvalidation,
)
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeGateway,
    FixedClock,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
    context,
    grant,
)


class CollidingTokens:
    """Worst-case generator: every identifier collides across workspaces."""

    def new_token(self) -> str:
        return "shared-0001-" + "x" * 32


class TenantIsolationFixture(unittest.TestCase):
    tokens: object = SequenceTokens()

    def setUp(self) -> None:
        self.clock = FixedClock()
        self.gateway = FakeYouTubeGateway()
        self.ephemeral = RecordingEphemeralStore(InMemoryEphemeralSecretStore())
        self.vault = InMemoryCredentialVault()
        self.service = ChannelConnectionsService(
            clock=self.clock,
            tokens=self.new_tokens(),
            gateway=self.gateway,
            ephemeral_secrets=self.ephemeral,
            credential_vault=self.vault,
        )
        self.first = context("workspace-1")
        self.second = context("workspace-2")

    def new_tokens(self) -> object:
        return SequenceTokens()

    def connect(self, actor, *, channel: str = "UC_channel_1", key: str = "shared-key"):
        self.gateway.grant = grant(provider_channel_id=channel)
        self.service.begin_authorization(actor, BeginAuthorization(idempotency_key=key))
        return self.service.complete_authorization(
            actor, callback(self.gateway, idempotency_key=key)
        )


class SharedIdentifierTests(TenantIsolationFixture):
    def new_tokens(self) -> object:
        return CollidingTokens()

    def test_identical_connection_ids_stay_independent(self) -> None:
        first = self.connect(self.first)
        second = self.connect(self.second)

        self.assertEqual(first.connection_id, second.connection_id)
        self.assertEqual(first.workspace_id, "workspace-1")
        self.assertEqual(second.workspace_id, "workspace-2")
        self.assertEqual(
            self.service.get_connection(self.first, first.connection_id).workspace_id,
            "workspace-1",
        )
        self.assertEqual(
            self.service.get_connection(self.second, first.connection_id).workspace_id,
            "workspace-2",
        )

    def test_identical_credential_slots_are_stored_per_workspace(self) -> None:
        self.connect(self.first)
        self.connect(self.second)

        self.assertEqual(
            self.vault.slot_ids("workspace-1"), self.vault.slot_ids("workspace-2")
        )
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)

    def test_disconnect_leaves_the_identically_named_foreign_records(self) -> None:
        first = self.connect(self.first)
        second = self.connect(self.second)

        self.service.disconnect(
            self.first,
            DisconnectConnection(
                connection_id=first.connection_id, idempotency_key="shared-key"
            ),
        )

        self.assertEqual(self.vault.slot_ids("workspace-1"), ())
        self.assertEqual(len(self.vault.slot_ids("workspace-2")), 1)
        self.assertEqual(
            self.service.get_connection(self.second, second.connection_id), second
        )
        with self.assertRaises(ChannelConnectionsError):
            self.service.get_connection(self.first, first.connection_id)

    def test_identical_idempotency_keys_do_not_collide(self) -> None:
        first = self.connect(self.first)
        second = self.connect(self.second)

        self.assertNotEqual(first.workspace_id, second.workspace_id)
        self.assertEqual(len(self.gateway.exchanges), 2)
        self.assertEqual(len(self.service._state.connections), 2)


class CrossWorkspaceAccessTests(TenantIsolationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owned = self.connect(self.first, key="begin-1")
        self.clock.advance(timedelta(minutes=1))
        self.foreign = self.connect(self.second, channel="UC_channel_2", key="begin-2")

    def test_every_management_operation_hides_a_foreign_connection(self) -> None:
        operations = (
            lambda: self.service.get_connection(self.second, self.owned.connection_id),
            lambda: self.service.disconnect(
                self.second,
                DisconnectConnection(
                    connection_id=self.owned.connection_id, idempotency_key="x"
                ),
            ),
            lambda: self.service.begin_reauthorization(
                self.second,
                BeginReauthorization(
                    connection_id=self.owned.connection_id, idempotency_key="x"
                ),
            ),
            lambda: self.service.report_credential_invalidation(
                self.second,
                ReportCredentialInvalidation(
                    connection_id=self.owned.connection_id, idempotency_key="x"
                ),
            ),
        )
        messages = set()
        for operation in operations:
            with self.assertRaises(ChannelConnectionsError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "CONNECTION_NOT_FOUND_OR_FORBIDDEN")
            messages.add(raised.exception.message)

        self.assertEqual(len(messages), 1)
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)

    def test_listing_never_crosses_the_boundary(self) -> None:
        first_page = self.service.list_connections(self.first)
        second_page = self.service.list_connections(self.second)

        self.assertEqual(
            [item.connection_id for item in first_page.items], [self.owned.connection_id]
        )
        self.assertEqual(
            [item.connection_id for item in second_page.items],
            [self.foreign.connection_id],
        )

    def test_a_foreign_cursor_is_rejected_without_consuming_it(self) -> None:
        self.connect(self.first, channel="UC_channel_3", key="begin-3")
        page = self.service.list_connections(self.first, ConnectionPageRequest(limit=1))
        assert page.next_cursor is not None

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.list_connections(
                self.second, ConnectionPageRequest(cursor=page.next_cursor, limit=1)
            )

        self.assertEqual(raised.exception.code, "INVALID_CURSOR")
        resumed = self.service.list_connections(
            self.first, ConnectionPageRequest(cursor=page.next_cursor, limit=1)
        )
        self.assertEqual(len(resumed.items), 1)

    def test_a_foreign_callback_cannot_complete_an_in_flight_intent(self) -> None:
        self.service.begin_authorization(
            self.first, BeginAuthorization(idempotency_key="begin-4")
        )

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.second, callback(self.gateway, idempotency_key="begin-4")
            )

        self.assertEqual(raised.exception.code, "INTENT_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(len(self.gateway.exchanges), 2)

    def test_audit_events_stay_attributed_to_their_workspace(self) -> None:
        workspaces = {event.workspace_id for event in self.service._state.audit_events}

        self.assertEqual(workspaces, {"workspace-1", "workspace-2"})
        for event in self.service._state.audit_events:
            if event.connection_id == self.owned.connection_id:
                self.assertEqual(event.workspace_id, "workspace-1")


if __name__ == "__main__":
    unittest.main()
