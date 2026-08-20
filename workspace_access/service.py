"""In-memory workspace-access orchestration with fail-closed sessions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from threading import RLock

from .errors import (
    raise_expired_or_revoked,
    raise_last_owner_required,
    raise_membership_not_found_or_forbidden,
    raise_no_accessible_workspace,
    raise_unauthenticated,
    raise_workspace_not_found_or_forbidden,
)
from .memory import IdempotencyRecord, InMemoryAuditLog, SessionState, UserState

from .models import (
    AccessSecret,
    AccountAccessExport,
    AccountMembershipExport,
    AuthenticatedSession,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    ChangeMembershipRole,
    CreateWorkspace,
    ErrorCode,
    GrantMembership,
    IssuedSession,
    Membership,
    Permission,
    RevokeMembership,
    RetentionReport,
    Role,
    SessionEvidence,
    VerifiedIdentity,
    Workspace,
    WorkspaceAccessError,
    WorkspaceContext,
    WorkspaceSelection,
    WorkspaceSummary,
    permissions_for_role,
)
from .ports import Clock, SystemClock, SystemTokenSource, TokenSource


DEFAULT_IDLE_TIMEOUT = timedelta(minutes=30)
DEFAULT_ABSOLUTE_TIMEOUT = timedelta(hours=12)


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
        self._users_by_id: dict[str, UserState] = {}
        self._user_id_by_identity: dict[tuple[str, str], str] = {}
        self._sessions_by_digest: dict[str, SessionState] = {}
        self._session_digest_by_id: dict[str, str] = {}
        self._workspaces_by_id: dict[str, Workspace] = {}
        self._memberships_by_id: dict[str, Membership] = {}
        self._membership_id_by_pair: dict[tuple[str, str], str] = {}
        self._preferred_workspace_by_user: dict[str, str] = {}
        self._idempotency_records: dict[
            tuple[str, str], IdempotencyRecord
        ] = {}
        self._audit_log = InMemoryAuditLog()

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
                user = UserState(
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
                    raise_unauthenticated()
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
            state = SessionState(
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
            self._audit(
                AuditCategory.SECURITY,
                AuditAction.SESSION_ESTABLISHED,
                actor_user_id=user_id,
                workspace_id=None,
                target_id=session_id,
            )
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
                raise_expired_or_revoked()
            user = self._users_by_id.get(state.user_id)
            if user is None or not user.enabled:
                raise_unauthenticated()
            if now >= state.idle_expires_at or now >= state.absolute_expires_at:
                state.revoked_at = now
                raise_expired_or_revoked()

            state.last_used_at = now
            state.idle_expires_at = min(
                now + self._idle_timeout,
                state.absolute_expires_at,
            )
            return self._authenticated_session(state)

    def create_workspace(
        self,
        session: AuthenticatedSession,
        command: CreateWorkspace,
    ) -> WorkspaceContext:
        name = command.name.strip()
        if not name or len(name) > 100:
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Workspace name is invalid",
                field="name",
            )
        with self._lock:
            actor = self._require_active_session(session)
            now = self._now()
            workspace = Workspace(
                workspace_id=self._token_source.new_id("workspace"),
                name=name,
                created_at=now,
                authorization_revision=1,
            )
            membership = Membership(
                membership_id=self._token_source.new_id("membership"),
                workspace_id=workspace.workspace_id,
                user_id=actor.user_id,
                role=Role.OWNER,
                created_at=now,
            )
            self._workspaces_by_id[workspace.workspace_id] = workspace
            self._memberships_by_id[membership.membership_id] = membership
            self._membership_id_by_pair[
                (workspace.workspace_id, actor.user_id)
            ] = membership.membership_id
            self._audit(
                AuditCategory.ADMINISTRATION,
                AuditAction.WORKSPACE_CREATED,
                actor_user_id=actor.user_id,
                workspace_id=workspace.workspace_id,
                target_id=workspace.workspace_id,
            )
            return self._context(
                state=actor,
                workspace=workspace,
                membership=membership,
                resolved_at=now,
            )

    def list_accessible_workspaces(
        self,
        session: AuthenticatedSession,
    ) -> tuple[WorkspaceSummary, ...]:
        with self._lock:
            actor = self._require_active_session(session)
            memberships = self._memberships_for_user(actor.user_id)
            if not memberships:
                raise_no_accessible_workspace()
            summaries = [
                WorkspaceSummary(
                    workspace_id=membership.workspace_id,
                    name=self._workspaces_by_id[membership.workspace_id].name,
                    role=membership.role,
                )
                for membership in memberships
            ]
            return tuple(
                sorted(
                    summaries,
                    key=lambda summary: (summary.name.casefold(), summary.workspace_id),
                )
            )

    def resolve_workspace_context(
        self,
        session: AuthenticatedSession,
        selection: WorkspaceSelection | None,
        required_permission: Permission,
    ) -> WorkspaceContext:
        if not isinstance(required_permission, Permission):
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Required permission is invalid",
                field="required_permission",
            )
        with self._lock:
            actor = self._require_active_session(session)
            memberships = self._memberships_for_user(actor.user_id)

            membership: Membership | None = None
            if selection is not None:
                membership = self._membership_for_pair(
                    selection.workspace_id,
                    actor.user_id,
                )
                if membership is None:
                    raise_workspace_not_found_or_forbidden()
            else:
                if not memberships:
                    raise_no_accessible_workspace()
                preferred_workspace_id = self._preferred_workspace_by_user.get(
                    actor.user_id
                )
                if preferred_workspace_id is not None:
                    membership = self._membership_for_pair(
                        preferred_workspace_id,
                        actor.user_id,
                    )
                if membership is None:
                    if len(memberships) != 1:
                        raise WorkspaceAccessError(
                            ErrorCode.WORKSPACE_SELECTION_REQUIRED,
                            message="Workspace selection is required",
                        )
                    membership = memberships[0]

            permissions = permissions_for_role(membership.role)
            if required_permission not in permissions:
                raise WorkspaceAccessError(
                    ErrorCode.PERMISSION_DENIED,
                    message="Permission is required",
                    field="required_permission",
                )
            workspace = self._workspaces_by_id.get(membership.workspace_id)
            if workspace is None:
                raise_workspace_not_found_or_forbidden()
            if selection is not None:
                self._preferred_workspace_by_user[actor.user_id] = workspace.workspace_id
            return self._context(
                state=actor,
                workspace=workspace,
                membership=membership,
                resolved_at=self._now(),
            )

    def grant_membership(
        self,
        context: WorkspaceContext,
        command: GrantMembership,
    ) -> Membership:
        self._validate_idempotency_key(command.idempotency_key)
        self._validate_role(command.role)
        with self._lock:
            _, workspace, _ = self._authorize_context(
                context,
                Permission.MEMBERSHIP_MANAGE,
            )
            payload = (command.user_id, command.role.value)
            found, replay = self._idempotency_replay(
                workspace.workspace_id,
                command.idempotency_key,
                "grant_membership",
                payload,
            )
            if found:
                if replay is None:
                    raise AssertionError("grant replay must contain a membership")
                return replay
            self._require_current_revision(context, workspace)

            user = self._users_by_id.get(command.user_id)
            if user is None or not user.enabled:
                raise_membership_not_found_or_forbidden()
            pair = (workspace.workspace_id, command.user_id)
            if pair in self._membership_id_by_pair:
                raise WorkspaceAccessError(
                    ErrorCode.MEMBERSHIP_ALREADY_EXISTS,
                    message="Membership already exists",
                )
            membership = Membership(
                membership_id=self._token_source.new_id("membership"),
                workspace_id=workspace.workspace_id,
                user_id=command.user_id,
                role=command.role,
                created_at=self._now(),
            )
            self._memberships_by_id[membership.membership_id] = membership
            self._membership_id_by_pair[pair] = membership.membership_id
            self._bump_authorization_revision(workspace)
            self._record_idempotency(
                workspace.workspace_id,
                command.idempotency_key,
                "grant_membership",
                payload,
                membership,
            )
            self._audit(
                AuditCategory.ADMINISTRATION,
                AuditAction.MEMBERSHIP_GRANTED,
                actor_user_id=context.user_id,
                workspace_id=workspace.workspace_id,
                target_id=membership.membership_id,
            )
            return membership

    def change_membership_role(
        self,
        context: WorkspaceContext,
        command: ChangeMembershipRole,
    ) -> Membership:
        self._validate_idempotency_key(command.idempotency_key)
        self._validate_role(command.role)
        with self._lock:
            _, workspace, _ = self._authorize_context(
                context,
                Permission.MEMBERSHIP_MANAGE,
            )
            payload = (command.membership_id, command.role.value)
            found, replay = self._idempotency_replay(
                workspace.workspace_id,
                command.idempotency_key,
                "change_membership_role",
                payload,
            )
            if found:
                if replay is None:
                    raise AssertionError("role replay must contain a membership")
                return replay
            target = self._membership_target(
                workspace.workspace_id,
                command.membership_id,
            )
            if (
                target.role is Role.OWNER
                and command.role is not Role.OWNER
                and self._owner_count(workspace.workspace_id) == 1
            ):
                raise_last_owner_required()
            self._require_current_revision(context, workspace)

            if target.role is command.role:
                updated = target
            else:
                updated = Membership(
                    membership_id=target.membership_id,
                    workspace_id=target.workspace_id,
                    user_id=target.user_id,
                    role=command.role,
                    created_at=target.created_at,
                )
                self._memberships_by_id[target.membership_id] = updated
                self._bump_authorization_revision(workspace)
            self._record_idempotency(
                workspace.workspace_id,
                command.idempotency_key,
                "change_membership_role",
                payload,
                updated,
            )
            if target.role is not command.role:
                self._audit(
                    AuditCategory.ADMINISTRATION,
                    AuditAction.MEMBERSHIP_ROLE_CHANGED,
                    actor_user_id=context.user_id,
                    workspace_id=workspace.workspace_id,
                    target_id=updated.membership_id,
                )
            return updated

    def revoke_membership(
        self,
        context: WorkspaceContext,
        command: RevokeMembership,
    ) -> None:
        self._validate_idempotency_key(command.idempotency_key)
        with self._lock:
            _, workspace, _ = self._authorize_context(
                context,
                Permission.MEMBERSHIP_MANAGE,
            )
            payload = (command.membership_id,)
            found, _ = self._idempotency_replay(
                workspace.workspace_id,
                command.idempotency_key,
                "revoke_membership",
                payload,
            )
            if found:
                return
            target = self._membership_target(
                workspace.workspace_id,
                command.membership_id,
            )
            if (
                target.role is Role.OWNER
                and self._owner_count(workspace.workspace_id) == 1
            ):
                raise_last_owner_required()
            self._require_current_revision(context, workspace)

            del self._memberships_by_id[target.membership_id]
            del self._membership_id_by_pair[(target.workspace_id, target.user_id)]
            if (
                self._preferred_workspace_by_user.get(target.user_id)
                == target.workspace_id
            ):
                del self._preferred_workspace_by_user[target.user_id]
            self._bump_authorization_revision(workspace)
            self._record_idempotency(
                workspace.workspace_id,
                command.idempotency_key,
                "revoke_membership",
                payload,
                None,
            )
            self._audit(
                AuditCategory.ADMINISTRATION,
                AuditAction.MEMBERSHIP_REVOKED,
                actor_user_id=context.user_id,
                workspace_id=workspace.workspace_id,
                target_id=target.membership_id,
            )

    def list_audit_events(
        self,
        context: WorkspaceContext,
    ) -> tuple[AuditEvent, ...]:
        with self._lock:
            _, workspace, _ = self._authorize_context(
                context,
                Permission.AUDIT_READ,
            )
            return self._audit_log.for_workspace(workspace.workspace_id)

    def export_my_access_data(
        self,
        session: AuthenticatedSession,
    ) -> AccountAccessExport:
        with self._lock:
            actor = self._require_active_session(session)
            user = self._users_by_id[actor.user_id]
            memberships = tuple(
                sorted(
                    (
                        AccountMembershipExport(
                            workspace_id=membership.workspace_id,
                            workspace_name=self._workspaces_by_id[
                                membership.workspace_id
                            ].name,
                            role=membership.role,
                        )
                        for membership in self._memberships_for_user(actor.user_id)
                    ),
                    key=lambda item: (item.workspace_name.casefold(), item.workspace_id),
                )
            )
            audit_events = self._audit_log.for_actor(actor.user_id)
            return AccountAccessExport(
                user_id=user.user_id,
                issuer=user.issuer or "",
                subject=user.subject or "",
                verified_email=user.verified_email,
                display_name=user.display_name,
                memberships=memberships,
                audit_events=audit_events,
            )

    def request_account_deletion(self, session: AuthenticatedSession) -> None:
        with self._lock:
            actor = self._require_active_session(session)
            user = self._users_by_id[actor.user_id]
            memberships = list(self._memberships_for_user(actor.user_id))
            if any(
                membership.role is Role.OWNER
                and self._owner_count(membership.workspace_id) == 1
                for membership in memberships
            ):
                raise_last_owner_required()

            now = self._now()
            removed_membership_ids = {membership.membership_id for membership in memberships}
            for membership in memberships:
                workspace = self._workspaces_by_id[membership.workspace_id]
                del self._memberships_by_id[membership.membership_id]
                del self._membership_id_by_pair[
                    (membership.workspace_id, membership.user_id)
                ]
                self._bump_authorization_revision(workspace)
                self._audit(
                    AuditCategory.ADMINISTRATION,
                    AuditAction.MEMBERSHIP_REVOKED,
                    actor_user_id=actor.user_id,
                    workspace_id=membership.workspace_id,
                    target_id=membership.membership_id,
                )

            self._preferred_workspace_by_user.pop(actor.user_id, None)
            for state in self._sessions_by_digest.values():
                if state.user_id == actor.user_id and state.revoked_at is None:
                    state.revoked_at = now
            self._audit(
                AuditCategory.SECURITY,
                AuditAction.SESSION_REVOKED,
                actor_user_id=actor.user_id,
                workspace_id=None,
                target_id=actor.user_id,
            )

            if user.issuer is not None and user.subject is not None:
                self._user_id_by_identity.pop((user.issuer, user.subject), None)
            user.issuer = None
            user.subject = None
            user.verified_email = None
            user.display_name = None
            user.enabled = False
            user.deleted_at = now
            self._idempotency_records = {
                key: record
                for key, record in self._idempotency_records.items()
                if actor.user_id not in record.payload
                and not any(value in removed_membership_ids for value in record.payload)
                and (record.result is None or record.result.user_id != actor.user_id)
            }
            self._audit(
                AuditCategory.ADMINISTRATION,
                AuditAction.ACCOUNT_DELETED,
                actor_user_id=actor.user_id,
                workspace_id=None,
                target_id=actor.user_id,
            )

    def purge_expired_data(self, reference_time: datetime) -> RetentionReport:
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="reference_time must be timezone-aware",
                field="reference_time",
            )
        reference_time = reference_time.astimezone(UTC)
        with self._lock:
            expired_digests = [
                digest
                for digest, state in self._sessions_by_digest.items()
                if state.absolute_expires_at + timedelta(days=30) <= reference_time
            ]
            for digest in expired_digests:
                state = self._sessions_by_digest.pop(digest)
                self._session_digest_by_id.pop(state.session_id, None)

            security_purged, administration_purged = self._audit_log.purge(
                reference_time
            )
            return RetentionReport(
                session_records_purged=len(expired_digests),
                security_audit_events_purged=security_purged,
                administration_audit_events_purged=administration_purged,
            )

    def logout(self, evidence: SessionEvidence) -> None:
        digest = self._digest(evidence.secret.reveal())
        with self._lock:
            state = self._sessions_by_digest.get(digest)
            if state is not None and state.revoked_at is None:
                state.revoked_at = self._now()
                self._audit(
                    AuditCategory.SECURITY,
                    AuditAction.SESSION_REVOKED,
                    actor_user_id=state.user_id,
                    workspace_id=None,
                    target_id=state.session_id,
                )

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
                self._audit(
                    AuditCategory.SECURITY,
                    AuditAction.SESSION_REVOKED,
                    actor_user_id=actor_state.user_id,
                    workspace_id=None,
                    target_id=target.session_id,
                )

    def revoke_all_user_sessions(self, session: AuthenticatedSession) -> None:
        with self._lock:
            actor_state = self._require_active_session(session)
            now = self._now()
            for state in self._sessions_by_digest.values():
                if state.user_id == actor_state.user_id and state.revoked_at is None:
                    state.revoked_at = now
            self._audit(
                AuditCategory.SECURITY,
                AuditAction.SESSION_REVOKED,
                actor_user_id=actor_state.user_id,
                workspace_id=None,
                target_id=actor_state.user_id,
            )

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
            self._audit(
                AuditCategory.SECURITY,
                AuditAction.USER_SUSPENDED,
                actor_user_id=None,
                workspace_id=None,
                target_id=user_id,
            )

    def _require_active_session(
        self,
        session: AuthenticatedSession,
    ) -> SessionState:
        return self._active_session_by_id(session.session_id, session.user_id)

    def _active_session_by_id(
        self,
        session_id: str,
        user_id: str,
    ) -> SessionState:
        digest = self._session_digest_by_id.get(session_id)
        state = self._sessions_by_digest.get(digest) if digest is not None else None
        now = self._now()
        if (
            state is None
            or state.user_id != user_id
            or state.revoked_at is not None
            or now >= state.idle_expires_at
            or now >= state.absolute_expires_at
        ):
            raise_unauthenticated()
        user = self._users_by_id.get(state.user_id)
        if user is None or not user.enabled:
            raise_unauthenticated()
        return state

    def _authorize_context(
        self,
        context: WorkspaceContext,
        permission: Permission,
    ) -> tuple[SessionState, Workspace, Membership]:
        state = self._active_session_by_id(context.session_id, context.user_id)
        workspace = self._workspaces_by_id.get(context.workspace_id)
        membership = self._memberships_by_id.get(context.membership_id)
        if (
            workspace is None
            or membership is None
            or membership.workspace_id != context.workspace_id
            or membership.user_id != context.user_id
            or self._membership_id_by_pair.get(
                (context.workspace_id, context.user_id)
            )
            != context.membership_id
        ):
            raise WorkspaceAccessError(
                ErrorCode.PERMISSION_DENIED,
                message="Permission is required",
            )
        if permission not in permissions_for_role(membership.role):
            raise WorkspaceAccessError(
                ErrorCode.PERMISSION_DENIED,
                message="Permission is required",
                field="required_permission",
            )
        return state, workspace, membership

    @staticmethod
    def _require_current_revision(
        context: WorkspaceContext,
        workspace: Workspace,
    ) -> None:
        if context.authorization_revision != workspace.authorization_revision:
            raise WorkspaceAccessError(
                ErrorCode.PERMISSION_DENIED,
                message="Authorization context is no longer current",
            )

    def _authenticated_session(self, state: SessionState) -> AuthenticatedSession:
        return AuthenticatedSession(
            session_id=state.session_id,
            user_id=state.user_id,
            authenticated_at=state.authenticated_at,
            idle_expires_at=state.idle_expires_at,
            absolute_expires_at=state.absolute_expires_at,
        )

    def _memberships_for_user(self, user_id: str) -> list[Membership]:
        return [
            membership
            for membership in self._memberships_by_id.values()
            if membership.user_id == user_id
        ]

    def _membership_for_pair(
        self,
        workspace_id: str,
        user_id: str,
    ) -> Membership | None:
        membership_id = self._membership_id_by_pair.get((workspace_id, user_id))
        return (
            self._memberships_by_id.get(membership_id)
            if membership_id is not None
            else None
        )

    def _membership_target(
        self,
        workspace_id: str,
        membership_id: str,
    ) -> Membership:
        membership = self._memberships_by_id.get(membership_id)
        if membership is None or membership.workspace_id != workspace_id:
            raise_membership_not_found_or_forbidden()
        return membership

    def _owner_count(self, workspace_id: str) -> int:
        return sum(
            membership.workspace_id == workspace_id
            and membership.role is Role.OWNER
            for membership in self._memberships_by_id.values()
        )

    def _bump_authorization_revision(self, workspace: Workspace) -> Workspace:
        updated = Workspace(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            created_at=workspace.created_at,
            authorization_revision=workspace.authorization_revision + 1,
        )
        self._workspaces_by_id[workspace.workspace_id] = updated
        return updated

    def _idempotency_replay(
        self,
        workspace_id: str,
        key: str,
        operation: str,
        payload: tuple[str, ...],
    ) -> tuple[bool, Membership | None]:
        record = self._idempotency_records.get((workspace_id, key))
        if record is None:
            return False, None
        if record.operation != operation or record.payload != payload:
            raise WorkspaceAccessError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                message="Idempotency key was reused with different input",
                field="idempotency_key",
            )
        return True, record.result

    def _record_idempotency(
        self,
        workspace_id: str,
        key: str,
        operation: str,
        payload: tuple[str, ...],
        result: Membership | None,
    ) -> None:
        self._idempotency_records[(workspace_id, key)] = IdempotencyRecord(
            operation=operation,
            payload=payload,
            result=result,
        )

    def _audit(
        self,
        category: AuditCategory,
        action: AuditAction,
        *,
        actor_user_id: str | None,
        workspace_id: str | None,
        target_id: str | None,
    ) -> None:
        event_id = self._token_source.new_id("audit")
        self._audit_log.append(
            AuditEvent(
                event_id=event_id,
                category=category,
                action=action,
                outcome=AuditOutcome.SUCCEEDED,
                actor_user_id=actor_user_id,
                workspace_id=workspace_id,
                target_id=target_id,
                occurred_at=self._now(),
                correlation_id=event_id,
            )
        )

    @staticmethod
    def _context(
        *,
        state: SessionState,
        workspace: Workspace,
        membership: Membership,
        resolved_at: datetime,
    ) -> WorkspaceContext:
        return WorkspaceContext(
            workspace_id=workspace.workspace_id,
            user_id=state.user_id,
            membership_id=membership.membership_id,
            role=membership.role,
            permissions=permissions_for_role(membership.role),
            session_id=state.session_id,
            authorization_revision=workspace.authorization_revision,
            resolved_at=resolved_at,
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
    def _validate_idempotency_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Idempotency key is invalid",
                field="idempotency_key",
            )

    @staticmethod
    def _validate_role(role: Role) -> None:
        if not isinstance(role, Role):
            raise WorkspaceAccessError(
                ErrorCode.INVALID_INPUT,
                message="Role is invalid",
                field="role",
            )

    def _debug_session_digests(self) -> frozenset[str]:
        """Test-only view proving raw session values are not retained."""

        with self._lock:
            return frozenset(self._sessions_by_digest)

    def _debug_set_workspace_preference(
        self,
        user_id: str,
        workspace_id: str,
    ) -> None:
        """Test-only hook for a preference that became stale outside a request."""

        with self._lock:
            self._preferred_workspace_by_user[user_id] = workspace_id

    def _debug_owner_count(self, workspace_id: str) -> int:
        with self._lock:
            return self._owner_count(workspace_id)

    def _debug_user_direct_identifiers(
        self,
        user_id: str,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        with self._lock:
            user = self._users_by_id[user_id]
            return (
                user.issuer,
                user.subject,
                user.verified_email,
                user.display_name,
            )

    def _debug_audit_counts(self) -> dict[AuditCategory, int]:
        with self._lock:
            return self._audit_log.counts()
