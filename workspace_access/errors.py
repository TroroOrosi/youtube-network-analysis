"""Stable non-enumerating workspace-access error constructors."""

from .models import ErrorCode, WorkspaceAccessError


def raise_unauthenticated() -> None:
    raise WorkspaceAccessError(
        ErrorCode.UNAUTHENTICATED,
        message="Authentication is required",
    )


def raise_expired_or_revoked() -> None:
    raise WorkspaceAccessError(
        ErrorCode.SESSION_EXPIRED_OR_REVOKED,
        message="Session is expired or revoked",
    )


def raise_no_accessible_workspace() -> None:
    raise WorkspaceAccessError(
        ErrorCode.NO_ACCESSIBLE_WORKSPACE,
        message="No accessible workspace is available",
    )


def raise_workspace_not_found_or_forbidden() -> None:
    raise WorkspaceAccessError(
        ErrorCode.WORKSPACE_NOT_FOUND_OR_FORBIDDEN,
        message="Workspace was not found or is not accessible",
    )


def raise_membership_not_found_or_forbidden() -> None:
    raise WorkspaceAccessError(
        ErrorCode.MEMBERSHIP_NOT_FOUND_OR_FORBIDDEN,
        message="Membership was not found or is not accessible",
    )


def raise_last_owner_required() -> None:
    raise WorkspaceAccessError(
        ErrorCode.LAST_OWNER_REQUIRED,
        message="Workspace must retain at least one owner",
    )
