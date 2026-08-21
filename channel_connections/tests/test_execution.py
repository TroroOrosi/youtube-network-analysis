from __future__ import annotations

import unittest
from datetime import timedelta

from channel_connections.errors import ChannelConnectionsError
from channel_connections.memory import (
    InMemoryCredentialVault,
    InMemoryEphemeralSecretStore,
)
from channel_connections.models import (
    AUTHORITY_TTL,
    BeginAuthorization,
    BeginReauthorization,
    ConnectionStatus,
    DisconnectConnection,
    ExecutionAuthority,
    IssueExecutionAuthority,
    ProviderOperation,
    ProviderOperationRequest,
    ReportCredentialInvalidation,
)
from channel_connections.ports import ProviderAuthorizationExpired, ProviderUnavailable
from channel_connections.service import ChannelConnectionsService
from channel_connections.tests.support import (
    FakeYouTubeDataGateway,
    FakeYouTubeGateway,
    FixedClock,
    NOW,
    RecordingEphemeralStore,
    SequenceTokens,
    callback,
    context,
)
from workspace_access.models import Permission


class ExecutionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.gateway = FakeYouTubeGateway()
        self.data_gateway = FakeYouTubeDataGateway()
        self.ephemeral = RecordingEphemeralStore(InMemoryEphemeralSecretStore())
        self.vault = InMemoryCredentialVault()
        self.service = ChannelConnectionsService(
            clock=self.clock,
            tokens=SequenceTokens(),
            gateway=self.gateway,
            ephemeral_secrets=self.ephemeral,
            credential_vault=self.vault,
            data_gateway=self.data_gateway,
        )
        self.owner = context()
        self.runner = context(
            "workspace-1", Permission.COLLECTION_RUN, user_id="runner-1"
        )
        self.service.begin_authorization(
            self.owner, BeginAuthorization(idempotency_key="begin-1")
        )
        self.connection = self.service.complete_authorization(
            self.owner, callback(self.gateway, idempotency_key="callback-1")
        )

    def issue(self, actor=None, *, connection_id: str | None = None) -> ExecutionAuthority:
        return self.service.issue_execution_authority(
            actor or self.runner,
            IssueExecutionAuthority(
                connection_id=connection_id or self.connection.connection_id
            ),
        )


