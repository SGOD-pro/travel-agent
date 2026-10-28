"""Integration tests for specification 0005 Slice 2: Database migration and PostGIS repository.

Verifies:
1. Alembic migration 0012_add_route_optimization defines routes, route_stops, route_legs,
   and route_restrictions with UUID foreign keys, Numeric(20, 6) fuel precision, and PostGIS indexes.
2. RouteRepository atomic transaction persistence across all four tables.
3. PostGIS Point and Geometry spatial encoding and decoding.
4. Strict tenant isolation on get, list, and save operations.
5. Optimistic route versioning and unique constraint enforcement for trip routes.
"""

from __future__ import annotations

import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import functions.api.models  # noqa: F401 - register TripModel in metadata
import functions.orchestrator.evidence.models  # noqa: F401 - register ClaimModel in metadata
import functions.orchestrator.execution.models  # noqa: F401 - register ExecutionModel in metadata
import functions.orchestrator.places.models  # noqa: F401 - register CanonicalPlaceModel in metadata
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
from functions.api.models import TripModel
from functions.orchestrator.routing.repository import (
    RouteRepository,
    RouteVersionConflictError,
    TenantMismatchError,
)

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("139.59.42.218", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("139.59.42.218", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    last_err: Exception | None = None
    for _ in range(5):
        try:
            res = _orig_getaddrinfo(host, port, *args, **kwargs)
            _dns_cache[key] = res
            return res
        except socket.gaierror as e:
            last_err = e
            time.sleep(0.3)
    if key in _dns_cache:
        return _dns_cache[key]
    if last_err:
        raise last_err
    raise socket.gaierror(-5, "No address associated with hostname")


socket.getaddrinfo = _resilient_getaddrinfo




async def _create_test_trip(
    session_factory: async_sessionmaker[AsyncSession],
    trip_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> None:
    """Helper creating a test trip in PostgreSQL to satisfy foreign key constraints."""
    async with session_factory() as session:
        async with session.begin():
            trip = TripModel(
                id=trip_id,
                owner_id=owner_id,
                current_version=1,
                state="draft",
            )
            session.add(trip)


def _build_sample_optimized_route(
    owner_id: uuid.UUID,
    trip_id: uuid.UUID | None = None,
    execution_id: uuid.UUID | None = None,
    route_version: int = 1,
) -> OptimizedRoute:
    """Creates a sample domain OptimizedRoute with stops, legs, and restrictions."""
    departure = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)

    stop1_id = uuid.uuid4()
    stop2_id = uuid.uuid4()
    stop3_id = uuid.uuid4()

    stops = [
        ScheduledStopOutput(
            stop_id=stop1_id,
            stop_order=0,
            name="Kolkata Airport",
            location=Point(latitude=22.6547, longitude=88.4467),
            stop_type=StopType.ORIGIN,
            is_must_visit=True,
            dwell_seconds=0,
            planned_arrival_at=departure,
            planned_departure_at=departure,
            wait_seconds=0,
        ),
        ScheduledStopOutput(
            stop_id=stop2_id,
            stop_order=1,
            name="Victoria Memorial",
            location=Point(latitude=22.5448, longitude=88.3426),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            dwell_seconds=3600,
            window_start=departure + timedelta(hours=1),
            window_end=departure + timedelta(hours=8),
            planned_arrival_at=departure + timedelta(minutes=45),
            planned_departure_at=departure + timedelta(minutes=105),
            wait_seconds=0,
        ),
        ScheduledStopOutput(
            stop_id=stop3_id,
            stop_order=2,
            name="Howrah Bridge",
            location=Point(latitude=22.5851, longitude=88.3468),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            dwell_seconds=1800,
            planned_arrival_at=departure + timedelta(minutes=120),
            planned_departure_at=departure + timedelta(minutes=150),
            wait_seconds=0,
        ),
    ]

    legs = [
        RouteLegOutput(
            leg_order=0,
            from_stop_id=stop1_id,
            to_stop_id=stop2_id,
            from_stop_name="Kolkata Airport",
            to_stop_name="Victoria Memorial",
            transport_mode=TransportMode.CAR_PETROL,
            departure_at=departure,
            arrival_at=departure + timedelta(minutes=45),
            distance_m=18500,
            travel_seconds=2700,
            geometry={
                "type": "LineString",
                "coordinates": [[88.4467, 22.6547], [88.3426, 22.5448]],
            },
            traffic_basis=TrafficBasis.MODELED,
        ),
        RouteLegOutput(
            leg_order=1,
            from_stop_id=stop2_id,
            to_stop_id=stop3_id,
            from_stop_name="Victoria Memorial",
            to_stop_name="Howrah Bridge",
            transport_mode=TransportMode.CAR_PETROL,
            departure_at=departure + timedelta(minutes=105),
            arrival_at=departure + timedelta(minutes=120),
            distance_m=5200,
            travel_seconds=900,
            geometry={
                "type": "LineString",
                "coordinates": [[88.3426, 22.5448], [88.3468, 22.5851]],
            },
            traffic_basis=TrafficBasis.MODELED,
        ),
    ]

    restrictions = [
        RouteRestrictionOutput(
            restriction_type=RestrictionType.VEHICLE_ZONE,
            geometry={
                "type": "Polygon",
                "coordinates": [
                    [
                        [88.3400, 22.5400],
                        [88.3450, 22.5400],
                        [88.3450, 22.5500],
                        [88.3400, 22.5500],
                        [88.3400, 22.5400],
                    ]
                ],
            },
            allowed_modes=["WALK"],
            was_applied=True,
        )
    ]

    return OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=owner_id,
        trip_id=trip_id,
        execution_id=execution_id,
        route_version=route_version,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.FEASIBLE,
        objective=RouteObjective.FASTEST,
        departure_at=departure,
        return_deadline=departure + timedelta(hours=10),
        actual_return_at=departure + timedelta(minutes=150),
        total_distance_m=23700,
        total_travel_seconds=3600,
        total_dwell_seconds=5400,
        total_wait_seconds=0,
        total_elapsed_seconds=9000,
        driving_distance_m=23700,
        walking_distance_m=0,
        fuel_amount=Decimal("204.550000"),
        fuel_currency=Currency.INR,
        fuel_basis="fuel_car_petrol (23.7km @ 12.5km/l)",
        stops=stops,
        legs=legs,
        restrictions=restrictions,
        dropped_stops=[],
        warnings=["TEST_WARNING"],
        assumptions=["Road routing network"],
    )


class TestRouteOptimizationMigrationAndRepository:
    """Tests covering migration schema definitions and RouteRepository implementation."""

    @pytest.mark.asyncio
    async def test_migration_0012_schema_and_indexes_exist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Verify tables routes, route_stops, route_legs, and route_restrictions exist with PostGIS indexes."""
        async with session_factory() as session:
            # Check table existence via information schema
            table_query = text("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name IN ('routes', 'route_stops', 'route_legs', 'route_restrictions');
            """)
            result = await session.execute(table_query)
            tables = {row[0] for row in result.fetchall()}
            assert {"routes", "route_stops", "route_legs", "route_restrictions"}.issubset(tables)

            # Check fuel_amount precision (Numeric(20, 6)) on routes table
            col_query = text("""
                SELECT column_name, data_type, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_name = 'routes' AND column_name = 'fuel_amount';
            """)
            col_res = await session.execute(col_query)
            col_row = col_res.fetchone()
            assert col_row is not None
            assert col_row[1] == "numeric"
            assert col_row[2] == 20
            assert col_row[3] == 6

            # Check PostGIS spatial indexes exist
            idx_query = text("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE tablename IN ('route_stops', 'route_restrictions');
            """)
            idx_res = await session.execute(idx_query)
            indexes = {row[0]: row[1] for row in idx_res.fetchall()}

            assert "idx_route_stops_location" in indexes
            assert "gist" in indexes["idx_route_stops_location"].lower()
            assert "idx_route_restrictions_geometry" in indexes
            assert "gist" in indexes["idx_route_restrictions_geometry"].lower()

    @pytest.mark.asyncio
    async def test_route_repository_atomic_save_and_retrieve(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Verify atomic transaction persistence across routes, stops, legs, and restrictions."""
        owner_id = uuid.uuid4()
        trip_id = uuid.uuid4()
        await _create_test_trip(session_factory, trip_id, owner_id)
        sample_route = _build_sample_optimized_route(owner_id=owner_id, trip_id=trip_id)

        async with session_factory() as session:
            async with session.begin():
                repo = RouteRepository(session)
                saved_model = await repo.save_route(sample_route, owner_id=owner_id)
                saved_id = saved_model.id

        # Retrieve in a separate transaction
        async with session_factory() as session:
            repo = RouteRepository(session)
            retrieved = await repo.get_route_by_id(saved_id, owner_id=owner_id)

            assert retrieved is not None
            assert retrieved.id == sample_route.id
            assert retrieved.owner_id == owner_id
            assert retrieved.trip_id == trip_id
            assert retrieved.route_version == 1
            assert retrieved.status == "FEASIBLE"
            assert retrieved.total_distance_m == 23700
            assert retrieved.fuel_amount == Decimal("204.550000")
            assert retrieved.fuel_currency == "INR"

            # Check stops
            assert len(retrieved.stops) == 3
            assert [s.stop_order for s in retrieved.stops] == [0, 1, 2]
            stop0 = retrieved.stops[0]
            assert stop0.name == "Kolkata Airport"
            pt0 = stop0.extract_point()
            assert abs(pt0.latitude - 22.6547) < 1e-4
            assert abs(pt0.longitude - 88.4467) < 1e-4

            # Check legs
            assert len(retrieved.legs) == 2
            leg0 = retrieved.legs[0]
            assert leg0.leg_order == 0
            assert leg0.distance_m == 18500
            assert leg0.transport_mode == TransportMode.CAR_PETROL.value
            assert leg0.traffic_basis == "MODELED"

            # Check restrictions
            assert len(retrieved.restrictions) == 1
            restr = retrieved.restrictions[0]
            assert restr.restriction_type == "VEHICLE_ZONE"
            assert restr.was_applied is True
            geom_dict = restr.extract_geometry_dict()
            assert geom_dict["type"] == "Polygon"

            # Verify domain model conversion round-trip
            domain_route = retrieved.to_optimized_route()
            assert domain_route.id == sample_route.id
            assert domain_route.total_distance_m == sample_route.total_distance_m
            assert len(domain_route.stops) == len(sample_route.stops)
            assert len(domain_route.legs) == len(sample_route.legs)
            assert len(domain_route.restrictions) == len(sample_route.restrictions)

    @pytest.mark.asyncio
    async def test_tenant_isolation_enforcement(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Verify cross-tenant access returns None and tenant mismatch raises error."""
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        sample_route = _build_sample_optimized_route(owner_id=owner_a, trip_id=None)

        async with session_factory() as session:
            async with session.begin():
                repo = RouteRepository(session)
                saved_model = await repo.save_route(sample_route, owner_id=owner_a)
                route_id = saved_model.id

        async with session_factory() as session:
            repo = RouteRepository(session)
            # Owner A can access
            res_a = await repo.get_route_by_id(route_id, owner_id=owner_a)
            assert res_a is not None

            # Owner B cannot access (returns None)
            res_b = await repo.get_route_by_id(route_id, owner_id=owner_b)
            assert res_b is None

            # Owner B cannot list Owner A routes
            routes_b = await repo.list_routes_by_owner(owner_b)
            assert all(r.id != route_id for r in routes_b)

            # Saving with mismatched owner_id raises TenantMismatchError
            with pytest.raises(TenantMismatchError, match="does not match caller owner_id"):
                await repo.save_route(sample_route, owner_id=owner_b)

    @pytest.mark.asyncio
    async def test_route_versioning_and_conflict_rejection(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Verify sequential route versions for trips and conflict rejection on duplicates."""
        owner_id = uuid.uuid4()
        trip_id = uuid.uuid4()
        await _create_test_trip(session_factory, trip_id, owner_id)

        route_v1 = _build_sample_optimized_route(owner_id=owner_id, trip_id=trip_id, route_version=1)
        route_v2 = _build_sample_optimized_route(owner_id=owner_id, trip_id=trip_id, route_version=2)

        async with session_factory() as session:
            async with session.begin():
                repo = RouteRepository(session)
                saved_v1 = await repo.save_route(route_v1, owner_id=owner_id)
                assert saved_v1.route_version == 1

        async with session_factory() as session:
            async with session.begin():
                repo = RouteRepository(session)
                # Auto-detects next version as 2 when not explicitly specified
                saved_v2 = await repo.save_route(
                    route_v2,
                    owner_id=owner_id,
                    previous_route_version=1,
                )
                assert saved_v2.route_version == 2
                assert saved_v2.previous_route_version == 1

        async with session_factory() as session:
            repo = RouteRepository(session)
            latest = await repo.get_latest_route_by_trip(trip_id, owner_id=owner_id)
            assert latest is not None
            assert latest.route_version == 2

            v1_fetch = await repo.get_route_version(trip_id, 1, owner_id=owner_id)
            assert v1_fetch is not None
            assert v1_fetch.route_version == 1

            # Attempting to save duplicate version 1 for same trip raises RouteVersionConflictError
            duplicate_v1 = _build_sample_optimized_route(
                owner_id=owner_id, trip_id=trip_id, route_version=1
            )

        async with session_factory() as session:
            async with session.begin():
                repo = RouteRepository(session)
                with pytest.raises(RouteVersionConflictError, match="already exists"):
                    await repo.save_route(
                        duplicate_v1,
                        owner_id=owner_id,
                        route_version=1,
                    )
