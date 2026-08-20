from __future__ import annotations

import unittest
from datetime import timedelta

from channel_connections.errors import ChannelConnectionsError
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import (
    AuthorizationFailureReason,
    BeginAuthorization,
    BeginReauthorization,
    ChannelConnection,
    ConnectionPageRequest,
    ConnectionProvider,
    ConnectionStatus,
    YOUTUBE_READONLY_SCOPE,
)
from channel_connections.ports import ProviderRejected, ProviderUnavailable
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FailingCredentialVault,
    FakeYouTubeGateway,
    FixedClock,
    NOW,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
    context,
    credential,
    grant,
)
from workspace_access.models import Permission


class ConnectionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.gateway = FakeYouTubeGateway()
        self.ephemeral = RecordingEphemeralStore(InMemoryEphemeralSecretStore())
        self.vault = InMemoryCredentialVault()
        self.service = self.build_service()
        self.owner = context()

    def build_service(self, vault: object | None = None) -> ChannelConnectionsService:
        return ChannelConnectionsService(
            clock=self.clock,
            tokens=SequenceTokens(),
            gateway=self.gateway,
            ephemeral_secrets=self.ephemeral,
            credential_vault=vault or self.vault,
        )

    def connect(
        self,
        owner=None,
        *,
        begin_key: str = "begin-1",
        callback_key: str = "callback-1",
    ) -> ChannelConnection:
        actor = owner or self.owner
        self.service.begin_authorization(actor, BeginAuthorization(idempotency_key=begin_key))
        return self.service.complete_authorization(
            actor, callback(self.gateway, idempotency_key=callback_key)
        )


class ConnectionPublicationTests(ConnectionFixture):
    def test_a_verified_grant_publishes_an_active_connection(self) -> None:
        connection = self.connect()

        self.assertIsInstance(connection, ChannelConnection)
        self.assertEqual(connection.workspace_id, "workspace-1")
        self.assertEqual(connection.provider, ConnectionProvider.YOUTUBE)
        self.assertEqual(connection.provider_channel_id, "UC_channel_1")
        self.assertEqual(connection.status, ConnectionStatus.ACTIVE)
        self.assertEqual(connection.connected_at, NOW)
        self.assertEqual(connection.updated_at, NOW)

    def test_exchange_uses_the_stored_verifier_and_fixed_redirect(self) -> None:
        self.connect()

        exchange = self.gateway.exchanges[0]
        self.assertEqual(exchange.redirect_uri_id, "hosted-callback")
        self.assertEqual(exchange.code, "synthetic-authorization-code")
        self.assertEqual(
            exchange.code_verifier,
            self.ephemeral.puts[0].secret.reveal(),
        )

    def test_publication_stores_a_credential_and_clears_ephemeral_secrets(self) -> None:
        connection = self.connect()

        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)
        self.assertEqual(self.ephemeral.slot_ids_left(), ())
        self.assertNotIn("workspace-1", self.vault.slot_ids("workspace-2"))
        rendered = repr(self.service._state)
        self.assertNotIn("synthetic-refresh-token", rendered)
        self.assertNotIn("synthetic-authorization-code", rendered)
        self.assertNotIn(connection.connection_id, self.vault.slot_ids("workspace-2"))

    def test_connection_is_readable_only_through_its_workspace(self) -> None:
        connection = self.connect()

        self.assertEqual(
            self.service.get_connection(self.owner, connection.connection_id),
            connection,
        )
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.get_connection(context("workspace-2"), connection.connection_id)
        self.assertEqual(raised.exception.code, "CONNECTION_NOT_FOUND_OR_FORBIDDEN")

    def test_members_may_read_but_not_manage(self) -> None:
        connection = self.connect()
        member = context("workspace-1", Permission.CHANNEL_READ)

        self.assertEqual(
            self.service.get_connection(member, connection.connection_id).connection_id,
            connection.connection_id,
        )
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.begin_authorization(member, BeginAuthorization(idempotency_key="k"))
        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_the_same_channel_cannot_be_connected_twice_in_one_workspace(self) -> None:
        self.connect()

        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-2")
        )
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner, callback(self.gateway, idempotency_key="callback-2")
            )

        self.assertEqual(raised.exception.code, "CONNECTION_ALREADY_EXISTS")
        self.assertEqual(len(self.gateway.revocations), 1)
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)

    def test_the_same_channel_connects_independently_in_two_workspaces(self) -> None:
        first = self.connect()
        second = self.connect(context("workspace-2"), begin_key="begin-1", callback_key="callback-1")

        self.assertNotEqual(first.connection_id, second.connection_id)
        self.assertEqual(first.provider_channel_id, second.provider_channel_id)
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)
        self.assertEqual(len(self.vault.slot_ids("workspace-2")), 1)

    def test_audit_records_the_established_connection_without_secrets(self) -> None:
        connection = self.connect()

        event = self.service._state.audit_events[-1]
        self.assertEqual(event.action.value, "CONNECTION_ESTABLISHED")
        self.assertEqual(event.connection_id, connection.connection_id)
        self.assertNotIn("synthetic", repr(event))


