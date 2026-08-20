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
    CALLBACK_REPLAY_TTL,
    ConnectionRetentionReport,
    DeleteWorkspaceConnections,
    DisconnectConnection,
    IDEMPOTENCY_TTL,
    INTENT_TTL,
    RedactedSecret,
)
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeGateway,
    FixedClock,
    NOW,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
    context,
    grant,
)
from workspace_access.models import Permission


class PrivacyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.gateway = FakeYouTubeGateway()
        self.ephemeral = RecordingEphemeralStore(InMemoryEphemeralSecretStore())
        self.vault = InMemoryCredentialVault()
        self.service = ChannelConnectionsService(
            clock=self.clock,
            tokens=SequenceTokens(),
            gateway=self.gateway,
            ephemeral_secrets=self.ephemeral,
            credential_vault=self.vault,
        )
        self.owner = context()
        self.other = context("workspace-2")

    def connect(self, actor=None, *, channel: str = "UC_channel_1", key: str = "k1"):
        self.gateway.grant = grant(provider_channel_id=channel)
        actor = actor or self.owner
        self.service.begin_authorization(actor, BeginAuthorization(idempotency_key=key))
        return self.service.complete_authorization(
            actor, callback(self.gateway, idempotency_key=key)
        )

    def purge(self, at, actor=None) -> ConnectionRetentionReport:
        return self.service.purge_retention(actor or self.owner, at)


