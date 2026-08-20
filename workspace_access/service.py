"""In-memory workspace-access orchestration with fail-closed sessions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

from .models import (
    AccessSecret,
    AuthenticatedSession,
    ErrorCode,
    IssuedSession,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceAccessError,
)
from .ports import Clock, SystemClock, SystemTokenSource, TokenSource


DEFAULT_IDLE_TIMEOUT = timedelta(minutes=30)
DEFAULT_ABSOLUTE_TIMEOUT = timedelta(hours=12)


@dataclass(slots=True)
class _UserState:
    user_id: str
    issuer: str
    subject: str
    verified_email: str | None
    display_name: str | None
    enabled: bool = True


@dataclass(slots=True)
class _SessionState:
    session_id: str
    user_id: str
    secret_digest: str
    authenticated_at: datetime
    last_used_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None = None


class WorkspaceAccessService:
    """Resolve verified identities into revocable server-side sessions."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        token_source: TokenSource | None = None,
        idle_timeout: timedelta = DEFAULT_IDLE_TIMEOUT,
        absolute_timeout: timedelta = DEFAULT_ABSOLUTE_TIMEOUT,
    ) -> None:
        if idle_timeout <= timedelta(0):
            raise ValueError("idle_timeout must be positive")
        if absolute_timeout <= timedelta(0):
            raise ValueError("absolute_timeout must be positive")
        if idle_timeout > absolute_timeout:
            raise ValueError("idle_timeout cannot exceed absolute_timeout")
        self._clock = clock or SystemClock()
        self._token_source = token_source or SystemTokenSource()
        self._idle_timeout = idle_timeout
        self._absolute_timeout = absolute_timeout
        self._lock = RLock()
        self._users_by_id: dict[str, _UserState] = {}
        self._user_id_by_identity: dict[tuple[str, str], str] = {}
        self._sessions_by_digest: dict[str, _SessionState] = {}
        self._session_digest_by_id: dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            "WorkspaceAccessService("
            f"users={len(self._users_by_id)}, "
            f"sessions={len(self._sessions_by_digest)})"
        )

    def establish_session(self, identity: VerifiedIdentity) -> IssuedSession:
        self._validate_identity(identity)
        with self._lock:
            now = self._now()
            identity_key = (identity.issuer, identity.subject)
            user_id = self._user_id_by_identity.get(identity_key)
            if user_id is None:
                user_id = self._token_source.new_id("user")
                user = _UserState(
                    user_id=user_id,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    verified_email=identity.verified_email,
                    display_name=identity.display_name,
                )
                self._users_by_id[user_id] = user
                self._user_id_by_identity[identity_key] = user_id
            else:
                user = self._users_by_id[user_id]
                if not user.enabled:
                    self._raise_unauthenticated()
                user.verified_email = identity.verified_email
                user.display_name = identity.display_name

            raw_secret = self._token_source.new_session_secret()
            if len(raw_secret) < 43:
                raise WorkspaceAccessError(
                    ErrorCode.INVALID_INPUT,
                    message="Session token source returned an invalid value",
                    field="session_secret",
                )
            digest = self._digest(raw_secret)
            if digest in self._sessions_by_digest:
                raise WorkspaceAccessError(
                    ErrorCode.INVALID_INPUT,
                    message="Session token source returned a duplicate value",
                    field="session_secret",
                )

            session_id = self._token_source.new_id("session")
            absolute_expires_at = now + self._absolute_timeout
            idle_expires_at = min(now + self._idle_timeout, absolute_expires_at)
            state = _SessionState(
                session_id=session_id,
                user_id=user_id,
                secret_digest=digest,
                authenticated_at=now,
                last_used_at=now,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
            )
            self._sessions_by_digest[digest] = state
            self._session_digest_by_id[session_id] = digest
            return IssuedSession(
                session_id=session_id,
                secret=AccessSecret(raw_secret),
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
            )

    def authenticate_session(
        self,
        evidence: SessionEvidence,
    ) -> AuthenticatedSession:
        digest = self._digest(evidence.secret.reveal())
        with self._lock:
            now = self._now()
            state = self._sessions_by_digest.get(digest)
            if state is None or state.revoked_at is not None:
                self._raise_expired_or_revoked()
            user = self._users_by_id.get(state.user_id)
            if user is None or not user.enabled:
                self._raise_unauthenticated()
            if now >= state.idle_expires_at or now >= state.absolute_expires_at:
                state.revoked_at = now
                self._raise_expired_or_revoked()

            state.last_used_at = now
            state.idle_expires_at = min(
                now + self._idle_timeout,
                state.absolute_expires_at,
            )
            return self._authenticated_session(state)

    def logout(self, evidence: SessionEvidence) -> None:
        digest = self._digest(evidence.secret.reveal())
        with self._lock:
            state = self._sessions_by_digest.get(digest)
            if state is not None and state.revoked_at is None:
                state.revoked_at = self._now()

    def revoke_session(
        self,
        session: AuthenticatedSession,
        target_session_id: str,
    ) -> None:
        with self._lock:
            actor_state = self._require_active_session(session)
            target_digest = self._session_digest_by_id.get(target_session_id)
            target = (
                self._sessions_by_digest.get(target_digest)
                if target_digest is not None
                else None
            )
            if (
                target is not None
                and target.user_id == actor_state.user_id
                and target.revoked_at is None
            ):
                target.revoked_at = self._now()

    def revoke_all_user_sessions(self, session: AuthenticatedSession) -> None:
        with self._lock:
            actor_state = self._require_active_session(session)
            now = self._now()
            for state in self._sessions_by_digest.values():
                if state.user_id == actor_state.user_id and state.revoked_at is None:
                    state.revoked_at = now

    def suspend_user_for_security(self, user_id: str) -> None:
        """Trusted security signal; not a browser/API administration method."""

        with self._lock:
            user = self._users_by_id.get(user_id)
            if user is None:
                return
            user.enabled = False
            now = self._now()
            for state in self._sessions_by_digest.values():
                if state.user_id == user_id and state.revoked_at is None:
                    state.revoked_at = now

    def _require_active_session(
        self,
        session: AuthenticatedSession,
    ) -> _SessionState:
        digest = self._session_digest_by_id.get(session.session_id)
        state = self._sessions_by_digest.get(digest) if digest is not None else None
        now = self._now()
        if (
            state is None
            or state.user_id != session.user_id
            or state.revoked_at is not None
            or now >= state.idle_expires_at
            or now >= state.absolute_expires_at
        ):
            self._raise_unauthenticated()
        user = self._users_by_id.get(state.user_id)
        if user is None or not user.enabled:
            self._raise_unauthenticated()
        return state

    def _authenticated_session(self, state: _SessionState) -> AuthenticatedSession:
        return AuthenticatedSession(
            session_id=state.session_id,
            user_id=state.user_id,
            authenticated_at=state.authenticated_at,
            idle_expires_at=state.idle_expires_at,
            absolute_expires_at=state.absolute_expires_at,
        )

    def _now(self) -> datetime:
        value = self._clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Clock must return a timezone-aware value",
                field="clock",
            )
        return value.astimezone(UTC)

    @staticmethod
    def _digest(raw_secret: str) -> str:
        return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_identity(identity: VerifiedIdentity) -> None:
        if not identity.issuer.strip() or len(identity.issuer) > 2048:
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Identity issuer is invalid",
                field="issuer",
            )
        if not identity.subject.strip() or len(identity.subject) > 512:
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Identity subject is invalid",
                field="subject",
            )

    @staticmethod
    def _raise_unauthenticated() -> None:
        raise WorkspaceAccessError(
            ErrorCode.UNAUTHENTICATED,
            message="Authentication is required",
        )

    @staticmethod
    def _raise_expired_or_revoked() -> None:
        raise WorkspaceAccessError(
            ErrorCode.SESSION_EXPIRED_OR_REVOKED,
            message="Session is expired or revoked",
        )

    def _debug_session_digests(self) -> frozenset[str]:
        """Test-only view proving raw session values are not retained."""

        with self._lock:
            return frozenset(self._sessions_by_digest)