class ProviderVerificationTests(ConnectionFixture):
    def start(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )

    def complete(self) -> ChannelConnectionsError:
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(self.owner, callback(self.gateway))
        return raised.exception

    def test_extra_scopes_are_rejected_and_revoked(self) -> None:
        self.gateway.grant = grant(
            granted_scopes=(YOUTUBE_READONLY_SCOPE, "https://www.googleapis.com/auth/youtube")
        )
        self.start()

        error = self.complete()

        self.assertEqual(error.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertEqual(error.reason_code, AuthorizationFailureReason.SCOPE_NOT_GRANTED.value)
        self.assertEqual(len(self.gateway.revocations), 1)
        self.assertEqual(self.vault.slot_ids("workspace-1"), ())

    def test_a_missing_refresh_token_is_rejected_and_revoked(self) -> None:
        self.gateway.grant = grant(provider_credential=credential(refresh_token=None))
        self.start()

        error = self.complete()

        self.assertEqual(
            error.reason_code, AuthorizationFailureReason.OFFLINE_CREDENTIAL_MISSING.value
        )
        self.assertEqual(len(self.gateway.revocations), 1)
        self.assertEqual(self.vault.slot_ids("workspace-1"), ())

    def test_a_failed_subscriber_probe_is_rejected_and_revoked(self) -> None:
        self.gateway.grant = grant(subscriber_capability_verified=False)
        self.start()

        error = self.complete()

        self.assertEqual(error.code, "PROVIDER_CAPABILITY_MISSING")
        self.assertEqual(len(self.gateway.revocations), 1)
        self.assertEqual(self.vault.slot_ids("workspace-1"), ())

    def test_an_ambiguous_channel_result_fails_closed(self) -> None:
        self.gateway.failure = ProviderRejected(AuthorizationFailureReason.CHANNEL_NOT_UNIQUE)
        self.start()

        error = self.complete()

        self.assertEqual(error.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertEqual(error.reason_code, AuthorizationFailureReason.CHANNEL_NOT_UNIQUE.value)
        self.assertEqual(self.service._state.connections, {})

    def test_a_malformed_provider_result_is_not_trusted(self) -> None:
        self.gateway.grant = {"provider_channel_id": "UC_channel_1"}  # type: ignore[assignment]
        self.start()

        error = self.complete()

        self.assertEqual(
            error.reason_code, AuthorizationFailureReason.INVALID_PROVIDER_RESPONSE.value
        )
        self.assertEqual(self.service._state.connections, {})

    def test_raw_provider_failure_detail_never_reaches_the_caller(self) -> None:
        self.gateway.failure = RuntimeError("provider said no: token ya29.secret")
        self.start()

        error = self.complete()

        self.assertNotIn("ya29", error.message)
        self.assertNotIn("token", error.message)
        self.assertIsNone(error.field)

    def test_an_unknown_provider_outcome_stays_retryable_and_unresolved(self) -> None:
        self.gateway.failure = ProviderUnavailable()
        self.start()

        error = self.complete()

        self.assertEqual(error.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertTrue(error.retryable)
        self.assertEqual(len(self.service._state.cleanups), 1)
        cleanup = next(iter(self.service._state.cleanups.values()))
        self.assertIsNone(cleanup.resolved_at)
        self.assertNotIn("ya29", repr(cleanup))

    def test_a_vault_failure_publishes_nothing_and_revokes(self) -> None:
        failing = FailingCredentialVault()
        self.service = self.build_service(vault=failing)
        self.start()

        error = self.complete()

        self.assertEqual(error.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertEqual(self.service._state.connections, {})
        self.assertEqual(self.service._state.active_keys, {})
        self.assertEqual(len(self.service._state.cleanups), 1)


class CallbackConsumptionTests(ConnectionFixture):
    def test_provider_denial_consumes_the_intent_and_its_secrets(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner,
                callback(self.gateway, code=None, provider_error="access_denied"),
            )

        self.assertEqual(raised.exception.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertEqual(
            raised.exception.reason_code, AuthorizationFailureReason.PROVIDER_DENIED.value
        )
        self.assertEqual(self.gateway.exchanges, [])
        self.assertEqual(self.service._state.intents, {})
        self.assertEqual(self.ephemeral.slot_ids_left(), ())

    def test_a_consumed_state_cannot_be_replayed_with_a_new_key(self) -> None:
        self.connect()

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner, callback(self.gateway, idempotency_key="callback-2")
            )

        self.assertEqual(raised.exception.code, "INTENT_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(len(self.gateway.exchanges), 1)

    def test_exact_duplicate_callback_returns_the_original_connection(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )
        command = callback(self.gateway)

        first = self.service.complete_authorization(self.owner, command)
        second = self.service.complete_authorization(self.owner, command)

        self.assertEqual(first, second)
        self.assertEqual(len(self.gateway.exchanges), 1)

    def test_a_changed_callback_payload_conflicts(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )
        self.service.complete_authorization(self.owner, callback(self.gateway))

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner, callback(self.gateway, code="another-code")
            )

        self.assertEqual(raised.exception.code, "CALLBACK_CONFLICT")

    def test_replay_expires_after_twenty_four_hours(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )
        command = callback(self.gateway)
        self.service.complete_authorization(self.owner, command)

        self.clock.advance(timedelta(hours=24, seconds=1))
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(self.owner, command)

        self.assertEqual(raised.exception.code, "INTENT_NOT_FOUND_OR_EXPIRED")


if __name__ == "__main__":
    unittest.main()


class UnknownOutcomeRetryTests(ConnectionFixture):
    def test_retry_after_an_unknown_outcome_never_exchanges_twice(self) -> None:
        self.gateway.failure = ProviderUnavailable()
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )
        command = callback(self.gateway)
        with self.assertRaises(ChannelConnectionsError):
            self.service.complete_authorization(self.owner, command)

        self.gateway.failure = None
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner, callback(self.gateway, idempotency_key="callback-2")
            )

        self.assertEqual(raised.exception.code, "OPERATION_IN_PROGRESS")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(self.gateway.exchanges), 1)
        self.assertEqual(self.service._state.connections, {})


class ListConnectionsTests(ConnectionFixture):
    def connect_channel(self, index: int, actor=None) -> ChannelConnection:
        self.gateway.grant = grant(
            provider_channel_id=f"UC_channel_{index}",
            channel_title=f"チャンネル{index}",
        )
        connection = self.connect(
            actor,
            begin_key=f"begin-{index}",
            callback_key=f"callback-{index}",
        )
        self.clock.advance(timedelta(minutes=1))
        return connection

    def test_listing_is_newest_first_and_defaults_to_fifty(self) -> None:
        first = self.connect_channel(1)
        second = self.connect_channel(2)

        page = self.service.list_connections(self.owner)

        self.assertEqual(
            [item.connection_id for item in page.items],
            [second.connection_id, first.connection_id],
        )
        self.assertIsNone(page.next_cursor)
        self.assertEqual(ConnectionPageRequest().limit, 50)

    def test_members_may_list_but_a_foreign_workspace_sees_nothing(self) -> None:
        self.connect_channel(1)
        member = context("workspace-1", Permission.CHANNEL_READ)

        self.assertEqual(len(self.service.list_connections(member).items), 1)
        self.assertEqual(
            self.service.list_connections(context("workspace-2")).items, ()
        )

    def test_full_traversal_returns_every_row_exactly_once(self) -> None:
        expected = [self.connect_channel(index).connection_id for index in range(1, 6)]

        seen: list[str] = []
        page = self.service.list_connections(self.owner, ConnectionPageRequest(limit=2))
        seen.extend(item.connection_id for item in page.items)
        while page.next_cursor is not None:
            page = self.service.list_connections(
                self.owner, ConnectionPageRequest(cursor=page.next_cursor, limit=2)
            )
            seen.extend(item.connection_id for item in page.items)

        self.assertEqual(seen, list(reversed(expected)))
        self.assertEqual(len(set(seen)), len(expected))

    def test_cursor_is_opaque_and_carries_no_connection_metadata(self) -> None:
        connection = self.connect_channel(1)
        self.connect_channel(2)

        page = self.service.list_connections(self.owner, ConnectionPageRequest(limit=1))

        self.assertIsNotNone(page.next_cursor)
        self.assertNotIn(connection.connection_id, page.next_cursor)
        self.assertNotIn("UC_channel", page.next_cursor)
        self.assertNotIn("workspace-1", page.next_cursor)

    def test_tampered_or_foreign_cursors_fail_identically(self) -> None:
        self.connect_channel(1)
        self.connect_channel(2)
        page = self.service.list_connections(self.owner, ConnectionPageRequest(limit=1))
        cursor = page.next_cursor
        assert cursor is not None

        failures = []
        for actor, token, limit in (
            (self.owner, cursor + "x", 1),
            (context("workspace-2"), cursor, 1),
            (self.owner, cursor, 2),
        ):
            with self.assertRaises(ChannelConnectionsError) as raised:
                self.service.list_connections(
                    actor, ConnectionPageRequest(cursor=token, limit=limit)
                )
            failures.append(raised.exception)

        self.assertEqual({failure.code for failure in failures}, {"INVALID_CURSOR"})
        self.assertEqual({failure.message for failure in failures}, {failures[0].message})

    def test_a_cursor_is_single_use(self) -> None:
        self.connect_channel(1)
        self.connect_channel(2)
        page = self.service.list_connections(self.owner, ConnectionPageRequest(limit=1))
        assert page.next_cursor is not None

        self.service.list_connections(
            self.owner, ConnectionPageRequest(cursor=page.next_cursor, limit=1)
        )
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.list_connections(
                self.owner, ConnectionPageRequest(cursor=page.next_cursor, limit=1)
            )

        self.assertEqual(raised.exception.code, "INVALID_CURSOR")

    def test_a_changed_result_set_expires_the_cursor(self) -> None:
        self.connect_channel(1)
        self.connect_channel(2)
        page = self.service.list_connections(self.owner, ConnectionPageRequest(limit=1))
        assert page.next_cursor is not None

        self.connect_channel(3)

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.list_connections(
                self.owner, ConnectionPageRequest(cursor=page.next_cursor, limit=1)
            )
        self.assertEqual(raised.exception.code, "CURSOR_EXPIRED")


class ReauthorizationTests(ConnectionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.connection = self.connect()
        self.original_slot = self.vault.slot_ids("workspace-1")[0]
        self.clock.advance(timedelta(hours=1))

    def reauthorize(self, *, key: str = "reauth-1", callback_key: str = "cb-reauth-1"):
        self.service.begin_reauthorization(
            self.owner,
            BeginReauthorization(
                connection_id=self.connection.connection_id, idempotency_key=key
            ),
        )
        return self.service.complete_authorization(
            self.owner, callback(self.gateway, idempotency_key=callback_key)
        )

    def test_reauthorization_requires_manage_permission_and_an_own_connection(self) -> None:
        member = context("workspace-1", Permission.CHANNEL_READ)
        with self.assertRaises(ChannelConnectionsError) as denied:
            self.service.begin_reauthorization(
                member,
                BeginReauthorization(
                    connection_id=self.connection.connection_id, idempotency_key="r"
                ),
            )
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

        for actor, connection_id in (
            (self.owner, "connection_missing"),
            (context("workspace-2"), self.connection.connection_id),
        ):
            with self.assertRaises(ChannelConnectionsError) as raised:
                self.service.begin_reauthorization(
                    actor,
                    BeginReauthorization(
                        connection_id=connection_id, idempotency_key="r"
                    ),
                )
            self.assertEqual(raised.exception.code, "CONNECTION_NOT_FOUND_OR_FORBIDDEN")

    def test_reauthorization_rotates_the_credential_slot_atomically(self) -> None:
        rotated = self.reauthorize()

        self.assertEqual(rotated.connection_id, self.connection.connection_id)
        self.assertEqual(rotated.status, ConnectionStatus.ACTIVE)
        self.assertEqual(rotated.connected_at, self.connection.connected_at)
        self.assertEqual(rotated.updated_at, NOW + timedelta(hours=1))

        slots = self.vault.slot_ids("workspace-1")
        self.assertEqual(len(slots), 1)
        self.assertNotEqual(slots[0], self.original_slot)
        self.assertEqual(self.gateway.revocations, [])

    def test_reauthorization_start_is_audited(self) -> None:
        self.service.begin_reauthorization(
            self.owner,
            BeginReauthorization(
                connection_id=self.connection.connection_id, idempotency_key="r"
            ),
        )

        event = self.service._state.audit_events[-1]
        self.assertEqual(event.action.value, "REAUTHORIZATION_STARTED")
        self.assertEqual(event.connection_id, self.connection.connection_id)

    def test_a_successful_rotation_is_audited(self) -> None:
        self.reauthorize()

        event = self.service._state.audit_events[-1]
        self.assertEqual(event.action.value, "CONNECTION_REAUTHORIZED")
        self.assertEqual(event.connection_id, self.connection.connection_id)

    def test_a_different_channel_cannot_replace_the_credential(self) -> None:
        self.gateway.grant = grant(provider_channel_id="UC_other_channel")

        self.service.begin_reauthorization(
            self.owner,
            BeginReauthorization(
                connection_id=self.connection.connection_id, idempotency_key="r"
            ),
        )
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(
                self.owner, callback(self.gateway, idempotency_key="cb-r")
            )

        self.assertEqual(raised.exception.code, "REAUTH_CHANNEL_MISMATCH")
        self.assertEqual(self.vault.slot_ids("workspace-1"), (self.original_slot,))
        self.assertEqual(len(self.gateway.revocations), 1)
        self.assertEqual(
            self.service.get_connection(self.owner, self.connection.connection_id),
            self.connection,
        )

    def test_repeated_rotations_leave_exactly_one_slot(self) -> None:
        self.reauthorize()
        self.clock.advance(timedelta(hours=1))
        self.reauthorize(key="reauth-2", callback_key="cb-reauth-2")

        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)
        self.assertEqual(len(self.service._state.connections), 1)
        self.assertEqual(len(self.service._state.active_keys), 1)
