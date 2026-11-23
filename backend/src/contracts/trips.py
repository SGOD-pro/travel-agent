"""Domain models for Trips, TripBrief, Versioning, and Concurrency.

Enforces:
- Version tracking with optimistic concurrency checks.
- TripBrief validation (date ranges, positive passenger counts, derived total_days).
- Deduplication of commands by command_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.money import Money


class TripState(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    PROPOSED = "proposed"
    APPROVED = "approved"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TransportMode(StrEnum):
    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"
    CAR_PETROL = "car_petrol"
    MOTORCYCLE_PETROL = "motorcycle_petrol"
    WALKING = "walking"
    BICYCLE = "bicycle"


class DestinationPoint(BaseModel):
    """Destination stop requested in brief."""

    model_config = ConfigDict(frozen=True)

    name: str
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provider_place_id: str | None = None
    stay_days: int = Field(default=1, ge=1)
    visit_date: date | None = None
    day_offset: int | None = Field(default=None, ge=0)


class TripBrief(BaseModel):
    """Canonical versioned travel brief contract."""

    model_config = ConfigDict(frozen=True)

    origin_name: str
    origin_lat: float = Field(..., ge=-90.0, le=90.0)
    origin_lng: float = Field(..., ge=-180.0, le=180.0)
    destinations: list[DestinationPoint] = Field(..., min_length=1)
    start_date: date
    end_date: date
    timezone: str = Field(
        default="Asia/Kolkata", description="Target IANA timezone string for trip scheduling"
    )
    adults: int = Field(default=1, ge=1)
    child_ages: list[int] = Field(default_factory=list)
    rooms: int = Field(default=1, ge=1)
    target_budget: Money | None = None
    preferred_modes: list[TransportMode] = Field(default_factory=lambda: [TransportMode.CAR_PETROL])
    dietary_preferences: list[str] = Field(default_factory=list)
    accessibility_requirements: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        if not v or not v.strip():
            return "Asia/Kolkata"
        import zoneinfo

        try:
            zoneinfo.ZoneInfo(v.strip())
        except Exception as exc:
            raise ValueError(f"Invalid IANA timezone string: '{v}'") from exc
        return v.strip()

    @field_validator("end_date")
    @classmethod
    def validate_date_range(cls, v: date, info: object) -> date:
        data = getattr(info, "data", {})
        start = data.get("start_date")
        if start and v < start:
            raise ValueError(f"end_date ({v}) cannot precede start_date ({start})")
        return v

    @property
    def total_days(self) -> int:
        """Derived calendar days inclusive."""
        return (self.end_date - self.start_date).days + 1


class TripBriefDraft(BaseModel):
    """Compatible draft travel brief contract for incomplete initial input."""

    model_config = ConfigDict(frozen=True)

    origin_name: str
    origin_lat: float = Field(..., ge=-90.0, le=90.0)
    origin_lng: float = Field(..., ge=-180.0, le=180.0)
    destinations: list[DestinationPoint] = Field(..., min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    timezone: str = Field(
        default="Asia/Kolkata", description="Target IANA timezone string for trip scheduling"
    )
    adults: int = Field(default=1, ge=1)
    child_ages: list[int] = Field(default_factory=list)
    rooms: int = Field(default=1, ge=1)
    target_budget: Money | None = None
    preferred_modes: list[TransportMode] = Field(default_factory=lambda: [TransportMode.CAR_PETROL])
    dietary_preferences: list[str] = Field(default_factory=list)
    accessibility_requirements: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        if not v or not v.strip():
            return "Asia/Kolkata"
        import zoneinfo

        try:
            zoneinfo.ZoneInfo(v.strip())
        except Exception as exc:
            raise ValueError(f"Invalid IANA timezone string: '{v}'") from exc
        return v.strip()

    @field_validator("end_date")
    @classmethod
    def validate_date_range(cls, v: date | None, info: object) -> date | None:
        if v is None:
            return None
        data = getattr(info, "data", {})
        start = data.get("start_date")
        if start and v < start:
            raise ValueError(f"end_date ({v}) cannot precede start_date ({start})")
        return v

    def is_complete(self) -> bool:
        """Returns True if all required planning constraints are populated."""
        return self.start_date is not None and self.end_date is not None

    def get_missing_fields(self) -> list[str]:
        """Returns list of missing required planning constraints."""
        missing: list[str] = []
        if self.start_date is None or self.end_date is None:
            missing.append("travel_dates")
        return missing

    def to_trip_brief(self) -> TripBrief:
        """Transforms into canonical validated TripBrief, raising ValueError if incomplete."""
        if not self.is_complete():
            missing = ", ".join(self.get_missing_fields())
            raise ValueError(f"Cannot convert incomplete draft to TripBrief; missing: {missing}")
        assert self.start_date is not None
        assert self.end_date is not None
        return TripBrief(
            origin_name=self.origin_name,
            origin_lat=self.origin_lat,
            origin_lng=self.origin_lng,
            destinations=self.destinations,
            start_date=self.start_date,
            end_date=self.end_date,
            timezone=self.timezone,
            adults=self.adults,
            child_ages=self.child_ages,
            rooms=self.rooms,
            target_budget=self.target_budget,
            preferred_modes=self.preferred_modes,
            dietary_preferences=self.dietary_preferences,
            accessibility_requirements=self.accessibility_requirements,
            hard_constraints=self.hard_constraints,
        )


class TripVersion(BaseModel):
    """Immutable snapshot of a Trip at a specific version."""

    model_config = ConfigDict(frozen=True)

    trip_id: uuid.UUID
    version: int = Field(..., ge=1)
    brief: TripBrief | TripBriefDraft
    command_id: uuid.UUID
    schema_version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Trip(BaseModel):
    """Trip aggregate root."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    owner_id: uuid.UUID
    current_version: int = Field(default=0, ge=0)
    state: TripState = Field(default=TripState.DRAFT)
    current_route_id: uuid.UUID | None = Field(default=None)
    itinerary_json: dict[str, Any] | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def advance_version(self, expected_base_version: int) -> int:
        """Validates optimistic locking constraint and increments version."""
        if self.current_version != expected_base_version:
            raise ValueError(
                f"Version conflict: current version is {self.current_version}, expected {expected_base_version}"
            )
        self.current_version += 1
        self.updated_at = datetime.now(UTC)
        return self.current_version


class TripDelta(BaseModel):
    """Immutable proposal delta applying changes on top of a base version."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    trip_id: uuid.UUID
    base_version: int = Field(..., ge=1)
    actor_id: uuid.UUID
    proposal: dict[str, Any]
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
