from __future__ import annotations

import json
import unittest

from channel_connections.errors import ChannelConnectionsError
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import (
    BeginAuthorization,
    DisconnectConnection,
)
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeGateway,
    FixedClock,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
    context,
)


class FakeStore:
    """One document, exactly what a restart would find on disk."""

    def __init__(self) -> None:
        self.document: str | None = None
        self.saves = 0

    def load(self) -> str | None:
        return self.document

    def save(self, document: str) -> None:
        self.document = document
        self.saves += 1


class RestartFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.gateway = FakeYouTubeGateway()
        self.ephemeral = RecordingEphemeralStore(InMemoryEphemeralSecretStore())
        self.vault = InMemoryCredentialVault()
        self.store = FakeStore()
        self.owner = context()
        self.service = self.restart()

    def restart(self) -> ChannelConnectionsService:
        self.service = ChannelConnectionsService(
            clock=self.clock,
            tokens=SequenceTokens(),
            gateway=self.gateway,
            ephemeral_secrets=self.ephemeral,
            credential_vault=self.vault,
            state_store=self.store,
        )
        return self.service

    def connect(self, *, begin_key: str = "begin-1", callback_key: str = "callback-1"):
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key=begin_key)
        )
        return self.service.complete_authorization(
            self.owner, callback(self.gateway, idempotency_key=callback_key)
        )


class ConnectionRestartTests(RestartFixture):
    def test_a_connected_channel_outlives_the_process(self) -> None:
        connection = self.connect()

        self.restart()

        restored = self.service.get_connection(self.owner, connection.connection_id)
        self.assertEqual(restored, connection)

    def test_a_disconnected_channel_does_not_come_back(self) -> None:
        """Disconnect removes the metadata, so a restart must not restore it."""

        connection = self.connect()
        self.service.disconnect(
            self.owner,
            DisconnectConnection(
                connection_id=connection.connection_id, idempotency_key="disconnect-1"
            ),
        )

        self.restart()

        self.assertEqual(self.service.list_connections(self.owner).items, ())
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.get_connection(self.owner, connection.connection_id)
        self.assertEqual(raised.exception.code, "CONNECTION_NOT_FOUND_OR_FORBIDDEN")

    def test_a_replayed_command_is_still_answered_once(self) -> None:
        connection = self.connect()

        self.restart()

        replayed = self.service.complete_authorization(
            self.owner, callback(self.gateway, idempotency_key="callback-1")
        )
        self.assertEqual(replayed, connection)
        self.assertEqual(len(self.service.list_connections(self.owner).items), 1)


class SecrecyTests(RestartFixture):
    def test_no_credential_material_is_ever_written_down(self) -> None:
        self.connect()

        document = self.store.document or ""
        self.assertNotIn("synthetic-access-token", document)
        self.assertNotIn("synthetic-refresh-token", document)
        self.assertNotIn("synthetic-authorization-code", document)

    def test_a_sign_in_still_underway_does_not_survive(self) -> None:
        """The verifier lives in the ephemeral store, which dies with the process."""

        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )

        self.restart()

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner, callback(self.gateway, idempotency_key="callback-1")
            )
        self.assertEqual(raised.exception.code, "INTENT_NOT_FOUND_OR_EXPIRED")

    def test_the_codec_refuses_to_write_a_credential(self) -> None:
        from channel_connections import snapshot
        from channel_connections.tests.support import credential

        with self.assertRaises(TypeError):
            snapshot._encode(credential())


class DocumentTests(RestartFixture):
    def test_an_empty_store_is_not_written_until_something_happens(self) -> None:
        self.assertIsNone(self.store.document)
        self.assertEqual(self.store.saves, 0)

    def test_a_document_from_a_newer_version_is_refused(self) -> None:
        self.store.document = json.dumps({"version": 99})

        with self.assertRaises(ValueError):
            self.restart()

    def test_an_unreadable_document_is_refused(self) -> None:
        self.store.document = "{not json"

        with self.assertRaises(ValueError):
            self.restart()

    def test_a_service_without_a_store_still_works(self) -> None:
        service = ChannelConnectionsService(
            clock=self.clock,
            tokens=SequenceTokens(),
            gateway=self.gateway,
            ephemeral_secrets=self.ephemeral,
            credential_vault=self.vault,
        )

        service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-plain")
        )

        self.assertEqual(len(service.list_connections(self.owner).items), 0)


if __name__ == "__main__":
    unittest.main()
