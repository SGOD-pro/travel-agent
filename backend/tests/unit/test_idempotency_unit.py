"""Unit tests for idempotency hashing and exception behavior."""

from __future__ import annotations

from functions.api.idempotency import (
    IdempotencyConflictError,
    compute_request_hash,
)


def test_compute_request_hash_key_order_invariance() -> None:
    """Verifies that dict key ordering does not change the canonical SHA256 hash."""
    payload_1 = {"destination": "Goa", "origin": "Bengaluru", "adults": 2}
    payload_2 = {"adults": 2, "origin": "Bengaluru", "destination": "Goa"}

    hash_1 = compute_request_hash(payload_1)
    hash_2 = compute_request_hash(payload_2)

    assert hash_1 == hash_2
    assert len(hash_1) == 64


def test_compute_request_hash_detects_value_differences() -> None:
    """Verifies that differing values produce distinct SHA256 hashes."""
    payload_1 = {"destination": "Goa"}
    payload_2 = {"destination": "Manali"}

    hash_1 = compute_request_hash(payload_1)
    hash_2 = compute_request_hash(payload_2)

    assert hash_1 != hash_2


def test_compute_request_hash_nested_order_invariance() -> None:
    """Verifies that nested dictionaries also sort keys deterministically."""
    payload_1 = {"options": {"pace": "fast", "budget": 5000}}
    payload_2 = {"options": {"budget": 5000, "pace": "fast"}}

    assert compute_request_hash(payload_1) == compute_request_hash(payload_2)


def test_idempotency_conflict_error_message() -> None:
    """Verifies IdempotencyConflictError formatting."""
    err = IdempotencyConflictError("Key already used with conflicting payload")
    assert str(err) == "Key already used with conflicting payload"
