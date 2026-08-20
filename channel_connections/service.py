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
import json
from datetime import datetime
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from workspace_access.models import AuditOutcome, Permission, WorkspaceContext

from .errors import ChannelConnectionsError, ErrorCode
from .memory import (
    AuthorizationIntent,
    IdempotencyRecord,
    MemoryState,
    StartedIntentRef,
)
from .models import (
    APPROVED_SCOPES,
    AuthorizationOperation,
    AuthorizationStart,
    BeginAuthorization,
    ConnectionAuditAction,
    ConnectionAuditEvent,
    ConnectionProvider,
    INTENT_TTL,
    RedactedSecret,
)
from .ports import (
    Clock,
    ConnectionAuditSink,
    CredentialVault,
    EphemeralSecretStore,
    TokenGenerator,
    YouTubeAuthorizationGateway,
)


DEFAULT_REDIRECT_URI_ID = "hosted-callback"
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
    ) -> Any:
        record = self._state.idempotency.get(record_key)
        if record is None:
            return None
        if record.fingerprint != _fingerprint(payload):
            raise _safe_error(
                ErrorCode.IDEMPOTENCY_CONFLICT, field="idempotency_key"
            )
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
    ) -> None:
        self._state.idempotency[record_key] = IdempotencyRecord(
            fingerprint=_fingerprint(payload),
            result=result,
            recorded_at=now,
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
