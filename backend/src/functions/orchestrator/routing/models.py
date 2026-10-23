"""SQLAlchemy ORM models for route optimization persisting into PostgreSQL and PostGIS.

Conforms strictly to specification 0005 Slice 2:
- Table routes: owner-isolated execution and trip route versions with fuel decimal math.
- Table route_stops: ordered stops with PostGIS Point geometries, windows, dwell/wait.
- Table route_legs: ordered multimodal legs with GeoJSON geometries and traffic basis.
- Table route_restrictions: PostGIS spatial restrictions, claim linkages, and mode filters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import shapely.geometry
import sqlalchemy as sa
from geoalchemy2 import Geometry
from geoalchemy2.shape import to_shape
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.base_model import Base
from contracts.money import Currency
from contracts.routing import (
    OptimizedRoute,
    Point,
    RestrictionType,
    RouteLegOutput,
    RouteObjective,
    RouteRestrictionOutput,
    RouteStatus,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
    TrafficBasis,
)
from contracts.trips import TransportMode


class RouteModel(Base):
    """Canonical database model for persisted optimized itineraries."""

    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    solver_status: Mapped[str] = mapped_column(String(32), nullable=False)
    objective: Mapped[str] = mapped_column(String(32), nullable=False)
    departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    return_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_return_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_distance_m: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    total_travel_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    total_dwell_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    total_wait_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    total_elapsed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    driving_distance_m: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    walking_distance_m: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    fuel_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=6), nullable=True
    )
    fuel_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default=sa.text("'INR'")
    )
    fuel_basis: Mapped[str | None] = mapped_column(String(256), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    assumptions: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    previous_route_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    stops: Mapped[list[RouteStopModel]] = relationship(
        "RouteStopModel",
        back_populates="route",
        cascade="all, delete-orphan",
        order_by="RouteStopModel.stop_order",
    )
    legs: Mapped[list[RouteLegModel]] = relationship(
        "RouteLegModel",
        back_populates="route",
        cascade="all, delete-orphan",
        order_by="RouteLegModel.leg_order",
    )
    restrictions: Mapped[list[RouteRestrictionModel]] = relationship(
        "RouteRestrictionModel",
        back_populates="route",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "uq_routes_trip_version",
            "trip_id",
            "route_version",
            unique=True,
            postgresql_where=sa.text("trip_id IS NOT NULL"),
        ),
    )

    def to_optimized_route(self) -> OptimizedRoute:
        """Converts persistent database entity to domain OptimizedRoute."""
        active_stops = [s.to_scheduled_stop_output() for s in self.stops if not s.is_skipped]
        dropped_stops = [s.to_scheduled_stop_output() for s in self.stops if s.is_skipped]
        legs_output = [leg.to_route_leg_output() for leg in self.legs]
        restrictions_output = [r.to_route_restriction_output() for r in self.restrictions]

        return OptimizedRoute(
            id=self.id,
            owner_id=self.owner_id,
            trip_id=self.trip_id,
            execution_id=self.execution_id,
            route_version=self.route_version,
            status=RouteStatus(self.status),
            solver_status=SolverStatus(self.solver_status),
            objective=RouteObjective(self.objective),
            departure_at=self.departure_at,
            return_deadline=self.return_deadline,
            actual_return_at=self.actual_return_at,
            total_distance_m=self.total_distance_m,
            total_travel_seconds=self.total_travel_seconds,
            total_dwell_seconds=self.total_dwell_seconds,
            total_wait_seconds=self.total_wait_seconds,
            total_elapsed_seconds=self.total_elapsed_seconds,
            driving_distance_m=self.driving_distance_m,
            walking_distance_m=self.walking_distance_m,
            fuel_amount=self.fuel_amount,
            fuel_currency=Currency(self.fuel_currency),
            fuel_basis=self.fuel_basis,
            stops=active_stops,
            legs=legs_output,
            restrictions=restrictions_output,
            dropped_stops=dropped_stops,
            warnings=list(self.warnings or []),
            assumptions=list(self.assumptions or []),
            diagnostics=[],
            previous_route_version=self.previous_route_version,
        )


class RouteStopModel(Base):
    """Ordered itinerary stop with PostGIS Point geometry and time windows."""

    __tablename__ = "route_stops"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("canonical_places.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    stop_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    location: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    stop_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_must_visit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    is_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    is_skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dwell_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    planned_arrival_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_departure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wait_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    route: Mapped[RouteModel] = relationship("RouteModel", back_populates="stops")

    __table_args__ = (
        UniqueConstraint("route_id", "stop_order", name="uq_route_stops_route_order"),
        Index("idx_route_stops_location", "location", postgresql_using="gist"),
        Index(
            "idx_route_stops_location_geog",
            sa.text("((location)::geography)"),
            postgresql_using="gist",
        ),
    )

    def extract_point(self) -> Point:
        """Extracts validated geographic coordinates from PostGIS geometry."""
        shape = to_shape(self.location)
        return Point(latitude=float(shape.y), longitude=float(shape.x))

    def to_scheduled_stop_output(self) -> ScheduledStopOutput:
        """Converts persistent stop model to domain ScheduledStopOutput."""
        return ScheduledStopOutput(
            stop_id=self.id,
            place_id=self.place_id,
            stop_order=self.stop_order,
            name=self.name,
            location=self.extract_point(),
            stop_type=StopType(self.stop_type),
            is_must_visit=self.is_must_visit,
            is_completed=self.is_completed,
            is_skipped=self.is_skipped,
            skip_reason=self.skip_reason,
            window_start=self.window_start,
            window_end=self.window_end,
            dwell_seconds=self.dwell_seconds,
            planned_arrival_at=self.planned_arrival_at,
            planned_departure_at=self.planned_departure_at,
            wait_seconds=self.wait_seconds,
        )


class RouteLegModel(Base):
    """Ordered itinerary route leg connecting consecutive stops with GeoJSON geometry."""

    __tablename__ = "route_legs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    leg_order: Mapped[int] = mapped_column(Integer, nullable=False)
    from_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("route_stops.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("route_stops.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transport_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    distance_m: Mapped[int] = mapped_column(Integer, nullable=False)
    travel_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parking_hub_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("route_stops.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    traffic_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default="valhalla", server_default=sa.text("'valhalla'")
    )
    provider_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    turn_instructions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    route: Mapped[RouteModel] = relationship("RouteModel", back_populates="legs")
    from_stop: Mapped[RouteStopModel] = relationship(
        "RouteStopModel", foreign_keys=[from_stop_id], lazy="joined"
    )
    to_stop: Mapped[RouteStopModel] = relationship(
        "RouteStopModel", foreign_keys=[to_stop_id], lazy="joined"
    )
    parking_hub: Mapped[RouteStopModel | None] = relationship(
        "RouteStopModel", foreign_keys=[parking_hub_id], lazy="joined"
    )

    __table_args__ = (
        UniqueConstraint("route_id", "leg_order", name="uq_route_legs_route_order"),
    )

    def to_route_leg_output(self) -> RouteLegOutput:
        """Converts persistent leg model to domain RouteLegOutput."""
        return RouteLegOutput(
            leg_order=self.leg_order,
            from_stop_id=self.from_stop_id,
            to_stop_id=self.to_stop_id,
            from_stop_name=self.from_stop.name if self.from_stop else "",
            to_stop_name=self.to_stop.name if self.to_stop else "",
            transport_mode=TransportMode(self.transport_mode),
            departure_at=self.departure_at,
            arrival_at=self.arrival_at,
            distance_m=self.distance_m,
            travel_seconds=self.travel_seconds,
            geometry=self.geometry,
            traffic_basis=TrafficBasis(self.traffic_basis),
            parking_hub_id=self.parking_hub_id,
            turn_instructions=list(self.turn_instructions or []),
        )


class RouteRestrictionModel(Base):
    """Spatial restriction polygon or linestring associated with an itinerary."""

    __tablename__ = "route_restrictions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    restriction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    geometry: Mapped[Any] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=False,
    )
    allowed_modes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    effective_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    was_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    unresolved_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    route: Mapped[RouteModel] = relationship("RouteModel", back_populates="restrictions")

    __table_args__ = (
        Index("idx_route_restrictions_geometry", "geometry", postgresql_using="gist"),
    )

    def extract_geometry_dict(self) -> dict[str, Any]:
        """Extracts GeoJSON dictionary from PostGIS geometry."""
        shape = to_shape(self.geometry)
        return dict(shapely.geometry.mapping(shape))

    def to_route_restriction_output(self) -> RouteRestrictionOutput:
        """Converts persistent restriction model to domain RouteRestrictionOutput."""
        return RouteRestrictionOutput(
            id=self.id,
            route_id=self.route_id,
            restriction_type=RestrictionType(self.restriction_type),
            geometry=self.extract_geometry_dict(),
            allowed_modes=list(self.allowed_modes or []),
            effective_start=self.effective_start,
            effective_end=self.effective_end,
            claim_id=self.claim_id,
            was_applied=self.was_applied,
            unresolved_reason=self.unresolved_reason,
            created_at=self.created_at,
        )


__all__ = [
    "RouteLegModel",
    "RouteModel",
    "RouteRestrictionModel",
    "RouteStopModel",
]
