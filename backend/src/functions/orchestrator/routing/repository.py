"""Repository for route persistence, versioning, and tenant isolation in PostgreSQL.

Conforms strictly to specification 0005 Slice 2:
- Atomic transaction persistence across routes, route_stops, route_legs, and route_restrictions.
- Strict tenant isolation: every query and mutation is fenced by owner_id.
- Optimistic versioning with unique (trip_id, route_version) constraints.
- PostGIS Point and Geometry conversions with Shapely and GeoAlchemy2.
"""

from __future__ import annotations

import uuid

import shapely.geometry
from geoalchemy2.shape import from_shape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from contracts.routing import OptimizedRoute
from functions.orchestrator.routing.models import (
    RouteLegModel,
    RouteModel,
    RouteRestrictionModel,
    RouteStopModel,
)


class TenantMismatchError(ValueError):
    """Raised when request owner_id does not match route entity owner_id."""


class RouteNotFoundError(LookupError):
    """Raised when a route cannot be found for the specified owner."""


class RouteVersionConflictError(ValueError):
    """Raised when attempting to save a route version that already exists for a trip."""


class RouteRepository:
    """Repository managing route persistence, versioning, and tenant isolation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_route(
        self,
        route: OptimizedRoute,
        *,
        owner_id: uuid.UUID,
        trip_id: uuid.UUID | None = None,
        execution_id: uuid.UUID | None = None,
        route_version: int | None = None,
        previous_route_version: int | None = None,
    ) -> RouteModel:
        """Persists optimized route, stops, legs, and restrictions atomically in one transaction.

        Enforces tenant isolation by validating that route.owner_id matches the provided owner_id.
        Handles version sequencing for trip itineraries.
        """
        # 1. Tenant isolation validation
        if route.owner_id is not None and route.owner_id != owner_id:
            raise TenantMismatchError(
                f"Route owner_id {route.owner_id} does not match caller owner_id {owner_id}"
            )

        eff_trip_id = trip_id or route.trip_id
        eff_exec_id = execution_id or route.execution_id
        eff_prev_version = (
            previous_route_version
            if previous_route_version is not None
            else route.previous_route_version
        )

        # 2. Version determination
        if route_version is not None:
            eff_version = route_version
        elif eff_trip_id is not None:
            # Query current max version for this trip and owner
            max_ver_stmt = select(func.max(RouteModel.route_version)).where(
                RouteModel.trip_id == eff_trip_id,
                RouteModel.owner_id == owner_id,
            )
            res = await self._session.execute(max_ver_stmt)
            curr_max = res.scalar_one_or_none()
            eff_version = (curr_max + 1) if curr_max is not None else 1
        else:
            eff_version = route.route_version or 1

        # Check for version conflict if trip_id is present
        if eff_trip_id is not None:
            conflict_stmt = select(RouteModel.id).where(
                RouteModel.trip_id == eff_trip_id,
                RouteModel.route_version == eff_version,
            )
            conflict_res = await self._session.execute(conflict_stmt)
            if conflict_res.scalar_one_or_none() is not None:
                raise RouteVersionConflictError(
                    f"Route version {eff_version} already exists for trip {eff_trip_id}"
                )

        # 3. Create RouteModel
        route_id = route.id or uuid.uuid4()
        route_model = RouteModel(
            id=route_id,
            owner_id=owner_id,
            execution_id=eff_exec_id,
            trip_id=eff_trip_id,
            route_version=eff_version,
            status=route.status.value,
            solver_status=route.solver_status.value,
            objective=route.objective.value,
            departure_at=route.departure_at,
            return_deadline=route.return_deadline,
            actual_return_at=route.actual_return_at,
            total_distance_m=route.total_distance_m,
            total_travel_seconds=route.total_travel_seconds,
            total_dwell_seconds=route.total_dwell_seconds,
            total_wait_seconds=route.total_wait_seconds,
            total_elapsed_seconds=route.total_elapsed_seconds,
            driving_distance_m=route.driving_distance_m,
            walking_distance_m=route.walking_distance_m,
            fuel_amount=route.fuel_amount,
            fuel_currency=route.fuel_currency.value,
            fuel_basis=route.fuel_basis,
            warnings=list(route.warnings),
            assumptions=list(route.assumptions),
            previous_route_version=eff_prev_version,
        )

        # 4. Create RouteStopModels
        stop_id_map: dict[uuid.UUID, uuid.UUID] = {}
        all_stops = list(route.stops) + list(route.dropped_stops)
        stop_models: list[RouteStopModel] = []

        for s in all_stops:
            s_id = s.stop_id or uuid.uuid4()
            if s.stop_id is not None:
                stop_id_map[s.stop_id] = s_id

            pt = shapely.geometry.Point(s.location.longitude, s.location.latitude)
            loc_geom = from_shape(pt, srid=4326)

            stop_model = RouteStopModel(
                id=s_id,
                route_id=route_id,
                place_id=s.place_id,
                stop_order=s.stop_order,
                name=s.name,
                location=loc_geom,
                stop_type=s.stop_type.value,
                is_must_visit=s.is_must_visit,
                is_completed=s.is_completed,
                is_skipped=s.is_skipped,
                skip_reason=s.skip_reason,
                window_start=s.window_start,
                window_end=s.window_end,
                dwell_seconds=s.dwell_seconds,
                planned_arrival_at=s.planned_arrival_at,
                planned_departure_at=s.planned_departure_at,
                wait_seconds=s.wait_seconds,
            )
            stop_models.append(stop_model)

        route_model.stops = stop_models

        # 5. Create RouteLegModels
        leg_models: list[RouteLegModel] = []
        for leg in route.legs:
            # Map stop IDs if newly assigned
            from_id = stop_id_map.get(leg.from_stop_id, leg.from_stop_id)
            to_id = stop_id_map.get(leg.to_stop_id, leg.to_stop_id)
            hub_id = (
                stop_id_map.get(leg.parking_hub_id, leg.parking_hub_id)
                if leg.parking_hub_id is not None
                else None
            )

            leg_model = RouteLegModel(
                id=uuid.uuid4(),
                route_id=route_id,
                leg_order=leg.leg_order,
                from_stop_id=from_id,
                to_stop_id=to_id,
                transport_mode=leg.transport_mode.value,
                departure_at=leg.departure_at,
                arrival_at=leg.arrival_at,
                distance_m=leg.distance_m,
                travel_seconds=leg.travel_seconds,
                geometry=dict(leg.geometry),
                parking_hub_id=hub_id,
                traffic_basis=leg.traffic_basis.value,
                turn_instructions=list(leg.turn_instructions or []),
            )
            leg_models.append(leg_model)

        route_model.legs = leg_models

        # 6. Create RouteRestrictionModels
        restriction_models: list[RouteRestrictionModel] = []
        for r in route.restrictions:
            geom_shape = shapely.geometry.shape(r.geometry)
            geom_elem = from_shape(geom_shape, srid=4326)

            restr_model = RouteRestrictionModel(
                id=r.id or uuid.uuid4(),
                route_id=route_id,
                restriction_type=r.restriction_type.value,
                geometry=geom_elem,
                allowed_modes=list(r.allowed_modes or []),
                effective_start=r.effective_start,
                effective_end=r.effective_end,
                claim_id=r.claim_id,
                was_applied=r.was_applied,
                unresolved_reason=r.unresolved_reason,
            )
            restriction_models.append(restr_model)

        route_model.restrictions = restriction_models

        # 7. Atomic session add and flush
        self._session.add(route_model)
        await self._session.flush()

        return route_model

    async def get_route_by_id(
        self,
        route_id: uuid.UUID,
        *,
        owner_id: uuid.UUID,
    ) -> RouteModel | None:
        """Retrieves an optimized route by ID with strict tenant isolation."""
        stmt = (
            select(RouteModel)
            .where(RouteModel.id == route_id, RouteModel.owner_id == owner_id)
            .options(
                selectinload(RouteModel.stops),
                selectinload(RouteModel.legs),
                selectinload(RouteModel.restrictions),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_route_by_trip(
        self,
        trip_id: uuid.UUID,
        *,
        owner_id: uuid.UUID,
    ) -> RouteModel | None:
        """Retrieves the latest route version for a trip with strict tenant isolation."""
        stmt = (
            select(RouteModel)
            .where(RouteModel.trip_id == trip_id, RouteModel.owner_id == owner_id)
            .order_by(RouteModel.route_version.desc())
            .options(
                selectinload(RouteModel.stops),
                selectinload(RouteModel.legs),
                selectinload(RouteModel.restrictions),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_route_version(
        self,
        trip_id: uuid.UUID,
        route_version: int,
        *,
        owner_id: uuid.UUID,
    ) -> RouteModel | None:
        """Retrieves a specific route version for a trip with tenant isolation."""
        stmt = (
            select(RouteModel)
            .where(
                RouteModel.trip_id == trip_id,
                RouteModel.route_version == route_version,
                RouteModel.owner_id == owner_id,
            )
            .options(
                selectinload(RouteModel.stops),
                selectinload(RouteModel.legs),
                selectinload(RouteModel.restrictions),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_routes_by_owner(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[RouteModel]:
        """Lists routes belonging to a tenant ordered by creation date."""
        stmt = (
            select(RouteModel)
            .where(RouteModel.owner_id == owner_id)
            .order_by(RouteModel.created_at.desc())
            .options(
                selectinload(RouteModel.stops),
                selectinload(RouteModel.legs),
                selectinload(RouteModel.restrictions),
            )
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


__all__ = [
    "RouteNotFoundError",
    "RouteRepository",
    "RouteVersionConflictError",
    "TenantMismatchError",
]
