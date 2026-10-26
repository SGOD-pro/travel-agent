"""Domain model for Road Transit, Fuel Math, and Route Budgeting.

Conforms strictly to docs/TRD.md:
- fuel_liters = route_distance_km / mileage_km_per_liter
- fuel_cost = fuel_liters * fuel_price_per_liter
- road_total = fuel_cost + applicable_tolls + parking + rental + applicable_fees_and_taxes
- Non-negative costs, positive mileage.
- Unverified tolls/fees remain explicit unknown BudgetLines (never coerced to 0).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.money import BudgetCategory, BudgetLine, Currency
from contracts.trips import TransportMode


class VehicleFuelProfile(BaseModel):
    """Vehicle mileage benchmarks and fuel pricing configuration."""

    model_config = ConfigDict(frozen=True)

    mode: TransportMode
    mileage_km_per_liter: Decimal = Field(..., gt=Decimal("0"))
    fuel_price_per_liter: Decimal = Field(..., gt=Decimal("0"))
    currency: Currency = Field(default=Currency.INR)
    source: str = Field(default="national_average_estimate")

    @classmethod
    def default_for_mode(
        cls,
        mode: TransportMode,
        fuel_price_per_liter: Decimal = Decimal("102.50"),
        currency: Currency = Currency.INR,
    ) -> VehicleFuelProfile:
        if mode == TransportMode.MOTORCYCLE_PETROL:
            return cls(
                mode=mode,
                mileage_km_per_liter=Decimal("40.0"),
                fuel_price_per_liter=fuel_price_per_liter,
                currency=currency,
                source="default_petrol_motorcycle_benchmark",
            )
        # Default to petrol car
        return cls(
            mode=TransportMode.CAR_PETROL,
            mileage_km_per_liter=Decimal("15.0"),
            fuel_price_per_liter=fuel_price_per_liter,
            currency=currency,
            source="default_petrol_car_benchmark",
        )


class RoadSegment(BaseModel):
    """Calculated road segment between two points."""

    model_config = ConfigDict(frozen=True)

    origin_name: str
    destination_name: str
    distance_km: Decimal = Field(..., ge=Decimal("0"))
    duration_minutes: int = Field(..., ge=0)
    toll_charges: Decimal | None = Field(
        default=None, description="Known toll cost, or None if unverified"
    )
    toll_unknown_reason: str | None = Field(
        default=None, description="Reason if toll rates are unverified for this corridor"
    )

    @field_validator("toll_unknown_reason")
    @classmethod
    def validate_toll_reason(cls, v: str | None, info: object) -> str | None:
        data = getattr(info, "data", {})
        if data.get("toll_charges") is None and not v:
            return "Toll plaza rates unverified on this corridor"
        return v


class RoadBudgetCalculator:
    """Computes exact fuel and road transit budget lines."""

    @staticmethod
    def calculate_segment_budget(
        segment: RoadSegment,
        vehicle_profile: VehicleFuelProfile,
    ) -> list[BudgetLine]:
        lines: list[BudgetLine] = []

        # 1. Fuel Calculation
        # fuel_liters = route_distance_km / mileage_km_per_liter
        fuel_liters = (segment.distance_km / vehicle_profile.mileage_km_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        fuel_cost = (fuel_liters * vehicle_profile.fuel_price_per_liter).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )

        lines.append(
            BudgetLine(
                category=BudgetCategory.TRANSPORT,
                amount=fuel_cost,
                currency=vehicle_profile.currency,
                basis=f"fuel_{vehicle_profile.mode.value} ({segment.distance_km}km @ {vehicle_profile.mileage_km_per_liter}km/l)",
                quantity=Decimal("1"),
                source_ref=vehicle_profile.source,
                estimated=True,
            )
        )

        # 2. Toll Calculation (Explicit Unknown if unverified)
        if segment.toll_charges is not None:
            lines.append(
                BudgetLine(
                    category=BudgetCategory.TOLLS_AND_FEES,
                    amount=segment.toll_charges,
                    currency=vehicle_profile.currency,
                    basis=f"toll_{segment.origin_name}_to_{segment.destination_name}",
                    quantity=Decimal("1"),
                    source_ref="official_plaza_rate",
                    estimated=False,
                )
            )
        else:
            lines.append(
                BudgetLine(
                    category=BudgetCategory.TOLLS_AND_FEES,
                    amount=None,
                    currency=vehicle_profile.currency,
                    basis=f"toll_{segment.origin_name}_to_{segment.destination_name}",
                    quantity=Decimal("1"),
                    source_ref="unverified_corridor",
                    estimated=True,
                    unknown_reason=segment.toll_unknown_reason or "Toll rates unverified",
                )
            )

        return lines


class Point(BaseModel):
    """Geographic coordinate point in WGS84."""

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)


class RouteObjective(StrEnum):
    FASTEST = "FASTEST"
    SHORTEST = "SHORTEST"
    BALANCED = "BALANCED"


class RouteStatus(StrEnum):
    FEASIBLE = "FEASIBLE"
    PARTIAL = "PARTIAL"
    INFEASIBLE = "INFEASIBLE"
    UNAVAILABLE = "UNAVAILABLE"


class SolverStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    TIME_LIMIT = "TIME_LIMIT"
    INFEASIBLE = "INFEASIBLE"
    ERROR = "ERROR"


class StopType(StrEnum):
    ORIGIN = "ORIGIN"
    DESTINATION = "DESTINATION"
    WAYPOINT = "WAYPOINT"
    PARKING_HUB = "PARKING_HUB"


class TrafficBasis(StrEnum):
    LIVE = "LIVE"
    MODELED = "MODELED"
    UNKNOWN = "UNKNOWN"


class RestrictionType(StrEnum):
    ROAD_CLOSURE = "ROAD_CLOSURE"
    VEHICLE_ZONE = "VEHICLE_ZONE"
    TIME_WINDOW = "TIME_WINDOW"
    DELAY = "DELAY"


class RouteStopInput(BaseModel):
    """Input stop representation for route optimization."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    place_id: UUID | None = None
    name: str
    location: Point
    stop_type: StopType = StopType.DESTINATION
    is_must_visit: bool = True
    window_start: datetime | None = None
    window_end: datetime | None = None
    dwell_seconds: int = Field(default=0, ge=0)
    is_completed: bool = False
    is_pedestrian_only: bool = False
    vehicle_restricted: bool = False
    parking_hub_id: UUID | None = None


