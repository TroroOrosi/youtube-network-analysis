"""Stable, non-enumerating channel-connections failures."""

from __future__ import annotations

import uuid
from enum import Enum


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    CONNECTION_NOT_FOUND_OR_FORBIDDEN = "CONNECTION_NOT_FOUND_OR_FORBIDDEN"
    CONNECTION_ALREADY_EXISTS = "CONNECTION_ALREADY_EXISTS"
    INTENT_NOT_FOUND_OR_EXPIRED = "INTENT_NOT_FOUND_OR_EXPIRED"
    CALLBACK_CONFLICT = "CALLBACK_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"
    PROVIDER_AUTHORIZATION_FAILED = "PROVIDER_AUTHORIZATION_FAILED"
    PROVIDER_CAPABILITY_MISSING = "PROVIDER_CAPABILITY_MISSING"
    REAUTH_CHANNEL_MISMATCH = "REAUTH_CHANNEL_MISMATCH"
    AUTHORITY_NOT_FOUND_OR_EXPIRED = "AUTHORITY_NOT_FOUND_OR_EXPIRED"
    CONNECTION_REAUTH_REQUIRED = "CONNECTION_REAUTH_REQUIRED"
    INVALID_CURSOR = "INVALID_CURSOR"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"


class ChannelConnectionsError(ValueError):
    """A safe public failure with stable machine-readable fields."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str,
        field: str | None = None,
        retryable: bool = False,
        correlation_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value
        self.message = message
        self.field = field
        self.retryable = retryable
        self.correlation_id = correlation_id or f"error_{uuid.uuid4().hex}"
        self.reason_code = reason_code
