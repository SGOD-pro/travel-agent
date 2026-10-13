"""Unit tests for claims and claim evidence schemas conforming to spec 0002.

Covers: AC-8 (Extracted claims persistence, validation invariants, typed values, and evidence links).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimCategory,
    ClaimCreate,
    ClaimEvidenceCreate,
    ClaimEvidenceRead,
    ClaimRead,
    ClaimUpdate,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)

# ============================================================================
# Claim Schema Unit Tests (AC-8)
# ============================================================================


def test_claim_create_with_valid_parameters() -> None:
    """Covers: AC-8 - ClaimCreate constructs successfully with typed payload and valid intervals."""
    # Arrange
    now = datetime.now(UTC)
    payload = {
        "subject_id": "dakshineswar-temple",
        "category": ClaimCategory.HOURS,
        "typed_value": {"open": "06:00", "close": "20:30", "breaks": [{"from": "12:30", "to": "15:00"}]},
        "verification": VerificationStatus.VERIFIED,
        "availability": AvailabilityStatus.AVAILABLE,
        "freshness": FreshnessStatus.FRESH,
        "valid_from": now,
        "valid_until": now + timedelta(days=30),
        "confidence": 0.95,
        "geographic_scope": {"locality": "Kolkata", "state": "WB"},
    }

    # Act
    claim = ClaimCreate(**payload)

    # Assert
    assert claim.subject_id == "dakshineswar-temple"
    assert claim.category == ClaimCategory.HOURS
    assert claim.verification == VerificationStatus.VERIFIED
    assert claim.confidence == 0.95
    assert claim.typed_value["open"] == "06:00"


def test_claim_read_validates_persisted_shape() -> None:
    """Covers: AC-8 - ClaimRead model validates persisted claim dictionary."""
    # Arrange
    now = datetime.now(UTC)
    claim_id = uuid.uuid4()
    payload = {
        "id": claim_id,
        "subject_id": "dakshineswar-temple",
        "category": ClaimCategory.HOURS,
        "typed_value": {"open": "06:00"},
        "verification": VerificationStatus.VERIFIED,
        "created_at": now,
        "updated_at": now,
    }

    # Act
    read_claim = ClaimRead.model_validate(payload)

    # Assert
    assert read_claim.id == claim_id
    assert read_claim.subject_id == "dakshineswar-temple"
    assert read_claim.category == ClaimCategory.HOURS
    assert read_claim.verification == VerificationStatus.VERIFIED


def test_claim_create_validity_interval_boundary_identical_timestamps() -> None:
    """Covers: AC-8 - ClaimCreate permits valid_from and valid_until to be identical."""
    # Arrange
    now = datetime.now(UTC)

    # Act
    claim = ClaimCreate(
        subject_id="point-in-time-fact",
        category=ClaimCategory.FEES,
        typed_value={"fee": 0},
        valid_from=now,
        valid_until=now,
    )

    # Assert
    assert claim.valid_from == now
    assert claim.valid_until == now


def test_claim_create_rejects_inverted_validity_interval() -> None:
    """Covers: AC-8 - ClaimCreate raises ValidationError when valid_until is earlier than valid_from."""
    # Arrange
    now = datetime.now(UTC)

    # Act & Assert
    with pytest.raises(ValidationError, match="valid_until must not be earlier than valid_from"):
        ClaimCreate(
            subject_id="invalid-interval-subject",
            category=ClaimCategory.RESTRICTION,
            typed_value={"open": False},
            valid_from=now,
            valid_until=now - timedelta(seconds=1),
        )


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0, None])
def test_claim_create_confidence_valid_range(confidence: float | None) -> None:
    """Covers: AC-8 - ClaimCreate accepts confidence in 0.0 to 1.0 or None."""
    # Arrange & Act
    claim = ClaimCreate(
        subject_id="confidence-subject",
        category=ClaimCategory.ATTRACTION,
        typed_value={"type": "heritage"},
        confidence=confidence,
    )

    # Assert
    assert claim.confidence == confidence


@pytest.mark.parametrize("invalid_confidence", [-0.01, 1.01, 2.5, -100.0])
def test_claim_create_rejects_out_of_range_confidence(invalid_confidence: float) -> None:
    """Covers: AC-8 - ClaimCreate raises ValidationError when confidence is outside [0.0, 1.0]."""
    # Arrange, Act & Assert
    with pytest.raises(ValidationError):
        ClaimCreate(
            subject_id="bad-confidence",
            category=ClaimCategory.WEATHER,
            typed_value={"temp_c": 30},
            confidence=invalid_confidence,
        )


def test_claim_create_rejects_empty_subject_id() -> None:
    """Covers: AC-8 - ClaimCreate rejects empty subject_id."""
    # Arrange, Act & Assert
    with pytest.raises(ValidationError):
        ClaimCreate(
            subject_id="",
            category=ClaimCategory.CUISINE,
            typed_value={"specialty": "bengali-sweets"},
        )


# ============================================================================
# ClaimUpdate Partial Update Invariant Tests (AC-8)
# ============================================================================


def test_claim_update_with_both_bounds_enforces_interval() -> None:
    """Covers: AC-8 - ClaimUpdate validator catches inverted interval when both bounds are provided."""
    # Arrange
    now = datetime.now(UTC)

    # Valid update
    valid_upd = ClaimUpdate(valid_from=now, valid_until=now + timedelta(days=1))
    assert valid_upd.valid_until is not None

    # Inverted update
    with pytest.raises(ValidationError, match="valid_until must not be earlier than valid_from"):
        ClaimUpdate(valid_from=now, valid_until=now - timedelta(days=1))


def test_claim_update_validate_with_existing_catches_partial_inversions() -> None:
    """Covers: AC-8 - validate_with_existing prevents interval inversion on partial updates."""
    # Arrange
    now = datetime.now(UTC)
    persisted_from = now
    persisted_until = now + timedelta(days=10)

    # Case 1: Partial update sends only valid_until earlier than persisted valid_from
    inverted_until = ClaimUpdate(valid_until=now - timedelta(days=1))
    with pytest.raises(ValueError, match="Updated valid_until cannot be earlier than valid_from"):
        inverted_until.validate_with_existing(
            existing_valid_from=persisted_from,
            existing_valid_until=persisted_until,
        )

    # Case 2: Partial update sends only valid_from later than persisted valid_until
    inverted_from = ClaimUpdate(valid_from=now + timedelta(days=15))
    with pytest.raises(ValueError, match="Updated valid_until cannot be earlier than valid_from"):
        inverted_from.validate_with_existing(
            existing_valid_from=persisted_from,
            existing_valid_until=persisted_until,
        )

    # Case 3: Valid partial updates pass without error
    valid_new_until = ClaimUpdate(valid_until=now + timedelta(days=20))
    valid_new_until.validate_with_existing(
        existing_valid_from=persisted_from,
        existing_valid_until=persisted_until,
    )

    valid_new_from = ClaimUpdate(valid_from=now + timedelta(days=2))
    valid_new_from.validate_with_existing(
        existing_valid_from=persisted_from,
        existing_valid_until=persisted_until,
    )


# ============================================================================
# ClaimEvidence Schema Unit Tests (AC-8)
# ============================================================================


def test_claim_evidence_create_and_read() -> None:
    """Covers: AC-8 - ClaimEvidenceCreate and Read handle junction data correctly."""
    # Arrange
    claim_id = uuid.uuid4()
    obs_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Act
    create_payload = ClaimEvidenceCreate(
        claim_id=claim_id,
        observation_id=obs_id,
        support_type=EvidenceSupportType.SUPPORTS,
        locator="header > h1",
    )

    read_payload = ClaimEvidenceRead(
        claim_id=claim_id,
        observation_id=obs_id,
        support_type=EvidenceSupportType.SUPPORTS,
        locator="header > h1",
        created_at=now,
    )

    # Assert
    assert create_payload.claim_id == claim_id
    assert create_payload.observation_id == obs_id
    assert create_payload.support_type == EvidenceSupportType.SUPPORTS
    assert read_payload.created_at == now
