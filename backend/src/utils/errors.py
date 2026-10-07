"""Domain and application error types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RetryClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    REPAIRABLE = "REPAIRABLE"
    USER_INPUT = "USER_INPUT"
    TERMINAL = "TERMINAL"


class TravelPlatformError(Exception):
    """Base exception for all travel platform errors."""

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        retry_class: RetryClass = RetryClass.TERMINAL,
        provider: str | None = None,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retry_class = retry_class
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}
