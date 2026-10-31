"""Pydantic domain contracts for place candidate registration and evidence eligibility.

Defines schemas for:
- Spatial corridor validation outcomes
- Candidate existence and operating hours verification statuses
- Registered candidate place metadata
- Candidate evidence eligibility reports
"""

from __future__ import annotations

import uuid
from datetime import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts.llm import PlaceProposal
from functions.orchestrator.places.schemas import CoordinatePoint


class CorridorValidationStatus(StrEnum):
    """Spatial corridor validation status for candidate places."""

    WITHIN_CORRIDOR = "WITHIN_CORRIDOR"
    OUTSIDE_CORRIDOR = "OUTSIDE_CORRIDOR"
    MALFORMED_COORDINATES = "MALFORMED_COORDINATES"


class CandidateExistenceStatus(StrEnum):
    """Existence verification status of a candidate place."""

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"


class CandidateHoursStatus(StrEnum):
    """Operating hours verification status of a candidate place."""

    VERIFIED_HOURS = "VERIFIED_HOURS"
    HOURS_ASSUMED = "HOURS_ASSUMED"
    UNKNOWN = "UNKNOWN"


class CandidateCorridorResult(BaseModel):
    """Result of spatial corridor validation for a single proposal."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    proposal: PlaceProposal
    status: CorridorValidationStatus
    distance_to_corridor_km: float | None = None
    diagnostic: str | None = None


class RegisteredCandidate(BaseModel):
    """Candidate place registered or resolved in canonical places."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    place_id: uuid.UUID | None = None
    primary_name: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    point: CoordinatePoint
    uncertainty: str = Field(default="EDITORIAL_DISCOVERY")
    is_candidate: bool = Field(default=True)
    corridor_distance_km: float | None = None
    existence_status: CandidateExistenceStatus = Field(default=CandidateExistenceStatus.UNVERIFIED)
    hours_status: CandidateHoursStatus = Field(default=CandidateHoursStatus.UNKNOWN)
    window_start: time | None = None
    window_end: time | None = None
    is_hours_assumed: bool = Field(default=False)
    suggested_stay_minutes: int = Field(default=60, ge=5, le=1440)
    price_estimate_inr: float | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class CandidateEligibilityReport(BaseModel):
    """Summary of candidate places partitioned by deterministic evidence eligibility."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    eligible_stops: list[RegisteredCandidate] = Field(
        default_factory=list,
        description="Candidate places with verified existence eligible for route solver",
    )
    editorial_recommendations: list[RegisteredCandidate] = Field(
        default_factory=list,
        description="Candidate places within corridor but unverified existence kept as recommendations",
    )
    excluded_candidates: list[RegisteredCandidate] = Field(
        default_factory=list,
        description="Candidate places excluded due to contradiction or corridor distance breach",
    )
    summary: dict[str, int] = Field(
        default_factory=dict,
        description="Aggregate counts across candidate classifications",
    )
