"""Pydantic schemas for claims and claim evidence."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClaimCategory(StrEnum):
    HOURS = "HOURS"
    FEES = "FEES"
    RESTRICTION = "RESTRICTION"
    CUISINE = "CUISINE"
    ATTRACTION = "ATTRACTION"
    WEATHER = "WEATHER"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    CONFLICTED = "CONFLICTED"
    UNVERIFIED = "UNVERIFIED"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class EvidenceSupportType(StrEnum):
    SUPPORTS = "SUPPORTS"
    REFUTES = "REFUTES"
    PARTIAL = "PARTIAL"


class ClaimBase(BaseModel):
    """Base schema for extracted and verified claims."""

    subject_id: str = Field(..., min_length=1, max_length=256)
    category: ClaimCategory | str = Field(..., min_length=1, max_length=64)
    typed_value: dict[str, Any] = Field(..., description="Structured claim payload")
    place_id: uuid.UUID | None = None
    execution_id: uuid.UUID | None = None
    verification: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)
    availability: AvailabilityStatus = Field(default=AvailabilityStatus.UNKNOWN)
    freshness: FreshnessStatus = Field(default=FreshnessStatus.UNKNOWN)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    geographic_scope: dict[str, Any] | None = None
    policy_version: str = Field(default="1.0", min_length=1, max_length=32)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    decision_history: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_validity_interval(self) -> ClaimBase:
        if self.valid_from is not None and self.valid_until is not None:
            if self.valid_until < self.valid_from:
                raise ValueError("valid_until must not be earlier than valid_from")
        return self


class ClaimCreate(ClaimBase):
    """Payload for inserting a new claim."""

    id: uuid.UUID | None = None


class ClaimUpdate(BaseModel):
    """Payload for updating an existing claim.

    Note: Partial updates supplying only one temporal bound (valid_from or valid_until)
    cannot detect inverted intervals against persisted values at schema validation time alone.
    Service and repository layers must call validate_with_existing(existing_from, existing_until)
    when applying partial updates to prevent interval inversion.
    """

    typed_value: dict[str, Any] | None = None
    verification: VerificationStatus | None = None
    availability: AvailabilityStatus | None = None
    freshness: FreshnessStatus | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    geographic_scope: dict[str, Any] | None = None
    policy_version: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    contradictions: list[dict[str, Any]] | None = None
    decision_history: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_validity_interval(self) -> ClaimUpdate:
        if self.valid_from is not None and self.valid_until is not None:
            if self.valid_until < self.valid_from:
                raise ValueError("valid_until must not be earlier than valid_from")
        return self

    def validate_with_existing(
        self,
        existing_valid_from: datetime | None,
        existing_valid_until: datetime | None,
    ) -> None:
        """Validate merged validity interval against existing persisted bounds."""
        effective_from = self.valid_from if self.valid_from is not None else existing_valid_from
        effective_until = self.valid_until if self.valid_until is not None else existing_valid_until
        if effective_from is not None and effective_until is not None:
            if effective_until < effective_from:
                raise ValueError("Updated valid_until cannot be earlier than valid_from")


class ClaimRead(ClaimBase):
    """Read representation of a persisted claim."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ClaimEvidenceCreate(BaseModel):
    """Payload for associating a claim with a source observation."""

    claim_id: uuid.UUID
    observation_id: uuid.UUID
    support_type: EvidenceSupportType = Field(default=EvidenceSupportType.SUPPORTS)
    locator: str = Field(default="", max_length=256)


class ClaimEvidenceRead(BaseModel):
    """Read representation of an evidence association."""

    model_config = ConfigDict(from_attributes=True)

    claim_id: uuid.UUID
    observation_id: uuid.UUID
    support_type: EvidenceSupportType
    locator: str
    created_at: datetime