class ExecutionAuthorityTests(ExecutionFixture):
    def test_authority_is_workspace_bound_and_carries_no_credential(self) -> None:
        authority = self.issue()

        self.assertEqual(authority.workspace_id, "workspace-1")
        self.assertEqual(authority.connection_id, self.connection.connection_id)
        self.assertEqual(authority.provider_channel_id, "UC_channel_1")
        self.assertEqual(authority.expires_at, NOW + AUTHORITY_TTL)
        self.assertEqual(AUTHORITY_TTL, timedelta(minutes=60))
        rendered = repr(authority)
        for forbidden in ("synthetic", "cred_", "refresh", "secret"):
            self.assertNotIn(forbidden, rendered)

    def test_issuing_requires_collection_run_and_an_own_active_connection(self) -> None:
        reader = context("workspace-1", Permission.CHANNEL_READ, user_id="reader-1")
        with self.assertRaises(ChannelConnectionsError) as denied:
            self.issue(reader)
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

        foreign = context("workspace-2", Permission.COLLECTION_RUN)
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.issue(foreign)
        self.assertEqual(raised.exception.code, "CONNECTION_NOT_FOUND_OR_FORBIDDEN")

    def test_a_reauth_required_connection_cannot_issue_authority(self) -> None:
        self.service.report_credential_invalidation(
            self.owner,
            ReportCredentialInvalidation(
                connection_id=self.connection.connection_id, idempotency_key="i1"
            ),
        )

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.issue()

        self.assertEqual(raised.exception.code, "CONNECTION_REAUTH_REQUIRED")

    def test_expired_missing_and_foreign_authorities_fail_identically(self) -> None:
        authority = self.issue()
        self.clock.advance(AUTHORITY_TTL + timedelta(seconds=1))

        expired = self._operation_error(authority)
        forged = ExecutionAuthority(
            authority_id="authority_forged",
            workspace_id="workspace-1",
            connection_id=self.connection.connection_id,
            provider_channel_id="UC_channel_1",
            issued_at=NOW,
            expires_at=NOW + AUTHORITY_TTL,
        )
        missing = self._operation_error(forged)

        self.assertEqual(expired.code, "AUTHORITY_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(missing.code, "AUTHORITY_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(expired.message, missing.message)

    def test_disconnect_revokes_every_authority_for_the_connection(self) -> None:
        authority = self.issue()

        self.service.disconnect(
            self.owner,
            DisconnectConnection(
                connection_id=self.connection.connection_id, idempotency_key="d1"
            ),
        )

        self.assertEqual(
            self._operation_error(authority).code, "AUTHORITY_NOT_FOUND_OR_EXPIRED"
        )
        self.assertEqual(self.data_gateway.calls, [])

    def test_rotation_keeps_an_authority_usable(self) -> None:
        authority = self.issue()
        self.service.begin_reauthorization(
            self.owner,
            BeginReauthorization(
                connection_id=self.connection.connection_id, idempotency_key="r1"
            ),
        )
        self.service.complete_authorization(
            self.owner, callback(self.gateway, idempotency_key="cb-r1")
        )

        result = self.service.run_provider_operation(
            authority, ProviderOperationRequest(operation=ProviderOperation.LIST_VIDEOS)
        )

        self.assertEqual(result.operation, ProviderOperation.LIST_VIDEOS)
        self.assertEqual(len(self.vault.slot_ids("workspace-1")), 1)
        self.assertEqual(
            self.data_gateway.calls[-1].credential_slot_id,
            self.vault.slot_ids("workspace-1")[0],
        )

    def test_reported_invalidation_revokes_authorities(self) -> None:
        authority = self.issue()

        self.service.report_credential_invalidation(
            self.owner,
            ReportCredentialInvalidation(
                connection_id=self.connection.connection_id, idempotency_key="i1"
            ),
        )

        self.assertEqual(
            self._operation_error(authority).code, "AUTHORITY_NOT_FOUND_OR_EXPIRED"
        )

    def _operation_error(self, authority: ExecutionAuthority) -> ChannelConnectionsError:
        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.run_provider_operation(
                authority,
                ProviderOperationRequest(operation=ProviderOperation.LIST_SUBSCRIBERS),
            )
        return raised.exception


class BrokeredOperationTests(ExecutionFixture):
    def test_subscriber_rows_are_minimized_and_paged(self) -> None:
        authority = self.issue()

        first = self.service.run_provider_operation(
            authority,
            ProviderOperationRequest(
                operation=ProviderOperation.LIST_SUBSCRIBERS, max_results=2
            ),
        )

        self.assertEqual(len(first.rows), 2)
        self.assertEqual(
            [field for field in type(first.rows[0]).__slots__],
            ["subscriber_channel_id", "title", "api_published_at"],
        )
        self.assertIsNotNone(first.next_page_token)
        self.assertGreater(first.quota_cost, 0)

        second = self.service.run_provider_operation(
            authority,
            ProviderOperationRequest(
                operation=ProviderOperation.LIST_SUBSCRIBERS,
                page_token=first.next_page_token,
                max_results=2,
            ),
        )
        self.assertNotEqual(
            {row.subscriber_channel_id for row in first.rows},
            {row.subscriber_channel_id for row in second.rows},
        )

    def test_comment_rows_never_carry_text_or_author_names(self) -> None:
        authority = self.issue()

        result = self.service.run_provider_operation(
            authority,
            ProviderOperationRequest(
                operation=ProviderOperation.LIST_VIDEO_COMMENT_AUTHORS,
                video_id="video-1",
            ),
        )

        fields = set(type(result.rows[0]).__slots__)
        self.assertEqual(
            fields,
            {"video_id", "author_channel_id", "comment_count", "latest_comment_at"},
        )
        for forbidden in ("text", "display_name", "reply", "comment_id"):
            self.assertNotIn(forbidden, fields)

    def test_the_broker_passes_only_a_slot_reference_to_the_gateway(self) -> None:
        authority = self.issue()

        self.service.run_provider_operation(
            authority,
            ProviderOperationRequest(operation=ProviderOperation.LIST_VIDEOS),
        )

        call = self.data_gateway.calls[0]
        self.assertEqual(call.workspace_id, "workspace-1")
        self.assertTrue(call.credential_slot_id.startswith("cred_"))
        self.assertNotIn("authority", call.credential_slot_id)

    def test_a_comment_operation_requires_a_video_id(self) -> None:
        authority = self.issue()

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.run_provider_operation(
                authority,
                ProviderOperationRequest(
                    operation=ProviderOperation.LIST_VIDEO_COMMENT_AUTHORS
                ),
            )

        self.assertEqual(raised.exception.code, "INVALID_INPUT")
        self.assertEqual(raised.exception.field, "video_id")

    def test_an_expired_grant_fails_closed_and_marks_reauth_required(self) -> None:
        authority = self.issue()
        self.data_gateway.failure = ProviderAuthorizationExpired()

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.run_provider_operation(
                authority,
                ProviderOperationRequest(operation=ProviderOperation.LIST_SUBSCRIBERS),
            )

        self.assertEqual(raised.exception.code, "CONNECTION_REAUTH_REQUIRED")
        self.assertEqual(
            self.service.get_connection(self.owner, self.connection.connection_id).status,
            ConnectionStatus.REAUTH_REQUIRED,
        )
        self.assertEqual(self.vault.slot_ids("workspace-1"), ())
        actions = [event.action.value for event in self.service._state.audit_events]
        self.assertIn("CONNECTION_REAUTH_REQUIRED", actions)

    def test_an_unknown_provider_outcome_stays_retryable(self) -> None:
        authority = self.issue()
        self.data_gateway.failure = ProviderUnavailable()

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.run_provider_operation(
                authority,
                ProviderOperationRequest(operation=ProviderOperation.LIST_SUBSCRIBERS),
            )

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertEqual(
            self.service.get_connection(self.owner, self.connection.connection_id).status,
            ConnectionStatus.ACTIVE,
        )

    def test_raw_provider_detail_never_reaches_the_caller(self) -> None:
        authority = self.issue()
        self.data_gateway.failure = RuntimeError("quota exceeded for ya29.secret")

        with self.assertRaises(ChannelConnectionsError) as raised:
            self.service.run_provider_operation(
                authority,
                ProviderOperationRequest(operation=ProviderOperation.LIST_SUBSCRIBERS),
            )

        self.assertNotIn("ya29", raised.exception.message)
        self.assertNotIn("quota", raised.exception.message.lower())


if __name__ == "__main__":
    unittest.main()
