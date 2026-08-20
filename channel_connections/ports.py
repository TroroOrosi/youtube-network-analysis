"""Stable synchronous ports for tenant-scoped channel connections."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from workspace_access.models import WorkspaceContext

from .models import (
    AuthorizationFailureReason,
    AuthorizationStart,
    BeginAuthorization,
    BeginReauthorization,
    ChannelConnection,
    CompleteAuthorization,
    ConnectionAuditEvent,
    ConnectionPage,
    ConnectionPageRequest,
    ConnectionRetentionReport,
    DeleteWorkspaceConnections,
    DisconnectConnection,
    ProviderCredential,
    RedactedSecret,
    ReportCredentialInvalidation,
    RevocationOutcome,
    VerifiedProviderGrant,
)


class ProviderRejected(Exception):
    """A gateway definitively refused, carrying only a safe reason enum."""

    def __init__(self, reason: AuthorizationFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ProviderUnavailable(Exception):
    """A gateway could not determine the provider outcome, such as a timeout."""


class ConnectionReader(Protocol):
    def get_connection(
        self, context: WorkspaceContext, connection_id: str
    ) -> ChannelConnection: ...

    def list_connections(
        self,
        context: WorkspaceContext,
        page: ConnectionPageRequest = ConnectionPageRequest(),
    ) -> ConnectionPage: ...


class ConnectionManager(Protocol):
    def begin_authorization(
        self, context: WorkspaceContext, command: BeginAuthorization
    ) -> AuthorizationStart: ...

    def complete_authorization(
        self, context: WorkspaceContext, command: CompleteAuthorization
    ) -> ChannelConnection: ...

    def begin_reauthorization(
        self, context: WorkspaceContext, command: BeginReauthorization
    ) -> AuthorizationStart: ...

    def disconnect(
        self, context: WorkspaceContext, command: DisconnectConnection
    ) -> None: ...

    def report_credential_invalidation(
        self, context: WorkspaceContext, command: ReportCredentialInvalidation
    ) -> ChannelConnection: ...


class ConnectionPrivacyAdministrator(Protocol):
    def delete_workspace_connections(
        self, context: WorkspaceContext, command: DeleteWorkspaceConnections
    ) -> None: ...

    def purge_retention(
        self, context: WorkspaceContext, reference_time: datetime
    ) -> ConnectionRetentionReport: ...


class EphemeralSecretStore(Protocol):
    """Short-lived storage for one in-flight authorization transaction."""

    def put(
        self,
        workspace_id: str,
        slot_id: str,
        secret: RedactedSecret,
        expires_at: datetime,
    ) -> None: ...

    def take(self, workspace_id: str, slot_id: str) -> RedactedSecret: ...

    def peek(self, workspace_id: str, slot_id: str) -> RedactedSecret | None: ...

    def delete(self, workspace_id: str, slot_id: str) -> None: ...


class CredentialVault(Protocol):
    """Opaque credential custody; it never returns credential material here."""

    def put(
        self,
        workspace_id: str,
        slot_id: str,
        credential: ProviderCredential,
    ) -> None: ...

    def delete(self, workspace_id: str, slot_id: str) -> None: ...


class YouTubeAuthorizationGateway(Protocol):
    """Owns provider endpoints, transport, and response validation."""

    def authorization_url(
        self,
        *,
        state: RedactedSecret,
        code_challenge: str,
        redirect_uri_id: str,
        scopes: tuple[str, ...],
    ) -> str: ...

    def exchange_and_verify(
        self,
        *,
        code: RedactedSecret,
        code_verifier: RedactedSecret,
        redirect_uri_id: str,
    ) -> VerifiedProviderGrant: ...

    def revoke(
        self, workspace_id: str, credential_slot_id: str
    ) -> RevocationOutcome: ...


class ConnectionAuditSink(Protocol):
    def record(self, event: ConnectionAuditEvent) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class TokenGenerator(Protocol):
    def new_token(self) -> str: ...