class RouteRequest(BaseModel):
    """Route optimization request payload."""

    model_config = ConfigDict(frozen=True)

    owner_id: UUID | None = None
    trip_id: UUID | None = None
    execution_id: UUID | None = None
    departure_at: datetime
    return_deadline: datetime | None = None
    stops: list[RouteStopInput]
    objective: RouteObjective = RouteObjective.FASTEST
    is_round_trip: bool = False
    start_stop_id: UUID | None = None
    end_stop_id: UUID | None = None
    time_limit_seconds: int = Field(default=10, ge=1)
    transport_mode: TransportMode = TransportMode.CAR_PETROL
    fuel_profile: VehicleFuelProfile | None = None


class RouteReplanRequest(BaseModel):
    """Payload for mid-trip dynamic replanning from traveler live location."""

    model_config = ConfigDict(frozen=True)

    current_location: Point
    replan_at: datetime
    completed_stop_ids: list[UUID] = Field(default_factory=list)
    expected_previous_version: int | None = None
    objective: RouteObjective | None = None
    return_deadline: datetime | None = None
    transport_mode: TransportMode | None = None
    fuel_profile: VehicleFuelProfile | None = None
    time_limit_seconds: int = Field(default=10, ge=1)


class MatrixElement(BaseModel):
    """Individual cell in a distance/duration matrix."""

    model_config = ConfigDict(frozen=True)

    distance_m: int | None = Field(default=None, ge=0)
    duration_s: int | None = Field(default=None, ge=0)
    reachable: bool = True


class MatrixRequest(BaseModel):
    """Matrix calculation request."""

    model_config = ConfigDict(frozen=True)

    origins: list[Point]
    destinations: list[Point]
    mode: TransportMode = TransportMode.CAR_PETROL


class MatrixResponse(BaseModel):
    """Distance and duration matrix response."""

    model_config = ConfigDict(frozen=True)

    durations_s: list[list[int | None]]
    distances_m: list[list[int | None]]


class ScheduledStopOutput(BaseModel):
    """Scheduled stop output from optimization."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    stop_id: UUID | None = None
    place_id: UUID | None = None
    stop_order: int
    name: str
    location: Point
    stop_type: StopType
    is_must_visit: bool = True
    is_completed: bool = False
    is_skipped: bool = False
    skip_reason: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    dwell_seconds: int = 0
    planned_arrival_at: datetime | None = None
    planned_departure_at: datetime | None = None
    wait_seconds: int = 0


class RouteLegOutput(BaseModel):
    """Scheduled transit leg connecting two stops."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    leg_order: int
    from_stop_id: UUID
    to_stop_id: UUID
    from_stop_name: str
    to_stop_name: str
    transport_mode: TransportMode = TransportMode.CAR_PETROL
    departure_at: datetime
    arrival_at: datetime
    distance_m: int
    travel_seconds: int
    geometry: dict[str, Any] = Field(
        default_factory=lambda: {"type": "LineString", "coordinates": []}
    )
    traffic_basis: TrafficBasis = TrafficBasis.MODELED
    parking_hub_id: UUID | None = None
    turn_instructions: list[dict[str, Any]] = Field(default_factory=list)


