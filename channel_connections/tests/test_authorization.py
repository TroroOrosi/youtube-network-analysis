from __future__ import annotations

import base64
import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from channel_connections.errors import ChannelConnectionsError
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import (
    APPROVED_SCOPES,
    AuthorizationStart,
    BeginAuthorization,
    INTENT_TTL,
)
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeGateway,
    FixedClock,
    NOW,
    RecordingEphemeralStore,
    SequenceTokens,
    context,
)
from workspace_access.models import Permission


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_service() -> tuple[
    ChannelConnectionsService, FixedClock, FakeYouTubeGateway, RecordingEphemeralStore
]:
    clock = FixedClock()
    gateway = FakeYouTubeGateway()
    ephemeral = RecordingEphemeralStore(InMemoryEphemeralSecretStore())
    service = ChannelConnectionsService(
        clock=clock,
        tokens=SequenceTokens(),
        gateway=gateway,
        ephemeral_secrets=ephemeral,
        credential_vault=InMemoryCredentialVault(),
    )
    return service, clock, gateway, ephemeral


class BeginAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock, self.gateway, self.ephemeral = build_service()
        self.owner = context()

    def test_owner_receives_only_intent_url_and_expiry(self) -> None:
        start = self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )

        self.assertIsInstance(start, AuthorizationStart)
        self.assertTrue(start.authorization_url.startswith("https://accounts.google.com/"))
        self.assertEqual(start.expires_at, NOW + INTENT_TTL)
        self.assertEqual(
            [field for field in AuthorizationStart.__slots__],
            ["intent_id", "authorization_url", "expires_at"],
        )

    def test_member_without_manage_permission_is_denied(self) -> None:
        member = context("workspace-1", Permission.CHANNEL_READ)

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.begin_authorization(
                member, BeginAuthorization(idempotency_key="key-1")
            )

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")
        self.assertEqual(self.gateway.authorization_calls, [])

    def test_request_uses_fixed_scope_and_redirect_configuration(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )

        call = self.gateway.authorization_calls[0]
        self.assertEqual(call.scopes, APPROVED_SCOPES)
        self.assertEqual(call.redirect_uri_id, "hosted-callback")

    def test_pkce_verifier_is_stored_and_challenge_is_s256(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )

        put = self.ephemeral.puts[0]
        self.assertEqual(put.workspace_id, "workspace-1")
        self.assertEqual(put.expires_at, NOW + INTENT_TTL)
        self.assertEqual(
            self.gateway.authorization_calls[0].code_challenge,
            s256(put.secret.reveal()),
        )

    def test_raw_state_is_never_retained_in_service_state(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )

        raw_state = self.gateway.authorization_calls[0].state
        verifier = self.ephemeral.puts[0].secret.reveal()
        rendered = repr(self.service._state)
        self.assertNotIn(raw_state, rendered)
        self.assertNotIn(verifier, rendered)
        self.assertIn(hashlib.sha256(raw_state.encode("utf-8")).hexdigest(), rendered)

    def test_state_and_verifier_differ_per_intent(self) -> None:
        first = self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )
        second = self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-2")
        )

        self.assertNotEqual(first.intent_id, second.intent_id)
        states = {call.state for call in self.gateway.authorization_calls}
        verifiers = {
            put.secret.reveal()
            for put in self.ephemeral.puts
            if put.slot_id.startswith("pkce_")
        }
        self.assertEqual(len(states), 2)
        self.assertEqual(len(verifiers), 2)

    def test_exact_replay_returns_the_original_start(self) -> None:
        command = BeginAuthorization(idempotency_key="key-1")

        first = self.service.begin_authorization(self.owner, command)
        second = self.service.begin_authorization(self.owner, command)

        self.assertEqual(first, second)
        self.assertEqual(len(self.gateway.authorization_calls), 1)

    def test_reused_key_from_another_actor_or_session_conflicts(self) -> None:
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )

        other_session = context("workspace-1", *self.owner.permissions, session_id="other")
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.begin_authorization(
                other_session, BeginAuthorization(idempotency_key="key-1")
            )
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

        other_user = context("workspace-1", *self.owner.permissions, user_id="user-2")
        second = self.service.begin_authorization(
            other_user, BeginAuthorization(idempotency_key="key-1")
        )
        self.assertNotEqual(second.intent_id, "")
        self.assertEqual(len(self.gateway.authorization_calls), 2)

    def test_expired_replay_starts_a_new_intent(self) -> None:
        command = BeginAuthorization(idempotency_key="key-1")
        first = self.service.begin_authorization(self.owner, command)

        self.clock.advance(INTENT_TTL + timedelta(seconds=1))
        second = self.service.begin_authorization(self.owner, command)

        self.assertNotEqual(first.intent_id, second.intent_id)
        self.assertEqual(second.expires_at, first.expires_at + INTENT_TTL + timedelta(seconds=1))

    def test_one_idempotency_key_creates_one_intent_under_concurrency(self) -> None:
        command = BeginAuthorization(idempotency_key="key-1")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.service.begin_authorization(self.owner, command),
                    range(8),
                )
            )

        self.assertEqual({result.intent_id for result in results}, {results[0].intent_id})
        self.assertEqual(len(self.gateway.authorization_calls), 1)

    def test_identical_keys_in_two_workspaces_stay_independent(self) -> None:
        other = context("workspace-2")

        first = self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )
        second = self.service.begin_authorization(
            other, BeginAuthorization(idempotency_key="key-1")
        )

        self.assertNotEqual(first.intent_id, second.intent_id)
        self.assertEqual(len(self.gateway.authorization_calls), 2)
        self.assertEqual(
            {put.workspace_id for put in self.ephemeral.puts},
            {"workspace-1", "workspace-2"},
        )

    def test_authorization_url_from_a_foreign_host_fails_closed(self) -> None:
        self.gateway.authorization_host = "accounts.google.com.attacker.example"

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.begin_authorization(
                self.owner, BeginAuthorization(idempotency_key="key-1")
            )

        self.assertEqual(raised.exception.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertNotIn("attacker", raised.exception.message)

    def test_failed_start_leaves_no_intent_or_ephemeral_secret(self) -> None:
        self.gateway.authorization_host = "attacker.example"

        with self.assertRaises(ChannelConnectionsError):
            self.service.begin_authorization(
                self.owner, BeginAuthorization(idempotency_key="key-1")
            )

        self.assertEqual(self.service._state.intents, {})
        self.assertEqual(self.service._state.idempotency, {})
        self.assertEqual(len(self.ephemeral.deleted), 1)

    def test_audit_event_records_the_start_without_secrets(self) -> None:
        start = self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="key-1")
        )

        event = self.service._state.audit_events[-1]
        self.assertEqual(event.action.value, "AUTHORIZATION_STARTED")
        self.assertEqual(event.workspace_id, "workspace-1")
        self.assertEqual(event.actor_user_id, "user-1")
        self.assertEqual(event.intent_id, start.intent_id)
        self.assertIsNone(event.connection_id)
        rendered = repr(event)
        self.assertNotIn(self.gateway.authorization_calls[0].state, rendered)


