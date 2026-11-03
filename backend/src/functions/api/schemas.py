"""Validated public API request and response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from contracts.trips import TripBrief


class Product(StrEnum):
    MEDIA = "media"
    TRAVEL = "travel"
    ROUTE = "route"


class ApiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RouteDestination(ApiSchema):
    name: str = Field(min_length=1, max_length=200)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    stay_minutes: int = Field(default=180, ge=0, le=1440)


class RouteRequest(ApiSchema):
    origin_name: str = Field(min_length=1, max_length=200)
    origin_lat: float = Field(ge=-90, le=90)
    origin_lng: float = Field(ge=-180, le=180)
    destinations: list[RouteDestination] = Field(min_length=1, max_length=100)
    start_datetime: datetime
    average_speed_kmh: float = Field(default=45.0, gt=0, le=200)
    vehicle_mileage_kmpl: float = Field(default=15.0, gt=0, le=200)
    fuel_price_per_liter: float = Field(default=102.5, ge=0, le=100_000)

    @field_validator("start_datetime")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start_datetime must include a timezone")
        return value


class CreateTripRequest(ApiSchema):
    brief: TripBrief


class CompositionOptions(ApiSchema):
    products: list[Product] = Field(default_factory=lambda: [Product.TRAVEL], min_length=1)
    media_url: HttpUrl | None = None
    route_request: RouteRequest | None = None

    @field_validator("products")
    @classmethod
    def require_unique_products(cls, value: list[Product]) -> list[Product]:
        if len(set(value)) != len(value):
            raise ValueError("products must not contain duplicates")
        order = {Product.MEDIA: 0, Product.TRAVEL: 1, Product.ROUTE: 2}
        if value != sorted(value, key=order.__getitem__):
            raise ValueError("products must be ordered media, travel, then route")
        return value

    @field_validator("media_url")
    @classmethod
    def reject_url_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username is not None or value.password is not None):
            raise ValueError("media_url must not contain embedded credentials")
        return value


class PlanRequest(CompositionOptions):
    planning_options: dict[str, Any] = Field(default_factory=dict)
    workflow: Literal["TRAVEL"] = "TRAVEL"


class MediaDiscoveryRequest(CompositionOptions):
    products: list[Product] = Field(default_factory=lambda: [Product.MEDIA], min_length=1)
    media_url: HttpUrl
    trip_id: UUID | None = None


class RouteOptimizationRequest(CompositionOptions):
    products: list[Product] = Field(default_factory=lambda: [Product.ROUTE], min_length=1)
    route_request: RouteRequest
    media_url: HttpUrl | None = None
    trip_id: UUID | None = None


class AcceptedJobResponse(BaseModel):
    job_id: UUID
    status: str = Field(description="Current asynchronous job status.")
    status_url: str


class CreatedTripResponse(BaseModel):
    id: UUID
    owner_id: UUID
    current_version: int
    state: str
    brief: TripBrief | None = None


class CompositionResult(BaseModel):
    products: list[Product]
    product_outputs: dict[str, Any] = Field(default_factory=dict)
