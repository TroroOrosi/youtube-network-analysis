from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from inspect import signature

from channel_connections.errors import ChannelConnectionsError, ErrorCode
from channel_connections.models import (
    APPROVED_SCOPES,
    AuthorizationOperation,
    AuthorizationStart,
    BeginAuthorization,
    BeginReauthorization,
    CALLBACK_REPLAY_TTL,
    ChannelConnection,
    CompleteAuthorization,
    ConnectionAuditAction,
    ConnectionAuditEvent,
    ConnectionPage,
    ConnectionPageRequest,
    ConnectionProvider,
    ConnectionRetentionReport,
    ConnectionStatus,
    DeleteWorkspaceConnections,
    DisconnectConnection,
    IDEMPOTENCY_TTL,
    INTENT_TTL,
    ProviderCredential,
    RedactedSecret,
    ReportCredentialInvalidation,
    RevocationOutcome,
    VerifiedProviderGrant,
    YOUTUBE_READONLY_SCOPE,
)
from channel_connections.ports import (
    ConnectionManager,
    ConnectionPrivacyAdministrator,
    ConnectionReader,
    CredentialVault,
    EphemeralSecretStore,
    YouTubeAuthorizationGateway,
)
from workspace_access.models import AuditOutcome, WorkspaceContext


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def connection(**overrides: object) -> ChannelConnection:
    fields: dict[str, object] = {
        "connection_id": "connection-1",
        "workspace_id": "workspace-1",
        "provider": ConnectionProvider.YOUTUBE,
        "provider_channel_id": "UC_channel_1",
        "channel_title": "分析チャンネル",
        "status": ConnectionStatus.ACTIVE,
        "connected_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ChannelConnection(**fields)  # type: ignore[arg-type]


class ErrorContractTests(unittest.TestCase):
    def test_error_codes_are_stable_machine_values(self) -> None:
        self.assertEqual(
            [code.value for code in ErrorCode],
            [
                "INVALID_INPUT",
                "PERMISSION_DENIED",
                "CONNECTION_NOT_FOUND_OR_FORBIDDEN",
                "CONNECTION_ALREADY_EXISTS",
                "INTENT_NOT_FOUND_OR_EXPIRED",
                "CALLBACK_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
                "OPERATION_IN_PROGRESS",
                "PROVIDER_AUTHORIZATION_FAILED",
                "PROVIDER_CAPABILITY_MISSING",
                "REAUTH_CHANNEL_MISMATCH",
                "AUTHORITY_NOT_FOUND_OR_EXPIRED",
                "CONNECTION_REAUTH_REQUIRED",
                "INVALID_CURSOR",
                "CURSOR_EXPIRED",
            ],
        )

    def test_errors_expose_only_stable_safe_fields(self) -> None:
        error = ChannelConnectionsError(
            ErrorCode.PROVIDER_AUTHORIZATION_FAILED,
            message="Authorization could not be completed",
            field="command.code",
            retryable=True,
            reason_code="PROVIDER_DENIED",
        )

        self.assertEqual(error.code, "PROVIDER_AUTHORIZATION_FAILED")
        self.assertEqual(error.message, "Authorization could not be completed")
        self.assertEqual(error.field, "command.code")
        self.assertTrue(error.retryable)
        self.assertEqual(error.reason_code, "PROVIDER_DENIED")
        self.assertTrue(error.correlation_id.startswith("error_"))

    def test_correlation_ids_are_generated_per_error(self) -> None:
        first = ChannelConnectionsError(ErrorCode.INVALID_INPUT, message="invalid")
        second = ChannelConnectionsError(ErrorCode.INVALID_INPUT, message="invalid")

        self.assertNotEqual(first.correlation_id, second.correlation_id)


class ScopeConstantTests(unittest.TestCase):
    def test_only_the_read_only_youtube_scope_is_approved(self) -> None:
        self.assertEqual(
            YOUTUBE_READONLY_SCOPE,
            "https://www.googleapis.com/auth/youtube.readonly",
        )
        self.assertEqual(APPROVED_SCOPES, (YOUTUBE_READONLY_SCOPE,))

    def test_retention_boundaries_match_the_approved_policy(self) -> None:
        self.assertEqual(INTENT_TTL, timedelta(minutes=10))
        self.assertEqual(CALLBACK_REPLAY_TTL, timedelta(hours=24))
        self.assertEqual(IDEMPOTENCY_TTL, timedelta(days=90))


class EnumContractTests(unittest.TestCase):
    def test_provider_status_and_operation_values_are_stable(self) -> None:
        self.assertEqual([item.value for item in ConnectionProvider], ["YOUTUBE"])
        self.assertEqual(
            [item.value for item in ConnectionStatus],
            ["ACTIVE", "REAUTH_REQUIRED"],
        )
        self.assertEqual(
            [item.value for item in AuthorizationOperation],
            ["CONNECT", "REAUTHORIZE"],
        )
        self.assertEqual(
            [item.value for item in RevocationOutcome],
            ["REVOKED", "NOT_CONFIRMED"],
        )

    def test_audit_actions_match_the_approved_list(self) -> None:
        self.assertEqual(
            [item.value for item in ConnectionAuditAction],
            [
                "AUTHORIZATION_STARTED",
                "CONNECTION_ESTABLISHED",
                "REAUTHORIZATION_STARTED",
                "CONNECTION_REAUTHORIZED",
                "CONNECTION_REAUTH_REQUIRED",
                "CONNECTION_DISCONNECTED",
                "WORKSPACE_CONNECTIONS_DELETED",
            ],
        )


class ChannelConnectionContractTests(unittest.TestCase):
    def test_connection_is_immutable_and_slotted(self) -> None:
        value = connection()

        with self.assertRaises(FrozenInstanceError):
            value.status = ConnectionStatus.REAUTH_REQUIRED  # type: ignore[misc]
        self.assertFalse(hasattr(value, "__dict__"))

    def test_connection_normalizes_datetimes_to_utc(self) -> None:
        local = datetime(2026, 8, 20, 21, 0, tzinfo=timezone(timedelta(hours=9)))

        value = connection(connected_at=local, updated_at=local)

        self.assertEqual(value.connected_at.tzinfo, UTC)
        self.assertEqual(value.connected_at, NOW)

    def test_connection_rejects_naive_datetimes(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            connection(connected_at=datetime(2026, 8, 20, 12, 0))

        self.assertEqual(raised.exception.code, "INVALID_INPUT")
        self.assertEqual(raised.exception.field, "connected_at")

    def test_connection_rejects_unbounded_identifiers_and_titles(self) -> None:
        for field, value in (
            ("connection_id", ""),
            ("workspace_id", " workspace-1"),
            ("provider_channel_id", "UC" + "x" * 300),
            ("channel_title", "t" * 501),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ChannelConnectionsError) as raised:
                    connection(**{field: value})
                self.assertEqual(raised.exception.field, field)

    def test_connection_rejects_unsupported_enum_values(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            connection(status="ACTIVE")

        self.assertEqual(raised.exception.field, "status")

    def test_connection_never_exposes_credential_material(self) -> None:
        rendered = repr(connection())

        for forbidden in ("token", "secret", "slot", "verifier", "state"):
            self.assertNotIn(forbidden, rendered.lower())


class AuthorizationStartTests(unittest.TestCase):
    def test_start_requires_a_bounded_https_authorization_url(self) -> None:
        start = AuthorizationStart(
            intent_id="intent-1",
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth?state=x",
            expires_at=NOW + INTENT_TTL,
        )

        self.assertEqual(start.expires_at.tzinfo, UTC)
        self.assertFalse(hasattr(start, "__dict__"))

    def test_start_rejects_non_https_urls(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            AuthorizationStart(
                intent_id="intent-1",
                authorization_url="http://accounts.google.com/o/oauth2/v2/auth",
                expires_at=NOW,
            )

        self.assertEqual(raised.exception.field, "authorization_url")


class PaginationContractTests(unittest.TestCase):
    def test_page_request_defaults_to_fifty(self) -> None:
        self.assertEqual(ConnectionPageRequest().limit, 50)
        self.assertIsNone(ConnectionPageRequest().cursor)

    def test_page_request_enforces_one_to_one_hundred(self) -> None:
        ConnectionPageRequest(limit=1)
        ConnectionPageRequest(limit=100)
        for limit in (0, 101, True, 2.0):
            with self.subTest(limit=limit):
                with self.assertRaises(ChannelConnectionsError) as raised:
                    ConnectionPageRequest(limit=limit)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.field, "limit")

    def test_page_request_rejects_unbounded_cursors(self) -> None:
        for cursor in ("", " token", "c" * 513):
            with self.subTest(cursor=cursor):
                with self.assertRaises(ChannelConnectionsError) as raised:
                    ConnectionPageRequest(cursor=cursor)
                self.assertEqual(raised.exception.field, "cursor")

    def test_page_holds_an_immutable_tuple(self) -> None:
        page = ConnectionPage(items=(connection(),), next_cursor=None)

        self.assertIsInstance(page.items, tuple)
        with self.assertRaises(FrozenInstanceError):
            page.next_cursor = "token"  # type: ignore[misc]

    def test_page_rejects_a_list_of_items(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            ConnectionPage(items=[connection()], next_cursor=None)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.field, "items")


class RedactedValueTests(unittest.TestCase):
    def test_secret_rendering_is_always_redacted(self) -> None:
        secret = RedactedSecret("synthetic-refresh-token")

        self.assertNotIn("synthetic-refresh-token", repr(secret))
        self.assertNotIn("synthetic-refresh-token", str(secret))
        self.assertIn("REDACTED", repr(secret))
        self.assertEqual(secret.reveal(), "synthetic-refresh-token")

    def test_provider_credential_rendering_hides_every_token(self) -> None:
        credential = ProviderCredential(
            access_token=RedactedSecret("synthetic-access-token"),
            refresh_token=RedactedSecret("synthetic-refresh-token"),
            expires_at=NOW + timedelta(hours=1),
            scopes=APPROVED_SCOPES,
        )

        rendered = f"{credential!r} {credential}"
        self.assertNotIn("synthetic-access-token", rendered)
        self.assertNotIn("synthetic-refresh-token", rendered)
        self.assertIn("REDACTED", rendered)

    def test_verified_grant_rendering_hides_credential_material(self) -> None:
        grant = VerifiedProviderGrant(
            provider=ConnectionProvider.YOUTUBE,
            provider_channel_id="UC_channel_1",
            channel_title="分析チャンネル",
            granted_scopes=APPROVED_SCOPES,
            credential=ProviderCredential(
                access_token=RedactedSecret("synthetic-access-token"),
                refresh_token=RedactedSecret("synthetic-refresh-token"),
                expires_at=NOW + timedelta(hours=1),
                scopes=APPROVED_SCOPES,
            ),
            subscriber_capability_verified=True,
        )

        rendered = f"{grant!r} {grant}"
        self.assertNotIn("synthetic-access-token", rendered)
        self.assertNotIn("synthetic-refresh-token", rendered)

    def test_credential_requires_a_refresh_token_and_utc_expiry(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            ProviderCredential(
                access_token=RedactedSecret("synthetic-access-token"),
                refresh_token=RedactedSecret(""),
                expires_at=NOW,
                scopes=APPROVED_SCOPES,
            )

        self.assertEqual(raised.exception.field, "refresh_token")

    def test_credential_rejects_raw_string_tokens(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            ProviderCredential(
                access_token="synthetic-access-token",  # type: ignore[arg-type]
                refresh_token=RedactedSecret("synthetic-refresh-token"),
                expires_at=NOW,
                scopes=APPROVED_SCOPES,
            )

        self.assertEqual(raised.exception.field, "access_token")


class CommandContractTests(unittest.TestCase):
    def test_commands_require_bounded_idempotency_keys(self) -> None:
        commands = (
            lambda key: BeginAuthorization(idempotency_key=key),
            lambda key: BeginReauthorization(
                connection_id="connection-1", idempotency_key=key
            ),
            lambda key: DisconnectConnection(
                connection_id="connection-1", idempotency_key=key
            ),
            lambda key: ReportCredentialInvalidation(
                connection_id="connection-1", idempotency_key=key
            ),
            lambda key: DeleteWorkspaceConnections(idempotency_key=key),
        )
        for build in commands:
            with self.subTest(command=build):
                build("key-1")
                with self.assertRaises(ChannelConnectionsError) as raised:
                    build("")
                self.assertEqual(raised.exception.field, "idempotency_key")

    def test_callback_command_requires_a_redacted_state(self) -> None:
        command = CompleteAuthorization(
            state=RedactedSecret("synthetic-state"),
            code=RedactedSecret("synthetic-code"),
            idempotency_key="key-1",
        )

        self.assertNotIn("synthetic-state", repr(command))
        self.assertNotIn("synthetic-code", repr(command))

        with self.assertRaises(ChannelConnectionsError) as raised:
            CompleteAuthorization(
                state="synthetic-state",  # type: ignore[arg-type]
                code=RedactedSecret("synthetic-code"),
                idempotency_key="key-1",
            )
        self.assertEqual(raised.exception.field, "state")

    def test_callback_command_requires_exactly_one_provider_outcome(self) -> None:
        CompleteAuthorization(
            state=RedactedSecret("synthetic-state"),
            provider_error="access_denied",
            idempotency_key="key-1",
        )

        for code, provider_error in (
            (None, None),
            (RedactedSecret("synthetic-code"), "access_denied"),
        ):
            with self.subTest(code=code, provider_error=provider_error):
                with self.assertRaises(ChannelConnectionsError) as raised:
                    CompleteAuthorization(
                        state=RedactedSecret("synthetic-state"),
                        code=code,
                        provider_error=provider_error,
                        idempotency_key="key-1",
                    )
                self.assertEqual(raised.exception.code, "INVALID_INPUT")

    def test_callback_command_bounds_provider_error_codes(self) -> None:
        for provider_error in ("", "e" * 257, "access denied"):
            with self.subTest(provider_error=provider_error):
                with self.assertRaises(ChannelConnectionsError) as raised:
                    CompleteAuthorization(
                        state=RedactedSecret("synthetic-state"),
                        provider_error=provider_error,
                        idempotency_key="key-1",
                    )
                self.assertEqual(raised.exception.field, "provider_error")

    def test_callback_command_bounds_secret_values(self) -> None:
        with self.assertRaises(ChannelConnectionsError) as raised:
            CompleteAuthorization(
                state=RedactedSecret("s" * 2049),
                code=RedactedSecret("synthetic-code"),
                idempotency_key="key-1",
            )

        self.assertEqual(raised.exception.field, "state")


class AuditAndRetentionContractTests(unittest.TestCase):
    def test_audit_event_carries_only_safe_opaque_fields(self) -> None:
        event = ConnectionAuditEvent(
            event_id="event-1",
            workspace_id="workspace-1",
            actor_user_id="user-1",
            action=ConnectionAuditAction.CONNECTION_ESTABLISHED,
            outcome=AuditOutcome.SUCCEEDED,
            occurred_at=NOW,
            correlation_id="correlation-1",
            connection_id="connection-1",
        )

        self.assertEqual(
            [field for field in ConnectionAuditEvent.__slots__],
            [
                "event_id",
                "workspace_id",
                "actor_user_id",
                "action",
                "outcome",
                "occurred_at",
                "correlation_id",
                "connection_id",
                "intent_id",
            ],
        )
        self.assertIsNone(event.intent_id)
        self.assertEqual(event.occurred_at.tzinfo, UTC)

    def test_retention_report_counts_are_non_negative_integers(self) -> None:
        report = ConnectionRetentionReport(
            intents_removed=1,
            callback_replays_removed=0,
            idempotency_records_removed=2,
            ephemeral_slots_removed=1,
            credential_slots_removed=0,
        )

        self.assertEqual(report.intents_removed, 1)
        with self.assertRaises(ChannelConnectionsError) as raised:
            ConnectionRetentionReport(
                intents_removed=-1,
                callback_replays_removed=0,
                idempotency_records_removed=0,
                ephemeral_slots_removed=0,
                credential_slots_removed=0,
            )
        self.assertEqual(raised.exception.field, "intents_removed")


class PortContractTests(unittest.TestCase):
    def test_every_tenant_operation_takes_a_workspace_context(self) -> None:
        tenant_operations = (
            (ConnectionReader, ("get_connection", "list_connections")),
            (
                ConnectionManager,
                (
                    "begin_authorization",
                    "complete_authorization",
                    "begin_reauthorization",
                    "disconnect",
                    "report_credential_invalidation",
                ),
            ),
            (
                ConnectionPrivacyAdministrator,
                ("delete_workspace_connections", "purge_retention"),
            ),
        )
        for protocol, methods in tenant_operations:
            for name in methods:
                with self.subTest(protocol=protocol.__name__, method=name):
                    parameters = signature(getattr(protocol, name)).parameters
                    self.assertEqual(list(parameters)[:2], ["self", "context"])
                    self.assertEqual(
                        parameters["context"].annotation,
                        WorkspaceContext.__name__,
                    )

    def test_credential_ports_are_workspace_scoped(self) -> None:
        for protocol, name in (
            (EphemeralSecretStore, "put"),
            (EphemeralSecretStore, "take"),
            (EphemeralSecretStore, "delete"),
            (CredentialVault, "put"),
            (CredentialVault, "delete"),
            (YouTubeAuthorizationGateway, "revoke"),
        ):
            with self.subTest(protocol=protocol.__name__, method=name):
                parameters = list(signature(getattr(protocol, name)).parameters)
                self.assertEqual(parameters[:2], ["self", "workspace_id"])

    def test_no_public_port_returns_credential_material(self) -> None:
        for protocol in (
            ConnectionReader,
            ConnectionManager,
            ConnectionPrivacyAdministrator,
        ):
            with self.subTest(protocol=protocol.__name__):
                for name in dir(protocol):
                    self.assertNotIn(
                        name,
                        {"get_token", "reveal_credential", "export_credential"},
                    )


class PackageSurfaceTests(unittest.TestCase):
    def test_package_root_hides_secret_and_provider_values(self) -> None:
        import channel_connections

        self.assertEqual(
            set(channel_connections.__all__),
            {
                "ChannelConnectionsError",
                "ConnectionExecutionBroker",
                "ConnectionManager",
                "ConnectionPrivacyAdministrator",
                "ConnectionReader",
                "ErrorCode",
            },
        )
        for hidden in (
            "RedactedSecret",
            "ProviderCredential",
            "VerifiedProviderGrant",
            "CredentialVault",
            "EphemeralSecretStore",
        ):
            self.assertNotIn(hidden, channel_connections.__all__)


if __name__ == "__main__":
    unittest.main()