if __name__ == "__main__":
    unittest.main()


class CompleteAuthorizationBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock, self.gateway, self.ephemeral = build_service()
        self.owner = context()
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )

    def callback(self, **overrides):
        from channel_connections.tests.support import callback

        return callback(self.gateway, **overrides)

    def test_an_unknown_state_is_indistinguishable_from_an_expired_one(self) -> None:
        from channel_connections.models import CompleteAuthorization, RedactedSecret

        unknown = CompleteAuthorization(
            state=RedactedSecret("never-issued-state"),
            code=RedactedSecret("synthetic-authorization-code"),
            idempotency_key="callback-1",
        )
        with self.assertRaises(ChannelConnectionsError) as unknown_raised:
            self.service.complete_authorization(self.owner, unknown)

        self.clock.advance(INTENT_TTL + timedelta(seconds=1))
        with self.assertRaises(ChannelConnectionsError) as expired_raised:
            self.service.complete_authorization(self.owner, self.callback())

        self.assertEqual(unknown_raised.exception.code, "INTENT_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(
            unknown_raised.exception.message, expired_raised.exception.message
        )
        self.assertEqual(self.gateway.exchanges, [])

    def test_a_foreign_workspace_cannot_complete_the_callback(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(context("workspace-2"), self.callback())

        self.assertEqual(raised.exception.code, "INTENT_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(self.gateway.exchanges, [])

    def test_another_owner_or_session_cannot_complete_the_callback(self) -> None:
        other_user = context("workspace-1", *self.owner.permissions, user_id="user-2")
        with self.assertRaises(ChannelConnectionsError) as user_raised:
            self.service.complete_authorization(other_user, self.callback())

        other_session = context(
            "workspace-1", *self.owner.permissions, session_id="another-session"
        )
        with self.assertRaises(ChannelConnectionsError) as session_raised:
            self.service.complete_authorization(other_session, self.callback())

        self.assertEqual(user_raised.exception.code, "CALLBACK_CONFLICT")
        self.assertEqual(session_raised.exception.code, "CALLBACK_CONFLICT")
        self.assertEqual(self.gateway.exchanges, [])

    def test_completion_requires_the_manage_permission(self) -> None:
        member = context("workspace-1", Permission.CHANNEL_READ)

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.complete_authorization(member, self.callback())

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")
        self.assertEqual(self.gateway.exchanges, [])

    def test_expired_intents_remove_their_ephemeral_secrets(self) -> None:
        self.clock.advance(INTENT_TTL + timedelta(seconds=1))

        with self.assertRaises(ChannelConnectionsError):
            self.service.complete_authorization(self.owner, self.callback())

        self.assertEqual(self.service._state.intents, {})
        self.assertEqual(self.ephemeral.slot_ids_left(), ())

    def test_only_one_exchange_runs_for_concurrent_callbacks(self) -> None:
        command = self.callback()

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(
                pool.map(
                    lambda _: self._attempt(command),
                    range(4),
                )
            )

        connections = [item for item in outcomes if not isinstance(item, Exception)]
        self.assertEqual(len(self.gateway.exchanges), 1)
        self.assertEqual({result.connection_id for result in connections}, {connections[0].connection_id})

    def _attempt(self, command):
        try:
            return self.service.complete_authorization(self.owner, command)
        except ChannelConnectionsError as error:
            return error
