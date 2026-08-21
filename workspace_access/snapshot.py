"""The state document this module writes and reads back.

The format belongs here, not to the store: a deployment can keep the text
wherever it likes, and nothing outside this module needs to know the shape of a
session or a membership. `VERSION` refuses a document written by code that knew
more than this one does, because silently ignoring fields loses access rules.

A session is written by digest only. The secret itself was never held in memory
either, so a stolen document cannot be replayed as a login.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .memory import IdempotencyRecord, SessionState, UserState
from .models import (
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    Membership,
    Role,
    Workspace,
)

VERSION = 1


@dataclass(frozen=True, slots=True)
class Snapshot:
    users: dict[str, UserState]
    sessions: dict[str, SessionState]
    workspaces: dict[str, Workspace]
    memberships: dict[str, Membership]
    preferred_workspaces: dict[str, str]
    idempotency: dict[tuple[str, str], IdempotencyRecord]
    audit_events: tuple[AuditEvent, ...]


def dump(snapshot: Snapshot) -> str:
    return json.dumps(
        {
            "version": VERSION,
            "users": [_user(user) for user in snapshot.users.values()],
            "sessions": [
                _session(digest, state) for digest, state in snapshot.sessions.items()
            ],
            "workspaces": [
                _workspace(workspace) for workspace in snapshot.workspaces.values()
            ],
            "memberships": [
                _membership(membership)
                for membership in snapshot.memberships.values()
            ],
            "preferred_workspaces": snapshot.preferred_workspaces,
            "idempotency": [
                _idempotency(workspace_id, key, record)
                for (workspace_id, key), record in snapshot.idempotency.items()
            ],
            "audit_events": [_event(event) for event in snapshot.audit_events],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def load(document: str) -> Snapshot:
    try:
        payload = json.loads(document)
    except ValueError as error:
        raise ValueError("state document is not readable JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("state document is not an object")
    version = payload.get("version")
    if version != VERSION:
        raise ValueError(f"state document version {version!r} is not supported")
    try:
        users = {item["user_id"]: _read_user(item) for item in payload["users"]}
        sessions = {
            item["secret_digest"]: _read_session(item) for item in payload["sessions"]
        }
        workspaces = {
            item["workspace_id"]: _read_workspace(item)
            for item in payload["workspaces"]
        }
        memberships = {
            item["membership_id"]: _read_membership(item)
            for item in payload["memberships"]
        }
        preferred = dict(payload["preferred_workspaces"])
        idempotency = {
            (item["workspace_id"], item["key"]): _read_idempotency(item)
            for item in payload["idempotency"]
        }
        events = tuple(_read_event(item) for item in payload["audit_events"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("state document is missing required fields") from error
    return Snapshot(
        users=users,
        sessions=sessions,
        workspaces=workspaces,
        memberships=memberships,
        preferred_workspaces=preferred,
        idempotency=idempotency,
        audit_events=events,
    )


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _read_moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _user(user: UserState) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "issuer": user.issuer,
        "subject": user.subject,
        "verified_email": user.verified_email,
        "display_name": user.display_name,
        "enabled": user.enabled,
        "deleted_at": _moment(user.deleted_at),
    }


def _read_user(item: dict[str, object]) -> UserState:
    return UserState(
        user_id=item["user_id"],
        issuer=item["issuer"],
        subject=item["subject"],
        verified_email=item["verified_email"],
        display_name=item["display_name"],
        enabled=item["enabled"],
        deleted_at=_read_moment(item["deleted_at"]),
    )


def _session(digest: str, state: SessionState) -> dict[str, object]:
    return {
        "session_id": state.session_id,
        "user_id": state.user_id,
        "secret_digest": digest,
        "authenticated_at": _moment(state.authenticated_at),
        "last_used_at": _moment(state.last_used_at),
        "idle_expires_at": _moment(state.idle_expires_at),
        "absolute_expires_at": _moment(state.absolute_expires_at),
        "revoked_at": _moment(state.revoked_at),
    }


def _read_session(item: dict[str, object]) -> SessionState:
    return SessionState(
        session_id=item["session_id"],
        user_id=item["user_id"],
        secret_digest=item["secret_digest"],
        authenticated_at=_read_moment(item["authenticated_at"]),
        last_used_at=_read_moment(item["last_used_at"]),
        idle_expires_at=_read_moment(item["idle_expires_at"]),
        absolute_expires_at=_read_moment(item["absolute_expires_at"]),
        revoked_at=_read_moment(item["revoked_at"]),
    )


def _workspace(workspace: Workspace) -> dict[str, object]:
    return {
        "workspace_id": workspace.workspace_id,
        "name": workspace.name,
        "created_at": _moment(workspace.created_at),
        "authorization_revision": workspace.authorization_revision,
    }


def _read_workspace(item: dict[str, object]) -> Workspace:
    return Workspace(
        workspace_id=item["workspace_id"],
        name=item["name"],
        created_at=_read_moment(item["created_at"]),
        authorization_revision=item["authorization_revision"],
    )


def _membership(membership: Membership) -> dict[str, object]:
    return {
        "membership_id": membership.membership_id,
        "workspace_id": membership.workspace_id,
        "user_id": membership.user_id,
        "role": membership.role.value,
        "created_at": _moment(membership.created_at),
    }


def _read_membership(item: dict[str, object]) -> Membership:
    return Membership(
        membership_id=item["membership_id"],
        workspace_id=item["workspace_id"],
        user_id=item["user_id"],
        role=Role(item["role"]),
        created_at=_read_moment(item["created_at"]),
    )


def _idempotency(
    workspace_id: str, key: str, record: IdempotencyRecord
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "key": key,
        "actor_user_id": record.actor_user_id,
        "operation": record.operation,
        "payload": list(record.payload),
        "result_kind": _result_kind(record.result),
        "result": _result(record.result),
    }


def _result_kind(result: Membership | Workspace | None) -> str | None:
    if isinstance(result, Membership):
        return "membership"
    if isinstance(result, Workspace):
        return "workspace"
    return None


def _result(result: Membership | Workspace | None) -> dict[str, object] | None:
    if isinstance(result, Membership):
        return _membership(result)
    if isinstance(result, Workspace):
        return _workspace(result)
    return None


def _read_idempotency(item: dict[str, object]) -> IdempotencyRecord:
    kind = item["result_kind"]
    payload = item["result"]
    if kind == "membership":
        result: Membership | Workspace | None = _read_membership(payload)
    elif kind == "workspace":
        result = _read_workspace(payload)
    elif kind is None:
        result = None
    else:
        raise ValueError(f"unknown idempotency result kind {kind!r}")
    return IdempotencyRecord(
        actor_user_id=item["actor_user_id"],
        operation=item["operation"],
        payload=tuple(item["payload"]),
        result=result,
    )


def _event(event: AuditEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "category": event.category.value,
        "action": event.action.value,
        "outcome": event.outcome.value,
        "actor_user_id": event.actor_user_id,
        "workspace_id": event.workspace_id,
        "target_id": event.target_id,
        "occurred_at": _moment(event.occurred_at),
        "correlation_id": event.correlation_id,
    }


def _read_event(item: dict[str, object]) -> AuditEvent:
    return AuditEvent(
        event_id=item["event_id"],
        category=AuditCategory(item["category"]),
        action=AuditAction(item["action"]),
        outcome=AuditOutcome(item["outcome"]),
        actor_user_id=item["actor_user_id"],
        workspace_id=item["workspace_id"],
        target_id=item["target_id"],
        occurred_at=_read_moment(item["occurred_at"]),
        correlation_id=item["correlation_id"],
    )
