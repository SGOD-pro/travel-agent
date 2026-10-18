"""Telemetry and payload sanitization utilities for remote LAYA decision routing.

Conforms to spec 0004 Slice 2:
- Strips Bearer tokens, Cloudflare Access headers, and sensitive credentials from logs and telemetry.
- Sanitizes contextual payloads by removing traveler personal details (PII) before transmission.
- Bounds client side request payload lengths defensively.
"""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_HEADER_KEYS = {
    "authorization",
    "cf-access-client-id",
    "cf-access-client-secret",
    "cookie",
    "x-api-key",
}

DISALLOWED_CONTEXT_FIELDS = {
    "traveler_name",
    "traveler_names",
    "travelers",
    "passengers",
    "email",
    "phone",
    "user_id",
    "token",
    "secret",
    "api_key",
    "password",
    "credentials",
}

ALLOWED_CONTEXT_FIELDS = {
    "execution_id",
    "phase",
    "category",
    "destination",
    "sanitized_summary",
    "static_search_completed",
    "unhandled_browser_requests",
    "candidate_places_count",
    "browser_escalation_slots_remaining",
}

MAX_SUMMARY_LENGTH = 4096


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Returns a copy of headers with sensitive credential values redacted."""
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADER_KEYS:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


def sanitize_context(
    context: dict[str, Any],
    max_summary_length: int = MAX_SUMMARY_LENGTH,
) -> dict[str, Any]:
    """Sanitizes context payload by stripping PII and bounding summary length.

    Preserves allowed domain fields while filtering disallowed or unexpected keys.
    """
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        lowered_key = key.lower()
        if lowered_key in DISALLOWED_CONTEXT_FIELDS:
            continue
        if key in ALLOWED_CONTEXT_FIELDS:
            if key == "sanitized_summary" and isinstance(value, str):
                if len(value) > max_summary_length:
                    sanitized[key] = value[:max_summary_length] + "... [truncated]"
                else:
                    sanitized[key] = value
            else:
                sanitized[key] = value
    return sanitized


def sanitize_text(text: str, secrets: list[str | None] | None = None) -> str:
    """Replaces occurrences of known secrets in log text with [REDACTED]."""
    if not text or not secrets:
        return text

    sanitized = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "[REDACTED]")

    # Also catch Bearer tokens via regex pattern
    sanitized = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", r"\1[REDACTED]", sanitized)
    return sanitized
