"""Tenant-scoped in-memory orchestration for channel connections.

Every public method resolves an exact permission from the supplied
`WorkspaceContext`, keys all state under that workspace, and serializes state
transitions with one lock. Provider endpoints, redirect identifiers, and the
requested scope are trusted server configuration; no caller value can influence
them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from workspace_access.models import AuditOutcome, Permission, WorkspaceContext

from .errors import ChannelConnectionsError, ErrorCode
from .memory import (
    AuthorizationIntent,
    CleanupRecord,
    CursorRecord,
    IdempotencyRecord,
    MemoryState,
    StartedIntentRef,
)
from .models import (
    APPROVED_SCOPES,
    AuthorizationFailureReason,
    AuthorizationOperation,
    AuthorizationStart,
    BeginAuthorization,
    CALLBACK_REPLAY_TTL,
    ChannelConnection,
    CompleteAuthorization,
    ConnectionAuditAction,
    ConnectionPage,
    ConnectionPageRequest,
    ConnectionAuditEvent,
    ConnectionProvider,
    ConnectionStatus,
    IDEMPOTENCY_TTL,
    RevocationOutcome,
    INTENT_TTL,
    MAX_IDENTIFIER_LENGTH,
    RedactedSecret,
    VerifiedProviderGrant,
)
from .ports import (
    Clock,
    ConnectionAuditSink,
    CredentialVault,
    EphemeralSecretStore,
    ProviderRejected,
    ProviderUnavailable,
    TokenGenerator,
    YouTubeAuthorizationGateway,
)


DEFAULT_REDIRECT_URI_ID = "hosted-callback"
_CONNECTION_ORDER = "connected_at_desc,connection_id_asc"
PROVIDER_AUTHORIZATION_HOSTS = ("accounts.google.com",)

_MESSAGES = {
    ErrorCode.INVALID_INPUT: "The request contains an unsupported value",
    ErrorCode.PERMISSION_DENIED: "The current role does not allow this operation",
    ErrorCode.CONNECTION_NOT_FOUND_OR_FORBIDDEN: "The connection is unavailable",
    ErrorCode.CONNECTION_ALREADY_EXISTS: "The channel is already connected",
    ErrorCode.INTENT_NOT_FOUND_OR_EXPIRED: "The authorization request is unavailable",
    ErrorCode.CALLBACK_CONFLICT: "The callback does not match the original request",
    ErrorCode.IDEMPOTENCY_CONFLICT: "The idempotency key was reused with a different request",
    ErrorCode.OPERATION_IN_PROGRESS: "Another operation is already in progress",
    ErrorCode.PROVIDER_AUTHORIZATION_FAILED: "Authorization could not be completed",
    ErrorCode.PROVIDER_CAPABILITY_MISSING: "The channel is missing a required capability",
    ErrorCode.REAUTH_CHANNEL_MISMATCH: "The authorized channel does not match",
    ErrorCode.INVALID_CURSOR: "The page cursor is unavailable",
    ErrorCode.CURSOR_EXPIRED: "The page cursor is no longer current",
}


def _safe_error(
    code: ErrorCode,
    *,
    field: str | None = None,
    retryable: bool = False,
    reason_code: str | None = None,
) -> ChannelConnectionsError:
    return ChannelConnectionsError(
        code,
        message=_MESSAGES[code],
        field=field,
        retryable=retryable,
        reason_code=reason_code,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_failure(reason: AuthorizationFailureReason) -> ChannelConnectionsError:
    if reason is AuthorizationFailureReason.SUBSCRIBER_CAPABILITY_MISSING:
        return _safe_error(ErrorCode.PROVIDER_CAPABILITY_MISSING, reason_code=reason.value)
    return _safe_error(ErrorCode.PROVIDER_AUTHORIZATION_FAILED, reason_code=reason.value)


def _credential_slot(intent: AuthorizationIntent) -> str:
    return f"cred_{intent.intent_id}"


def _allowlisted_authorization_url(value: object) -> str:
    if not isinstance(value, str):
        raise _safe_error(ErrorCode.PROVIDER_AUTHORIZATION_FAILED)
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.hostname not in PROVIDER_AUTHORIZATION_HOSTS
        or parts.port is not None
    ):
        raise _safe_error(ErrorCode.PROVIDER_AUTHORIZATION_FAILED)
    return value


class ChannelConnectionsService:
    """In-memory reference implementation of the connection lifecycle."""

    def __init__(
        self,
        *,
        clock: Clock,
        tokens: TokenGenerator,
        gateway: YouTubeAuthorizationGateway,
        ephemeral_secrets: EphemeralSecretStore,
        credential_vault: CredentialVault,
        audit_sink: ConnectionAuditSink | None = None,
        redirect_uri_id: str = DEFAULT_REDIRECT_URI_ID,
        provider: ConnectionProvider = ConnectionProvider.YOUTUBE,
    ) -> None:
        self._clock = clock
        self._tokens = tokens
        self._gateway = gateway
        self._ephemeral = ephemeral_secrets
        self._vault = credential_vault
        self._audit_sink = audit_sink
        self._redirect_uri_id = redirect_uri_id
        self._provider = provider
        self._lock = RLock()
        self._state = MemoryState()

    # Authorization

    def begin_authorization(
        self, context: WorkspaceContext, command: BeginAuthorization
    ) -> AuthorizationStart:
        _require(context, Permission.CHANNEL_MANAGE_CONNECTION)
        return self._begin(
            context,
            operation=AuthorizationOperation.CONNECT,
            target_connection_id=None,
            idempotency_key=command.idempotency_key,
        )

    def complete_authorization(
        self, context: WorkspaceContext, command: CompleteAuthorization
    ) -> ChannelConnection:
        _require(context, Permission.CHANNEL_MANAGE_CONNECTION)
        with self._lock:
            now = self._now()
            state_digest = _digest(command.state.reveal())
            record_key = (
                context.workspace_id,
                context.user_id,
                "complete",
                command.idempotency_key,
            )
            payload = {
                "session_id": context.session_id,
                "state_digest": state_digest,
                "code_digest": (
                    _digest(command.code.reveal()) if command.code is not None else None
                ),
                "provider_error": command.provider_error,
            }
            replayed = self._replay(
                record_key, payload, now, conflict=ErrorCode.CALLBACK_CONFLICT
            )
            if replayed is not None:
                return replayed

            intent = self._claim_intent(context, state_digest, now)
            if command.provider_error is not None:
                self._consume_intent(intent)
                raise _safe_error(
                    ErrorCode.PROVIDER_AUTHORIZATION_FAILED,
                    reason_code=AuthorizationFailureReason.PROVIDER_DENIED.value,
                )

            grant = self._exchange(intent, command, now)
            connection = self._publish(context, intent, grant, now)
            self._remember(record_key, payload, connection, now, CALLBACK_REPLAY_TTL)
            return connection

    # Reads

    def get_connection(
        self, context: WorkspaceContext, connection_id: str
    ) -> ChannelConnection:
        _require(context, Permission.CHANNEL_READ)
        with self._lock:
            return self._connection(context.workspace_id, connection_id)

    def list_connections(
        self,
        context: WorkspaceContext,
        page: ConnectionPageRequest = ConnectionPageRequest(),
    ) -> ConnectionPage:
        _require(context, Permission.CHANNEL_READ)
        with self._lock:
            now = self._now()
            workspace_id = context.workspace_id
            revision = self._state.workspace_revisions.get(workspace_id, 0)
            query = _fingerprint({"limit": page.limit, "order": _CONNECTION_ORDER})

            offset = 0
            if page.cursor is not None:
                record = self._state.cursors.get(page.cursor)
                if (
                    record is None
                    or record.workspace_id != workspace_id
                    or record.query_fingerprint != query
                ):
                    raise _safe_error(ErrorCode.INVALID_CURSOR)
                if record.revision != revision:
                    raise _safe_error(ErrorCode.CURSOR_EXPIRED)
                offset = record.offset
                del self._state.cursors[page.cursor]

            rows = self._ordered_connections(workspace_id)
            window = rows[offset : offset + page.limit]
            next_cursor = None
            if offset + page.limit < len(rows):
                next_cursor = f"cursor_{self._tokens.new_token()}"
                self._state.cursors[next_cursor] = CursorRecord(
                    workspace_id=workspace_id,
                    query_fingerprint=query,
                    revision=revision,
                    offset=offset + page.limit,
                    created_at=now,
                )
            return ConnectionPage(items=tuple(window), next_cursor=next_cursor)

    def _ordered_connections(self, workspace_id: str) -> list[ChannelConnection]:
        rows = [
            connection
            for connection in self._state.connections.values()
            if connection.workspace_id == workspace_id
        ]
        rows.sort(key=lambda connection: connection.connection_id)
        rows.sort(key=lambda connection: connection.connected_at, reverse=True)
        return rows

    def _bump_revision(self, workspace_id: str) -> None:
        self._state.workspace_revisions[workspace_id] = (
            self._state.workspace_revisions.get(workspace_id, 0) + 1
        )

    # Internal orchestration

    def _begin(
        self,
        context: WorkspaceContext,
        *,
        operation: AuthorizationOperation,
        target_connection_id: str | None,
        idempotency_key: str,
    ) -> AuthorizationStart:
        with self._lock:
            now = self._now()
            record_key = (
                context.workspace_id,
                context.user_id,
                f"begin:{operation.value}",
                idempotency_key,
            )
            payload = {
                "operation": operation.value,
                "session_id": context.session_id,
                "target_connection_id": target_connection_id,
            }
            replayed = self._replay(record_key, payload, now)
            if replayed is not None:
                return replayed

            intent_id = f"intent_{self._tokens.new_token()}"
            raw_state = self._tokens.new_token()
            verifier = self._tokens.new_token()
            slot_id = f"pkce_{intent_id}"
            expires_at = now + INTENT_TTL

            self._ephemeral.put(
                context.workspace_id, slot_id, RedactedSecret(verifier), expires_at
            )
            try:
                url = self._gateway.authorization_url(
                    state=RedactedSecret(raw_state),
                    code_challenge=_s256_challenge(verifier),
                    redirect_uri_id=self._redirect_uri_id,
                    scopes=APPROVED_SCOPES,
                )
                start = AuthorizationStart(
                    intent_id=intent_id,
                    authorization_url=_allowlisted_authorization_url(url),
                    expires_at=expires_at,
                )
            except BaseException:
                self._ephemeral.delete(context.workspace_id, slot_id)
                raise

            url_slot_id = f"url_{intent_id}"
            self._ephemeral.put(
                context.workspace_id,
                url_slot_id,
                RedactedSecret(start.authorization_url),
                expires_at,
            )
            state_digest = _digest(raw_state)
            intent = AuthorizationIntent(
                intent_id=intent_id,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                session_id=context.session_id,
                operation=operation,
                provider=self._provider,
                redirect_uri_id=self._redirect_uri_id,
                target_connection_id=target_connection_id,
                state_digest=state_digest,
                verifier_slot_id=slot_id,
                created_at=now,
                expires_at=expires_at,
            )
            intent_key = _key(context.workspace_id, intent_id)
            self._state.intents[intent_key] = intent
            self._state.intents_by_state[state_digest] = intent_key
            self._remember(
                record_key,
                payload,
                StartedIntentRef(intent_id=intent_id, expires_at=expires_at),
                now,
                IDEMPOTENCY_TTL,
            )
            self._record_audit(
                context,
                action=(
                    ConnectionAuditAction.AUTHORIZATION_STARTED
                    if operation is AuthorizationOperation.CONNECT
                    else ConnectionAuditAction.REAUTHORIZATION_STARTED
                ),
                occurred_at=now,
                intent_id=intent_id,
                connection_id=target_connection_id,
            )
            return start

    def _claim_intent(
        self, context: WorkspaceContext, state_digest: str, now: datetime
    ) -> AuthorizationIntent:
        intent_key = self._state.intents_by_state.get(state_digest)
        intent = None if intent_key is None else self._state.intents.get(intent_key)
        if intent is None:
            raise _safe_error(ErrorCode.INTENT_NOT_FOUND_OR_EXPIRED)
        if intent.expires_at <= now:
            self._forget_intent(intent.workspace_id, intent.intent_id)
            raise _safe_error(ErrorCode.INTENT_NOT_FOUND_OR_EXPIRED)
        if intent.workspace_id != context.workspace_id:
            raise _safe_error(ErrorCode.INTENT_NOT_FOUND_OR_EXPIRED)
        if not (
            hmac.compare_digest(intent.user_id, context.user_id)
            and hmac.compare_digest(intent.session_id, context.session_id)
        ):
            raise _safe_error(ErrorCode.CALLBACK_CONFLICT)
        if intent.claimed_at is not None:
            raise _safe_error(ErrorCode.OPERATION_IN_PROGRESS, retryable=True)
        intent.claimed_at = now
        return intent

    def _exchange(
        self,
        intent: AuthorizationIntent,
        command: CompleteAuthorization,
        now: datetime,
    ) -> VerifiedProviderGrant:
        workspace_id = intent.workspace_id
        try:
            verifier = self._ephemeral.take(workspace_id, intent.verifier_slot_id)
        except LookupError:
            self._consume_intent(intent)
            raise _safe_error(ErrorCode.INTENT_NOT_FOUND_OR_EXPIRED) from None

        try:
            grant = self._gateway.exchange_and_verify(
                code=command.code,
                code_verifier=verifier,
                redirect_uri_id=intent.redirect_uri_id,
            )
        except ProviderRejected as rejected:
            self._consume_intent(intent)
            raise _provider_failure(rejected.reason) from None
        except BaseException:
            self._record_cleanup(
                workspace_id, "PROVIDER_OUTCOME_UNKNOWN", _credential_slot(intent), now
            )
            raise _safe_error(
                ErrorCode.PROVIDER_AUTHORIZATION_FAILED,
                retryable=True,
                reason_code=AuthorizationFailureReason.PROVIDER_UNAVAILABLE.value,
            ) from None

        if not isinstance(grant, VerifiedProviderGrant):
            self._consume_intent(intent)
            raise _provider_failure(AuthorizationFailureReason.INVALID_PROVIDER_RESPONSE)
        return grant

    def _publish(
        self,
        context: WorkspaceContext,
        intent: AuthorizationIntent,
        grant: VerifiedProviderGrant,
        now: datetime,
    ) -> ChannelConnection:
        workspace_id = context.workspace_id
        slot_id = _credential_slot(intent)
        try:
            self._vault.put(workspace_id, slot_id, grant.credential)
        except BaseException:
            self._consume_intent(intent)
            self._record_cleanup(workspace_id, "CREDENTIAL_WRITE_UNKNOWN", slot_id, now)
            raise _safe_error(
                ErrorCode.PROVIDER_AUTHORIZATION_FAILED, retryable=True
            ) from None

        failure = self._verify_grant(grant, intent)
        if failure is not None:
            self._revoke_slot(workspace_id, slot_id, now)
            self._consume_intent(intent)
            raise failure

        active_key = (workspace_id, grant.provider.value, grant.provider_channel_id)
        if active_key in self._state.active_keys:
            self._revoke_slot(workspace_id, slot_id, now)
            self._consume_intent(intent)
            raise _safe_error(ErrorCode.CONNECTION_ALREADY_EXISTS)

        connection_id = f"connection_{self._tokens.new_token()}"
        connection = ChannelConnection(
            connection_id=connection_id,
            workspace_id=workspace_id,
            provider=grant.provider,
            provider_channel_id=grant.provider_channel_id,
            channel_title=grant.channel_title,
            status=ConnectionStatus.ACTIVE,
            connected_at=now,
            updated_at=now,
        )
        connection_key = _key(workspace_id, connection_id)
        self._state.connections[connection_key] = connection
        self._state.credential_slots[connection_key] = slot_id
        self._state.active_keys[active_key] = connection_id
        self._bump_revision(workspace_id)
        self._consume_intent(intent)
        self._record_audit(
            context,
            action=ConnectionAuditAction.CONNECTION_ESTABLISHED,
            occurred_at=now,
            intent_id=intent.intent_id,
            connection_id=connection_id,
        )
        return connection

    def _verify_grant(
        self, grant: VerifiedProviderGrant, intent: AuthorizationIntent
    ) -> ChannelConnectionsError | None:
        if grant.provider is not intent.provider:
            return _provider_failure(AuthorizationFailureReason.INVALID_PROVIDER_RESPONSE)
        if tuple(grant.granted_scopes) != APPROVED_SCOPES:
            return _provider_failure(AuthorizationFailureReason.SCOPE_NOT_GRANTED)
        if grant.credential.refresh_token is None:
            return _provider_failure(AuthorizationFailureReason.OFFLINE_CREDENTIAL_MISSING)
        if not grant.subscriber_capability_verified:
            return _provider_failure(
                AuthorizationFailureReason.SUBSCRIBER_CAPABILITY_MISSING
            )
        return None

    def _revoke_slot(self, workspace_id: str, slot_id: str, now: datetime) -> None:
        try:
            outcome = self._gateway.revoke(workspace_id, slot_id)
        except BaseException:
            outcome = None
        if outcome is not RevocationOutcome.REVOKED:
            self._record_cleanup(workspace_id, "REVOCATION_UNCONFIRMED", slot_id, now)
        self._vault.delete(workspace_id, slot_id)

    def _record_cleanup(
        self,
        workspace_id: str,
        kind: str,
        credential_slot_id: str | None,
        now: datetime,
    ) -> None:
        cleanup_id = f"cleanup_{self._tokens.new_token()}"
        self._state.cleanups[cleanup_id] = CleanupRecord(
            cleanup_id=cleanup_id,
            workspace_id=workspace_id,
            kind=kind,
            credential_slot_id=credential_slot_id,
            recorded_at=now,
        )

    def _consume_intent(self, intent: AuthorizationIntent) -> None:
        self._forget_intent(intent.workspace_id, intent.intent_id)

    def _connection(self, workspace_id: str, connection_id: object) -> ChannelConnection:
        if (
            not isinstance(connection_id, str)
            or not connection_id
            or len(connection_id) > MAX_IDENTIFIER_LENGTH
        ):
            raise _safe_error(ErrorCode.CONNECTION_NOT_FOUND_OR_FORBIDDEN)
        connection = self._state.connections.get(_key(workspace_id, connection_id))
        if connection is None:
            raise _safe_error(ErrorCode.CONNECTION_NOT_FOUND_OR_FORBIDDEN)
        return connection

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise _safe_error(ErrorCode.INVALID_INPUT, field="clock")
        return now

    def _replay(
        self,
        record_key: tuple[str, str, str, str],
        payload: dict[str, Any],
        now: datetime,
        conflict: ErrorCode = ErrorCode.IDEMPOTENCY_CONFLICT,
    ) -> Any:
        record = self._state.idempotency.get(record_key)
        if record is None:
            return None
        if record.fingerprint != _fingerprint(payload):
            raise _safe_error(conflict, field="idempotency_key")
        if record.expires_at <= now:
            del self._state.idempotency[record_key]
            return None
        result = record.result
        if isinstance(result, StartedIntentRef):
            return self._restore_start(record_key, result, now)
        return result

    def _restore_start(
        self,
        record_key: tuple[str, str, str, str],
        reference: StartedIntentRef,
        now: datetime,
    ) -> AuthorizationStart | None:
        workspace_id = record_key[0]
        stored_url = None
        if reference.expires_at > now:
            stored_url = self._ephemeral.peek(
                workspace_id, f"url_{reference.intent_id}"
            )
        if stored_url is None:
            del self._state.idempotency[record_key]
            self._forget_intent(workspace_id, reference.intent_id)
            return None
        return AuthorizationStart(
            intent_id=reference.intent_id,
            authorization_url=stored_url.reveal(),
            expires_at=reference.expires_at,
        )

    def _remember(
        self,
        record_key: tuple[str, str, str, str],
        payload: dict[str, Any],
        result: Any,
        now: datetime,
        ttl: timedelta,
    ) -> None:
        self._state.idempotency[record_key] = IdempotencyRecord(
            fingerprint=_fingerprint(payload),
            result=result,
            recorded_at=now,
            expires_at=now + ttl,
        )

    def _forget_intent(self, workspace_id: str, intent_id: str) -> None:
        intent = self._state.intents.pop(_key(workspace_id, intent_id), None)
        if intent is None:
            return
        self._state.intents_by_state.pop(intent.state_digest, None)
        self._ephemeral.delete(workspace_id, intent.verifier_slot_id)
        self._ephemeral.delete(workspace_id, f"url_{intent.intent_id}")

    def _record_audit(
        self,
        context: WorkspaceContext,
        *,
        action: ConnectionAuditAction,
        occurred_at: datetime,
        intent_id: str | None = None,
        connection_id: str | None = None,
    ) -> None:
        event = ConnectionAuditEvent(
            event_id=f"event_{self._tokens.new_token()}",
            workspace_id=context.workspace_id,
            actor_user_id=context.user_id,
            action=action,
            outcome=AuditOutcome.SUCCEEDED,
            occurred_at=occurred_at,
            correlation_id=f"correlation_{self._tokens.new_token()}",
            connection_id=connection_id,
            intent_id=intent_id,
        )
        self._state.audit_events.append(event)
        if self._audit_sink is not None:
            self._audit_sink.record(event)


def _key(*parts: str) -> str:
    return "\x1f".join(parts)


def _require(context: WorkspaceContext, permission: Permission) -> None:
    if permission not in context.permissions:
        raise _safe_error(ErrorCode.PERMISSION_DENIED, field="context.permissions")
