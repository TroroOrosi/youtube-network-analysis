"""Small deterministic boundaries used by workspace-access."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class TokenSource(Protocol):
    def new_id(self, prefix: str) -> str: ...

    def new_session_secret(self) -> str: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemTokenSource:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def new_session_secret(self) -> str:
        return secrets.token_urlsafe(32)


class StateStore(Protocol):
    """Where this module's state document rests between two processes.

    The store never learns what is inside: it keeps one text and hands it back
    unchanged. A file, a row, or nothing at all is a deployment decision, not a
    rule of the module.
    """

    def load(self) -> str | None: ...

    def save(self, document: str) -> None: ...
