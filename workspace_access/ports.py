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
