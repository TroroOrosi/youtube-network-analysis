"""Stable, non-enumerating analysis-api failures."""

from __future__ import annotations

import uuid
from enum import Enum


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    VIEW_NOT_FOUND_OR_FORBIDDEN = "VIEW_NOT_FOUND_OR_FORBIDDEN"
    DATASET_NOT_READY = "DATASET_NOT_READY"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_CURSOR = "INVALID_CURSOR"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"


class AnalysisApiError(ValueError):
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
