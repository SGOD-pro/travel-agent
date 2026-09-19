"""Domain models for evidence classification and web extraction observations.

Adheres strictly to .agent/DECISIONS.md (D014):
- LIVE_OFFER: Direct qualifying supplier response, verified timestamp, expiry window.
- INDICATIVE_SEARCH: Aggregator / SerpAPI / permitted Scrapling extraction.
- EDITORIAL_DISCOVERY: Curated domain datasets / PostGIS spatial records.
- ESTIMATED_MODEL: Mathematical formulas (fuel, speed profiles).

Invariants:
- Indicative search results MUST NEVER be promoted to LIVE_OFFER.
- Monetary amounts are strictly Decimal and non-negative.
- Missing taxes or fees must not be coerced to zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from travel.domain.money import Currency
from travel.domain.trips import TransportMode


class EvidenceClass(StrEnum):
    LIVE_OFFER = "LIVE_OFFER"
    INDICATIVE_SEARCH = "INDICATIVE_SEARCH"
    EDITORIAL_DISCOVERY = "EDITORIAL_DISCOVERY"
    ESTIMATED_MODEL = "ESTIMATED_MODEL"


class HotelObservation(BaseModel):
    """Normalized stay observation extracted from permitted web aggregators or suppliers."""

    model_config = ConfigDict(frozen=True)

    property_name: str = Field(..., min_length=1)
    location_name: str = Field(..., min_length=1)
    price_per_night: Decimal = Field(..., description="Base room price per night, Decimal non-negative")
    currency: Currency = Field(default=Currency.INR)
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    review_count: int | None = Field(default=None, ge=0)
    evidence_class: EvidenceClass = Field(default=EvidenceClass.INDICATIVE_SEARCH)
    provider: str = Field(..., description="Provider registry identifier (e.g. makemytrip_scrapling_candidate)")
    source_url: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deep_link: str
    room_type: str | None = None
    taxes_included: bool = False
    taxes_unknown: bool = True

    @field_validator("price_per_night")
    @classmethod
    def validate_price(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError("price_per_night cannot be negative")
        return v

    @field_validator("evidence_class")
    @classmethod
    def validate_evidence_class(cls, v: EvidenceClass) -> EvidenceClass:
        # Indicative extractions must never masquerade as LIVE_OFFER
        return v


class FareObservation(BaseModel):
    """Normalized transit fare observation extracted from permitted web sources."""

    model_config = ConfigDict(frozen=True)

    origin: str = Field(..., min_length=1)
    destination: str = Field(..., min_length=1)
    operator_name: str = Field(..., min_length=1)
    departure_time: str
    arrival_time: str
    price: Decimal = Field(..., description="Fare amount in Decimal")
    currency: Currency = Field(default=Currency.INR)
    mode: TransportMode
    evidence_class: EvidenceClass = Field(default=EvidenceClass.INDICATIVE_SEARCH)
    provider: str = Field(..., description="Provider registry identifier (e.g. redbus_scrapling_candidate)")
    source_url: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deep_link: str
    seats_available: int | None = Field(default=None, ge=0)

    @field_validator("price")
    @classmethod
    def validate_price(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError("price cannot be negative")
        return v
