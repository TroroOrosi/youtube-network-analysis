"""Immutable public contracts for workspace identity and authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Role(str, Enum):
    OWNER = "OWNER"
    MEMBER = "MEMBER"


class Permission(str, Enum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_UPDATE = "workspace.update"
    WORKSPACE_DELETE = "workspace.delete"
    MEMBERSHIP_LIST = "membership.list"
    MEMBERSHIP_MANAGE = "membership.manage"
    CHANNEL_READ = "channel.read"
    CHANNEL_MANAGE_CONNECTION = "channel.manage_connection"
    COLLECTION_READ = "collection.read"
    COLLECTION_RUN = "collection.run"
    ANALYSIS_READ = "analysis.read"
    ANALYSIS_EXPORT = "analysis.export"
    AUDIT_READ = "audit.read"


_MEMBER_PERMISSIONS = frozenset(
    {
        Permission.WORKSPACE_READ,
        Permission.MEMBERSHIP_LIST,
        Permission.CHANNEL_READ,
        Permission.COLLECTION_READ,
        Permission.COLLECTION_RUN,
        Permission.ANALYSIS_READ,
        Permission.ANALYSIS_EXPORT,
    }
)
_OWNER_PERMISSIONS = _MEMBER_PERMISSIONS | frozenset(
    {
        Permission.WORKSPACE_UPDATE,
        Permission.WORKSPACE_DELETE,
        Permission.MEMBERSHIP_MANAGE,
        Permission.CHANNEL_MANAGE_CONNECTION,
        Permission.AUDIT_READ,
    }
)
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.OWNER: _OWNER_PERMISSIONS,
        Role.MEMBER: _MEMBER_PERMISSIONS,
    }
)


def permissions_for_role(role: Role) -> frozenset[Permission]:
    """Return the closed permission set for an MVP role."""

    return ROLE_PERMISSIONS[role]


class ErrorCode(str, Enum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    SESSION_EXPIRED_OR_REVOKED = "SESSION_EXPIRED_OR_REVOKED"
    CSRF_VALIDATION_FAILED = "CSRF_VALIDATION_FAILED"
    WORKSPACE_SELECTION_REQUIRED = "WORKSPACE_SELECTION_REQUIRED"
    NO_ACCESSIBLE_WORKSPACE = "NO_ACCESSIBLE_WORKSPACE"
    WORKSPACE_NOT_FOUND_OR_FORBIDDEN = "WORKSPACE_NOT_FOUND_OR_FORBIDDEN"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    MEMBERSHIP_NOT_FOUND_OR_FORBIDDEN = "MEMBERSHIP_NOT_FOUND_OR_FORBIDDEN"
    MEMBERSHIP_ALREADY_EXISTS = "MEMBERSHIP_ALREADY_EXISTS"
    LAST_OWNER_REQUIRED = "LAST_OWNER_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_INPUT = "INVALID_INPUT"


class WorkspaceAccessError(ValueError):
    """Safe, stable workspace-access failure."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str,
        field: str | None = None,
        retryable: bool = False,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value
        self.message = message
        self.field = field
        self.retryable = retryable
        self.correlation_id = correlation_id


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WorkspaceAccessError(
            ErrorCode.INVALID_INPUT,
            message=f"{field} must be timezone-aware",
            field=field,
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class AccessSecret:
    """Sensitive value whose normal rendering is always redacted."""

    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "AccessSecret(<REDACTED>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    authenticated_at: datetime
    verified_email: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authenticated_at",
            _utc(self.authenticated_at, "authenticated_at"),
        )


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    secret: AccessSecret


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_id: str
    secret: AccessSecret
    idle_expires_at: datetime
    absolute_expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "idle_expires_at",
            _utc(self.idle_expires_at, "idle_expires_at"),
        )
        object.__setattr__(
            self,
            "absolute_expires_at",
            _utc(self.absolute_expires_at, "absolute_expires_at"),
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session_id: str
    user_id: str
    authenticated_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "authenticated_at",
            "idle_expires_at",
            "absolute_expires_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _utc(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    name: str
    created_at: datetime
    authorization_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace_id: str
    name: str
    role: Role


@dataclass(frozen=True, slots=True)
class Membership:
    membership_id: str
    workspace_id: str
    user_id: str
    role: Role
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class WorkspaceSelection:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    workspace_id: str
    user_id: str
    membership_id: str
    role: Role
    permissions: frozenset[Permission]
    session_id: str
    authorization_revision: int
    resolved_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved_at", _utc(self.resolved_at, "resolved_at"))


@dataclass(frozen=True, slots=True)
class CreateWorkspace:
    name: str


@dataclass(frozen=True, slots=True)
class GrantMembership:
    user_id: str
    role: Role
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ChangeMembershipRole:
    membership_id: str
    role: Role
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RevokeMembership:
    membership_id: str
    idempotency_key: str