class RouteRestrictionOutput(BaseModel):
    """Spatial route restriction applied or evaluated during routing."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    route_id: UUID | None = None
    restriction_type: RestrictionType
    geometry: dict[str, Any]
    allowed_modes: list[str] = Field(default_factory=list)
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    claim_id: UUID | None = None
    was_applied: bool = False
    unresolved_reason: str | None = None
    created_at: datetime | None = None


class OptimizedRoute(BaseModel):
    """Complete optimized itinerary route output."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    owner_id: UUID | None = None
    execution_id: UUID | None = None
    trip_id: UUID | None = None
    route_version: int = 1
    status: RouteStatus
    solver_status: SolverStatus
    objective: RouteObjective = RouteObjective.FASTEST
    departure_at: datetime
    return_deadline: datetime | None = None
    actual_return_at: datetime | None = None
    total_distance_m: int = 0
    total_travel_seconds: int = 0
    total_dwell_seconds: int = 0
    total_wait_seconds: int = 0
    total_elapsed_seconds: int = 0
    driving_distance_m: int = 0
    walking_distance_m: int = 0
    fuel_amount: Decimal | None = None
    fuel_currency: Currency = Currency.INR
    fuel_basis: str | None = None
    stops: list[ScheduledStopOutput] = Field(default_factory=list)
    legs: list[RouteLegOutput] = Field(default_factory=list)
    restrictions: list[RouteRestrictionOutput] = Field(default_factory=list)
    dropped_stops: list[ScheduledStopOutput] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    previous_route_version: int | None = None
    parent_route_id: UUID | None = None


class GeoJsonFeature(BaseModel):
    """RFC 7946 GeoJSON Feature representation."""

    model_config = ConfigDict(frozen=True)

    type: str = "Feature"
    geometry: dict[str, Any]
    properties: dict[str, Any] = Field(default_factory=dict)


class RouteMapGeoJsonResponse(BaseModel):
    """RFC 7946 GeoJSON FeatureCollection response with simplestyle attributes (AC-5)."""

    model_config = ConfigDict(frozen=True)

    type: str = "FeatureCollection"
    bbox: list[float] | None = None
    features: list[GeoJsonFeature]
    properties: dict[str, Any] = Field(default_factory=dict)
    route_id: UUID | None = None
    route_version: int = 1
    parent_route_id: UUID | None = None
    status: RouteStatus = RouteStatus.FEASIBLE
    solver_status: SolverStatus = SolverStatus.OPTIMAL
    total_distance_m: int = 0
    total_travel_seconds: int = 0
    fuel_cost: str | None = None
    warnings: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class RouteOptimizerWorkerOutput(BaseModel):
    """Output summary payload emitted by optimizer worker upon task completion."""

    model_config = ConfigDict(frozen=True)

    status: RouteStatus
    route_id: UUID
    route_version: int
    trip_id: UUID | None = None
    execution_id: UUID | None = None
    total_distance_m: int
    total_travel_seconds: int
    stops_count: int
    legs_count: int
    fuel_amount: Decimal | None = None
    fuel_currency: Currency = Currency.INR
    warnings: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class AcceptedRouteResponse(BaseModel):
    """Asynchronous route submission response conforming to 0005 AC-9."""

    job_id: UUID
    status: str = "QUEUED"
    status_url: str


class RouteVersionItem(BaseModel):
    """Individual version summary in a route's lineage."""

    model_config = ConfigDict(frozen=True)

    route_id: UUID
    route_version: int
    parent_route_id: UUID | None = None
    created_at: datetime
    status: str
    total_distance_m: int
    total_travel_seconds: int
    distance_delta_m: int | None = None
    duration_delta_seconds: int | None = None
    dropped_stop_ids: list[UUID] = Field(default_factory=list)
    replan_origin: Point | None = None


class RouteVersionHistoryResponse(BaseModel):
    """Chronological route version history response conforming to spec 0006 AC-7."""

    model_config = ConfigDict(frozen=True)

    root_route_id: UUID
    current_route_id: UUID
    versions: list[RouteVersionItem] = Field(default_factory=list)


# Backward compatibility aliases
RoutePoint = Point
RouteLeg = RouteLegOutput
RouteResponse = OptimizedRoute
RouteAlternative = OptimizedRoute
