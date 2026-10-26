"""Idempotent test fixture seeder for browser acceptance testing.

Pre-populates deterministic canonical fixtures into live PostgreSQL:
1. Multimodal Route (Parking Hub + Walking Leg + Vehicle Return + Driving Home)
2. PARTIAL Outcome Route (Dropped Optional Stop + Advisory Diagnostics)
3. INFEASIBLE Outcome Route (No Verified Parking Hub in Pedestrian Precinct)
4. Foreign Tenant Route (Owned by User B for 403 Forbidden Isolation verification)
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import shapely.geometry
from geoalchemy2.shape import from_shape
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from functions.api.models import ApiIdempotencyModel, TripModel
from functions.orchestrator.execution.models import OutboxEventModel
from functions.orchestrator.routing.models import RouteLegModel, RouteModel, RouteStopModel

logger = logging.getLogger("acceptance_seeder")

USER_A_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_B_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")

DEMO_TRIP_A_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEMO_TRIP_MM_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
DEMO_TRIP_PARTIAL_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
DEMO_TRIP_INFEASIBLE_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
DEMO_TRIP_B_ID = uuid.UUID("99999999-0000-0000-0000-000000000001")

MULTIMODAL_ROUTE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
PARTIAL_ROUTE_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
INFEASIBLE_ROUTE_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
USER_B_ROUTE_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


async def seed_acceptance_fixtures(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Idempotently seeds all browser acceptance test entities."""
    now = datetime.now(UTC)

    async with session_factory() as session:
        # 0. Clean up any previous replan child routes and idempotency reservations
        child_stmt = select(RouteModel.id).where(RouteModel.parent_route_id == MULTIMODAL_ROUTE_ID)
        child_ids = (await session.execute(child_stmt)).scalars().all()
        for cid in child_ids:
            await session.execute(delete(RouteLegModel).where(RouteLegModel.route_id == cid))
            await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id == cid))
            await session.execute(delete(RouteModel).where(RouteModel.id == cid))

        await session.execute(
            delete(ApiIdempotencyModel).where(
                ApiIdempotencyModel.owner_id == USER_A_ID,
                ApiIdempotencyModel.operation == "route_optimization:replan_reservation",
                ApiIdempotencyModel.key == str(MULTIMODAL_ROUTE_ID),
            )
        )

        # Clean up any existing routes on DEMO_TRIP_A_ID so Scenario 1 starts fresh
        trip_a_routes_stmt = select(RouteModel.id).where(RouteModel.trip_id == DEMO_TRIP_A_ID)
        trip_a_rids = (await session.execute(trip_a_routes_stmt)).scalars().all()
        for rid in trip_a_rids:
            await session.execute(delete(RouteLegModel).where(RouteLegModel.route_id == rid))
            await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id == rid))
            await session.execute(delete(RouteModel).where(RouteModel.id == rid))

        # Re-seed canonical fixtures if on legacy trip IDs
        for fix_id in (MULTIMODAL_ROUTE_ID, PARTIAL_ROUTE_ID, INFEASIBLE_ROUTE_ID):
            existing = await session.get(RouteModel, fix_id)
            if existing is not None and existing.trip_id == DEMO_TRIP_A_ID:
                await session.execute(delete(RouteLegModel).where(RouteLegModel.route_id == fix_id))
                await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id == fix_id))
                await session.execute(delete(RouteModel).where(RouteModel.id == fix_id))

        # Mark historical/stuck outbox events as delivered so the worker queue starts clean
        await session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.delivered_at.is_(None))
            .values(delivered_at=now)
        )

        # Normalize any legacy transport modes in existing route_legs
        await session.execute(
            update(RouteLegModel)
            .where(RouteLegModel.transport_mode.in_(["DRIVE", "drive"]))
            .values(transport_mode="car_petrol")
        )
        await session.execute(
            update(RouteLegModel)
            .where(RouteLegModel.transport_mode.in_(["WALK", "walk"]))
            .values(transport_mode="walking")
        )

        # 1. Ensure Trip records exist for all isolated test scenarios
        trips_to_seed = [
            (DEMO_TRIP_A_ID, USER_A_ID),
            (DEMO_TRIP_MM_ID, USER_A_ID),
            (DEMO_TRIP_PARTIAL_ID, USER_A_ID),
            (DEMO_TRIP_INFEASIBLE_ID, USER_A_ID),
            (DEMO_TRIP_B_ID, USER_B_ID),
        ]
        for t_id, o_id in trips_to_seed:
            trip_rec = await session.get(TripModel, t_id)
            if trip_rec is None:
                trip_rec = TripModel(
                    id=t_id,
                    owner_id=o_id,
                    current_version=1,
                    state="draft",
                    created_at=now,
                    updated_at=now,
                )
                session.add(trip_rec)

        await session.commit()

    # 2. Seed Multimodal Route
    async with session_factory() as session:
        existing_mm = await session.get(RouteModel, MULTIMODAL_ROUTE_ID)
        if existing_mm is None:
            mm_route = RouteModel(
                id=MULTIMODAL_ROUTE_ID,
                owner_id=USER_A_ID,
                trip_id=DEMO_TRIP_MM_ID,
                route_version=1,
                status="FEASIBLE",
                solver_status="OPTIMAL",
                objective="FASTEST",
                departure_at=now,
                total_distance_m=3300,
                total_travel_seconds=1000,
                total_dwell_seconds=1800,
                total_wait_seconds=0,
                total_elapsed_seconds=2800,
                driving_distance_m=2400,
                walking_distance_m=900,
                fuel_amount=Decimal("45.00"),
                fuel_currency="INR",
                warnings=[],
                assumptions=["Verified public parking hub injected at Cubbon Park Hub"],
                created_at=now,
                updated_at=now,
            )
            session.add(mm_route)

            # Stops
            stop_origin = RouteStopModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                stop_order=1,
                name="Bengaluru MG Road Depot",
                location=from_shape(shapely.geometry.Point(77.5946, 12.9716), srid=4326),
                stop_type="ORIGIN",
                is_must_visit=True,
                dwell_seconds=0,
            )
            stop_park_in = RouteStopModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                stop_order=2,
                name="Cubbon Park Verified Parking Hub",
                location=from_shape(shapely.geometry.Point(77.5900, 12.9750), srid=4326),
                stop_type="PARKING_HUB",
                is_must_visit=True,
                dwell_seconds=120,
            )
            stop_ped = RouteStopModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                stop_order=3,
                name="Botanical Glass House Sanctuary",
                location=from_shape(shapely.geometry.Point(77.5920, 12.9770), srid=4326),
                stop_type="DESTINATION",
                is_must_visit=True,
                dwell_seconds=1800,
            )
            stop_park_out = RouteStopModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                stop_order=4,
                name="Cubbon Park Verified Parking Hub (Vehicle Return)",
                location=from_shape(shapely.geometry.Point(77.5900, 12.9750), srid=4326),
                stop_type="PARKING_HUB",
                is_must_visit=True,
                dwell_seconds=120,
            )
            stop_return = RouteStopModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                stop_order=5,
                name="Bengaluru MG Road Depot (Return)",
                location=from_shape(shapely.geometry.Point(77.5946, 12.9716), srid=4326),
                stop_type="RETURN_ORIGIN",
                is_must_visit=True,
                dwell_seconds=0,
            )
            session.add_all([stop_origin, stop_park_in, stop_ped, stop_park_out, stop_return])

            # Legs
            leg1 = RouteLegModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                leg_order=1,
                from_stop_id=stop_origin.id,
                to_stop_id=stop_park_in.id,
                transport_mode="car_petrol",
                departure_at=now,
                arrival_at=now + timedelta(seconds=180),
                distance_m=1200,
                travel_seconds=180,
                traffic_basis="HISTORICAL_SPEEDS",
                geometry={
                    "type": "LineString",
                    "coordinates": [[77.5946, 12.9716], [77.5920, 12.9730], [77.5900, 12.9750]],
                },
            )
            leg2 = RouteLegModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                leg_order=2,
                from_stop_id=stop_park_in.id,
                to_stop_id=stop_ped.id,
                transport_mode="walking",
                departure_at=now + timedelta(seconds=300),
                arrival_at=now + timedelta(seconds=620),
                distance_m=450,
                travel_seconds=320,
                traffic_basis="PEDESTRIAN_NETWORK",
                geometry={
                    "type": "LineString",
                    "coordinates": [[77.5900, 12.9750], [77.5910, 12.9760], [77.5920, 12.9770]],
                },
            )
            leg3 = RouteLegModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                leg_order=3,
                from_stop_id=stop_ped.id,
                to_stop_id=stop_park_out.id,
                transport_mode="walking",
                departure_at=now + timedelta(seconds=2420),
                arrival_at=now + timedelta(seconds=2740),
                distance_m=450,
                travel_seconds=320,
                traffic_basis="PEDESTRIAN_NETWORK",
                geometry={
                    "type": "LineString",
                    "coordinates": [[77.5920, 12.9770], [77.5910, 12.9760], [77.5900, 12.9750]],
                },
            )
            leg4 = RouteLegModel(
                id=uuid.uuid4(),
                route_id=MULTIMODAL_ROUTE_ID,
                leg_order=4,
                from_stop_id=stop_park_out.id,
                to_stop_id=stop_return.id,
                transport_mode="car_petrol",
                departure_at=now + timedelta(seconds=2860),
                arrival_at=now + timedelta(seconds=3040),
                distance_m=1200,
                travel_seconds=180,
                traffic_basis="HISTORICAL_SPEEDS",
                geometry={
                    "type": "LineString",
                    "coordinates": [[77.5900, 12.9750], [77.5920, 12.9730], [77.5946, 12.9716]],
                },
            )
            session.add_all([leg1, leg2, leg3, leg4])
            await session.commit()
            logger.info("Seeded Multimodal Route fixture: %s", MULTIMODAL_ROUTE_ID)

    # 3. Seed PARTIAL Outcome Route
    async with session_factory() as session:
        existing_part = await session.get(RouteModel, PARTIAL_ROUTE_ID)
        if existing_part is None:
            part_route = RouteModel(
                id=PARTIAL_ROUTE_ID,
                owner_id=USER_A_ID,
                trip_id=DEMO_TRIP_PARTIAL_ID,
                route_version=1,
                status="PARTIAL",
                solver_status="FEASIBLE",
                objective="FASTEST",
                departure_at=now,
                total_distance_m=140000,
                total_travel_seconds=9000,
                total_dwell_seconds=3600,
                total_wait_seconds=0,
                total_elapsed_seconds=12600,
                driving_distance_m=140000,
                walking_distance_m=0,
                fuel_amount=Decimal("1250.00"),
                fuel_currency="INR",
                warnings=["Optional stop 'Tiger Hill Summit' dropped: hard time window cannot be met"],
                assumptions=[
                    "Optional stop 'Tiger Hill Summit' dropped: arrival at 18:15 exceeds 17:00 window",
                    "Surviving route is optimal across all mandatory stops",
                ],
                created_at=now,
                updated_at=now,
            )
            session.add(part_route)

            p_stop1 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=PARTIAL_ROUTE_ID,
                stop_order=1,
                name="Bengaluru MG Road Origin",
                location=from_shape(shapely.geometry.Point(77.5946, 12.9716), srid=4326),
                stop_type="ORIGIN",
                is_must_visit=True,
                is_completed=True,
            )
            p_stop2 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=PARTIAL_ROUTE_ID,
                stop_order=2,
                name="Mysuru Palace Heritage",
                location=from_shape(shapely.geometry.Point(76.6551, 12.3051), srid=4326),
                stop_type="DESTINATION",
                is_must_visit=True,
                is_completed=True,
            )
            p_stop3 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=PARTIAL_ROUTE_ID,
                stop_order=3,
                name="Tiger Hill Summit (Dropped)",
                location=from_shape(shapely.geometry.Point(75.7382, 12.4244), srid=4326),
                stop_type="DESTINATION",
                is_must_visit=False,
                is_skipped=True,
                skip_reason="TIME_WINDOW_VIOLATION: Arrival at 18:15 exceeds 17:00 window",
            )
            session.add_all([p_stop1, p_stop2, p_stop3])

            p_leg = RouteLegModel(
                id=uuid.uuid4(),
                route_id=PARTIAL_ROUTE_ID,
                leg_order=1,
                from_stop_id=p_stop1.id,
                to_stop_id=p_stop2.id,
                transport_mode="car_petrol",
                departure_at=now,
                arrival_at=now + timedelta(seconds=9000),
                distance_m=140000,
                travel_seconds=9000,
                traffic_basis="HISTORICAL_SPEEDS",
                geometry={
                    "type": "LineString",
                    "coordinates": [[77.5946, 12.9716], [77.1000, 12.7000], [76.6551, 12.3051]],
                },
            )
            session.add(p_leg)
            await session.commit()
            logger.info("Seeded PARTIAL Route fixture: %s", PARTIAL_ROUTE_ID)

    # 4. Seed INFEASIBLE Hard Constraint Outcome Route
    async with session_factory() as session:
        existing_inf = await session.get(RouteModel, INFEASIBLE_ROUTE_ID)
        if existing_inf is None:
            inf_route = RouteModel(
                id=INFEASIBLE_ROUTE_ID,
                owner_id=USER_A_ID,
                trip_id=DEMO_TRIP_INFEASIBLE_ID,
                route_version=1,
                status="INFEASIBLE",
                solver_status="INFEASIBLE",
                objective="FASTEST",
                departure_at=now,
                total_distance_m=0,
                total_travel_seconds=0,
                total_dwell_seconds=0,
                total_wait_seconds=0,
                total_elapsed_seconds=0,
                driving_distance_m=0,
                walking_distance_m=0,
                fuel_amount=Decimal("0.00"),
                fuel_currency="INR",
                warnings=["Route cannot be completed by vehicle without verified parking"],
                assumptions=[
                    "NO_VERIFIED_PARKING_HUB: Destination 'Mangrove Boardwalk' lies in a pedestrian-only precinct, but zero verified parking places exist within 1000m threshold",
                    "Mathematical Hard Constraint Infeasibility: Zero feasible itineraries found satisfying mandatory constraints",
                ],
                created_at=now,
                updated_at=now,
            )
            session.add(inf_route)

            inf_stop1 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=INFEASIBLE_ROUTE_ID,
                stop_order=1,
                name="Forest Base Camp",
                location=from_shape(shapely.geometry.Point(88.8500, 22.0000), srid=4326),
                stop_type="ORIGIN",
                is_must_visit=True,
                is_skipped=False,
            )
            inf_stop2 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=INFEASIBLE_ROUTE_ID,
                stop_order=2,
                name="Mangrove Boardwalk Sanctuary",
                location=from_shape(shapely.geometry.Point(88.9000, 21.9497), srid=4326),
                stop_type="DESTINATION",
                is_must_visit=True,
                is_skipped=True,
                skip_reason="NO_VERIFIED_PARKING_HUB: No verified parking in precinct"[:64],
            )
            session.add_all([inf_stop1, inf_stop2])
            await session.commit()
            logger.info("Seeded INFEASIBLE Route fixture: %s", INFEASIBLE_ROUTE_ID)

    # 5. Seed User B Foreign Route (For 403 Forbidden Isolation Verification)
    async with session_factory() as session:
        existing_b = await session.get(RouteModel, USER_B_ROUTE_ID)
        if existing_b is None:
            b_route = RouteModel(
                id=USER_B_ROUTE_ID,
                owner_id=USER_B_ID,
                trip_id=DEMO_TRIP_B_ID,
                route_version=1,
                status="FEASIBLE",
                solver_status="OPTIMAL",
                objective="FASTEST",
                departure_at=now,
                total_distance_m=50000,
                total_travel_seconds=3600,
                total_dwell_seconds=0,
                total_wait_seconds=0,
                total_elapsed_seconds=3600,
                driving_distance_m=50000,
                walking_distance_m=0,
                fuel_amount=Decimal("500.00"),
                fuel_currency="INR",
                warnings=[],
                assumptions=["User B private route"],
                created_at=now,
                updated_at=now,
            )
            session.add(b_route)
            b_stop = RouteStopModel(
                id=uuid.uuid4(),
                route_id=USER_B_ROUTE_ID,
                stop_order=1,
                name="User B Private Depot",
                location=from_shape(shapely.geometry.Point(77.5946, 12.9716), srid=4326),
                stop_type="ORIGIN",
                is_must_visit=True,
            )
            session.add(b_stop)
            await session.commit()
            logger.info("Seeded User B Route fixture: %s", USER_B_ROUTE_ID)