class RetentionBoundaryTests(PrivacyFixture):
    def test_unclaimed_intents_expire_at_exactly_ten_minutes(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="k1")
        )

        before = self.purge(NOW + INTENT_TTL - timedelta(seconds=1))
        self.assertEqual(before.intents_removed, 0)
        self.assertEqual(len(self.service._state.intents), 1)

        report = self.purge(NOW + INTENT_TTL)

        self.assertEqual(report.intents_removed, 1)
        self.assertEqual(self.service._state.intents, {})
        self.assertEqual(self.service._state.intents_by_state, {})
        self.assertEqual(self.ephemeral.slot_ids_left(), ())

    def test_callback_replay_records_expire_at_exactly_twenty_four_hours(self) -> None:
        self.connect()

        before = self.purge(NOW + CALLBACK_REPLAY_TTL - timedelta(seconds=1))
        self.assertEqual(before.callback_replays_removed, 0)

        report = self.purge(NOW + CALLBACK_REPLAY_TTL)

        self.assertEqual(report.callback_replays_removed, 1)
        self.assertNotIn(
            ("workspace-1", "user-1", "complete", "k1"), self.service._state.idempotency
        )

    def test_mutation_idempotency_records_expire_at_exactly_ninety_days(self) -> None:
        connection = self.connect()
        self.service.disconnect(
            self.owner,
            DisconnectConnection(
                connection_id=connection.connection_id, idempotency_key="d1"
            ),
        )

        before = self.purge(NOW + IDEMPOTENCY_TTL - timedelta(seconds=1))
        self.assertEqual(before.idempotency_records_removed, 0)

        report = self.purge(NOW + IDEMPOTENCY_TTL)

        self.assertGreaterEqual(report.idempotency_records_removed, 1)
        self.assertEqual(self.service._state.idempotency, {})

    def test_unresolved_cleanup_records_are_never_silently_purged(self) -> None:
        self.gateway.revocation_outcome = self.gateway.revocation_outcome.NOT_CONFIRMED
        connection = self.connect()
        self.service.disconnect(
            self.owner,
            DisconnectConnection(
                connection_id=connection.connection_id, idempotency_key="d1"
            ),
        )

        self.purge(NOW + timedelta(days=400))

        cleanups = list(self.service._state.cleanups.values())
        self.assertEqual(len(cleanups), 1)
        self.assertIsNone(cleanups[0].resolved_at)

    def test_orphan_ephemeral_and_credential_slots_are_reconciled(self) -> None:
        self.ephemeral.put(
            "workspace-1", "pkce_orphan", RedactedSecret("synthetic"), NOW
        )
        self.vault.put("workspace-1", "cred_orphan", grant().credential)

        report = self.purge(NOW + timedelta(seconds=1))

        self.assertEqual(report.ephemeral_slots_removed, 1)
        self.assertEqual(report.credential_slots_removed, 1)
        self.assertEqual(self.vault.slot_ids("workspace-1"), ())

    def test_purging_keeps_live_connections_and_their_credentials(self) -> None:
        connection = self.connect()

        report = self.purge(NOW + timedelta(days=400))

        self.assertEqual(report.credential_slots_removed, 0)
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)
        self.assertEqual(
            self.service.get_connection(self.owner, connection.connection_id).status,
            connection.status,
        )

    def test_purging_requires_the_manage_permission_and_its_own_workspace(self) -> None:
        self.connect()
        self.ephemeral.put(
            "workspace-1", "pkce_orphan", RedactedSecret("synthetic"), NOW
        )

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.purge(NOW, context("workspace-1", Permission.CHANNEL_READ))
        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

        report = self.purge(NOW + timedelta(seconds=1), self.other)
        self.assertEqual(report.ephemeral_slots_removed, 0)
        self.assertEqual(len(self.ephemeral.slot_ids_left()), 1)

    def test_a_naive_reference_time_is_rejected(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.purge(NOW.replace(tzinfo=None))

        self.assertEqual(raised.exception.code, "INVALID_INPUT")


class WorkspaceCascadeTests(PrivacyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owned = self.connect(key="k1")
        self.foreign = self.connect(self.other, key="k1")
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="pending")
        )

    def delete(self, actor=None, *, key: str = "cascade-1") -> None:
        self.service.delete_workspace_connections(
            actor or self.owner, DeleteWorkspaceConnections(idempotency_key=key)
        )

    def test_cascade_removes_every_copy_for_one_workspace_only(self) -> None:
        self.delete()

        self.assertEqual(self.vault.slot_ids("workspace-1"), ())
        self.assertEqual(len(self.vault.slot_ids("workspace-2")), 1)
        self.assertEqual(self.service._state.intents, {})
        self.assertEqual(
            [item.workspace_id for item in self.service._state.connections.values()],
            ["workspace-2"],
        )
        self.assertEqual(
            self.service.get_connection(self.other, self.foreign.connection_id),
            self.foreign,
        )
        with self.assertRaises(ChannelConnectionsError):
            self.service.get_connection(self.owner, self.owned.connection_id)
        self.assertEqual(
            [slot for _, slot in self.ephemeral.slot_ids_left()],
            [],
        )

    def test_cascade_requires_workspace_delete(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.delete(context("workspace-1", Permission.CHANNEL_MANAGE_CONNECTION))

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)

    def test_cascade_replay_deletes_and_audits_exactly_once(self) -> None:
        self.delete()
        self.delete()

        actions = [event.action.value for event in self.service._state.audit_events]
        self.assertEqual(actions.count("WORKSPACE_CONNECTIONS_DELETED"), 1)
        self.assertEqual(self.vault.slot_ids("workspace-1"), ())
        self.assertEqual(len(self.vault.slot_ids("workspace-2")), 1)

    def test_cascade_is_audited_and_leaves_no_secret_behind(self) -> None:
        self.delete()

        event = self.service._state.audit_events[-1]
        self.assertEqual(event.action.value, "WORKSPACE_CONNECTIONS_DELETED")
        self.assertEqual(event.workspace_id, "workspace-1")
        rendered = repr(self.service._state)
        for forbidden in ("synthetic-refresh-token", "synthetic-access-token"):
            self.assertNotIn(forbidden, rendered)


class RepresentationSafetyTests(PrivacyFixture):
    def test_no_stored_record_or_audit_event_contains_a_secret(self) -> None:
        connection = self.connect()
        self.service.disconnect(
            self.owner,
            DisconnectConnection(
                connection_id=connection.connection_id, idempotency_key="d1"
            ),
        )

        rendered = repr(self.service._state)
        secrets = (
            "synthetic-refresh-token",
            "synthetic-access-token",
            "synthetic-authorization-code",
            self.gateway.authorization_calls[-1].state,
        )
        for secret in secrets:
            self.assertNotIn(secret, rendered)
        for event in self.service._state.audit_events:
            for secret in secrets:
                self.assertNotIn(secret, repr(event))

    def test_public_connection_values_expose_no_credential_reference(self) -> None:
        connection = self.connect()

        slot = self.vault.slot_ids("workspace-1")[0]
        self.assertNotIn(slot, repr(connection))
        self.assertFalse(hasattr(connection, "credential_slot_id"))
        self.assertFalse(hasattr(connection, "authorized_by_user_id"))


if __name__ == "__main__":
    unittest.main()
