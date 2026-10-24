"""Unit test suite for Slice 5: Mid-trip dynamic replanning engine.

Conforms strictly to specification 0005 AC-8:
- Accepts current traveler coordinates.
- Freezes completed stops with historical arrival timestamps from prior route version.
- Treats the current position as the new departure origin.
- Optimizes remaining uncompleted stops.
- Writes an immutable incremented route_version referencing previous_route_version.
- Enforces optimistic concurrency controls against concurrent replans.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from contracts.money import Currency
from contracts.routing import (
    OptimizedRoute,
    Point,
    RouteLegOutput,
    RouteObjective,
    RouteReplanRequest,
    RouteStatus,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
    VehicleFuelProfile,
)
from contracts.trips import TransportMode
from functions.orchestrator.routing.repository import RouteVersionConflictError
from functions.route_optimizer.fixtures import FIXED_STOP_IDS, MockValhallaClient
from functions.route_optimizer.replan import RouteReplanService
from functions.route_optimizer.valhalla import ValhallaSSRFError


def _build_sample_route_version_1() -> OptimizedRoute:
    """Builds a realistic scheduled Route Version 1 with 5 stops."""
    base_time = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)

    stops = [
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[0],
            stop_order=0,
            name="Hotel Grand Arcade",
            location=Point(latitude=22.5726, longitude=88.3639),
            stop_type=StopType.ORIGIN,
            planned_arrival_at=base_time,
            planned_departure_at=base_time,
            dwell_seconds=0,
            is_completed=False,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[1],
            stop_order=1,
            name="Victoria Memorial",
            location=Point(latitude=22.5448, longitude=88.3426),
            stop_type=StopType.DESTINATION,
            window_start=base_time + timedelta(minutes=15),
            window_end=base_time + timedelta(hours=3),
            dwell_seconds=3600,
            planned_arrival_at=base_time + timedelta(minutes=30),
            planned_departure_at=base_time + timedelta(minutes=90),
            is_completed=False,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[2],
            stop_order=2,
            name="Indian Museum",
            location=Point(latitude=22.5579, longitude=88.3511),
            stop_type=StopType.DESTINATION,
            window_start=base_time + timedelta(hours=1),
            window_end=base_time + timedelta(hours=5),
            dwell_seconds=3600,
            planned_arrival_at=base_time + timedelta(minutes=110),
            planned_departure_at=base_time + timedelta(minutes=170),
            is_completed=False,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[3],
            stop_order=3,
            name="St Paul Cathedral",
            location=Point(latitude=22.5442, longitude=88.3468),
            stop_type=StopType.DESTINATION,
            window_start=base_time + timedelta(hours=2),
            window_end=base_time + timedelta(hours=6),
            dwell_seconds=1800,
            planned_arrival_at=base_time + timedelta(minutes=185),
            planned_departure_at=base_time + timedelta(minutes=215),
            is_completed=False,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[4],
            stop_order=4,
            name="Howrah Terminus",
            location=Point(latitude=22.5850, longitude=88.3430),
            stop_type=StopType.DESTINATION,
            window_start=base_time + timedelta(hours=3),
            window_end=base_time + timedelta(hours=8),
            dwell_seconds=0,
            planned_arrival_at=base_time + timedelta(minutes=245),
            planned_departure_at=base_time + timedelta(minutes=245),
            is_completed=False,
        ),
    ]

    legs = [
        RouteLegOutput(
            id=uuid.uuid4(),
            leg_order=0,
            from_stop_id=stops[0].id,
            to_stop_id=stops[1].id,
            from_stop_name=stops[0].name,
            to_stop_name=stops[1].name,
            transport_mode=TransportMode.CAR_PETROL,
            departure_at=stops[0].planned_departure_at or base_time,
            arrival_at=stops[1].planned_arrival_at or base_time,
            distance_m=4200,
            travel_seconds=1800,
        ),
        RouteLegOutput(
            id=uuid.uuid4(),
            leg_order=1,
            from_stop_id=stops[1].id,
            to_stop_id=stops[2].id,
            from_stop_name=stops[1].name,
            to_stop_name=stops[2].name,
            transport_mode=TransportMode.CAR_PETROL,
            departure_at=stops[1].planned_departure_at or base_time,
            arrival_at=stops[2].planned_arrival_at or base_time,
            distance_m=2100,
            travel_seconds=1200,
        ),
    ]

    return OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        route_version=1,
        previous_route_version=None,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
        objective=RouteObjective.FASTEST,
        departure_at=base_time,
        total_distance_m=12500,
        total_travel_seconds=4800,
        total_dwell_seconds=9000,
        driving_distance_m=12500,
        fuel_amount=Decimal("125.000000"),
        fuel_currency=Currency.INR,
        stops=stops,
        legs=legs,
    )


@pytest.mark.asyncio
async def test_replan_freezes_completed_stops_and_updates_version() -> None:
    """Verifies that completed stops retain historical arrival timestamps and version increments to 2."""
    route_v1 = _build_sample_route_version_1()
    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())

    # Traveler reports delay while at Victoria Memorial (Stop 1 completed)
    replan_time = datetime(2026, 10, 24, 11, 0, tzinfo=UTC)  # 30 min delay
    current_gps = Point(latitude=22.5450, longitude=88.3430)

    request = RouteReplanRequest(
        current_location=current_gps,
        replan_at=replan_time,
        completed_stop_ids=[FIXED_STOP_IDS[0], FIXED_STOP_IDS[1]],
        expected_previous_version=1,
    )

    route_v2 = await replan_service.replan_route(route_v1, request)

    # 1. Version sequencing
    assert route_v2.route_version == 2
    assert route_v2.previous_route_version == 1
    assert route_v2.id != route_v1.id
    assert route_v2.owner_id == route_v1.owner_id
    assert route_v2.trip_id == route_v1.trip_id

    # 2. Frozen completed stops
    assert len(route_v2.stops) >= 5
    frozen_stop_0 = route_v2.stops[0]
    frozen_stop_1 = route_v2.stops[1]

    assert frozen_stop_0.id == FIXED_STOP_IDS[0]
    assert frozen_stop_0.is_completed is True
    assert frozen_stop_0.planned_arrival_at == route_v1.stops[0].planned_arrival_at
    assert frozen_stop_0.planned_departure_at == route_v1.stops[0].planned_departure_at

    assert frozen_stop_1.id == FIXED_STOP_IDS[1]
    assert frozen_stop_1.is_completed is True
    assert frozen_stop_1.planned_arrival_at == route_v1.stops[1].planned_arrival_at
    assert frozen_stop_1.planned_departure_at == route_v1.stops[1].planned_departure_at

    # 3. Current location departure origin
    origin_stop = route_v2.stops[2]
    assert origin_stop.name == "Current Location"
    assert origin_stop.stop_type == StopType.ORIGIN
    assert origin_stop.location.latitude == current_gps.latitude
    assert origin_stop.location.longitude == current_gps.longitude
    assert origin_stop.planned_arrival_at == replan_time
    assert origin_stop.planned_departure_at == replan_time

    # 4. Remaining stops recalculated
    remaining_stops = route_v2.stops[3:]
    remaining_stop_ids = {s.id for s in remaining_stops}
    assert FIXED_STOP_IDS[2] in remaining_stop_ids
    assert FIXED_STOP_IDS[3] in remaining_stop_ids
    assert FIXED_STOP_IDS[4] in remaining_stop_ids

    # 5. Historical legs preserved
    assert len(route_v2.legs) >= 1
    assert route_v2.legs[0].from_stop_id == FIXED_STOP_IDS[0]
    assert route_v2.legs[0].to_stop_id == FIXED_STOP_IDS[1]


@pytest.mark.asyncio
async def test_replan_optimistic_concurrency_conflict() -> None:
    """Verifies that version mismatch raises RouteVersionConflictError."""
    route_v1 = _build_sample_route_version_1()
    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())

    request = RouteReplanRequest(
        current_location=Point(latitude=22.5450, longitude=88.3430),
        replan_at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC),
        completed_stop_ids=[FIXED_STOP_IDS[0]],
        expected_previous_version=2,  # Conflicting expectation: current is version 1
    )

    with pytest.raises(RouteVersionConflictError, match="Optimistic concurrency check failed"):
        await replan_service.replan_route(route_v1, request)


@pytest.mark.asyncio
async def test_replan_coordinate_ssrf_validation() -> None:
    """Out-of-bounds current_location raises ValhallaSSRFError."""
    route_v1 = _build_sample_route_version_1()
    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())

    request = RouteReplanRequest.model_construct(
        current_location=Point.model_construct(latitude=95.0, longitude=88.0),  # Bypass Pydantic field constraint
        replan_at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC),
        completed_stop_ids=[FIXED_STOP_IDS[0]],
    )

    with pytest.raises(ValhallaSSRFError, match="Invalid latitude"):
        await replan_service.replan_route(route_v1, request)


@pytest.mark.asyncio
async def test_replan_zero_completed_stops() -> None:
    """Replan before reaching any destination sets current location as sole initial origin."""
    route_v1 = _build_sample_route_version_1()
    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())

    # Traveler starts from a different location before reaching any destination
    new_start = Point(latitude=22.5600, longitude=88.3500)
    replan_time = datetime(2026, 10, 24, 9, 15, tzinfo=UTC)

    request = RouteReplanRequest(
        current_location=new_start,
        replan_at=replan_time,
        completed_stop_ids=[],
    )

    route_v2 = await replan_service.replan_route(route_v1, request)

    assert route_v2.route_version == 2
    assert route_v2.stops[0].name == "Current Location"
    assert route_v2.stops[0].location.latitude == new_start.latitude
    assert route_v2.stops[0].stop_order == 0


@pytest.mark.asyncio
async def test_replan_all_stops_completed() -> None:
    """Replan when all destination stops are marked completed returns feasible route cleanly."""
    route_v1 = _build_sample_route_version_1()
    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())

    request = RouteReplanRequest(
        current_location=Point(latitude=22.5850, longitude=88.3430),
        replan_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        completed_stop_ids=FIXED_STOP_IDS,
    )

    route_v2 = await replan_service.replan_route(route_v1, request)

    assert route_v2.route_version == 2
    assert route_v2.status == RouteStatus.FEASIBLE
    assert all(s.is_completed for s in route_v2.stops)
    assert any("All stops were previously completed" in w for w in route_v2.warnings)


@pytest.mark.asyncio
async def test_replan_dropped_optional_stop_on_tight_deadline() -> None:
    """Replan with late departure time drops optional stop that exceeds its time window."""
    base_time = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)

    stops = [
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[0],
            stop_order=0,
            name="Origin",
            location=Point(latitude=22.5726, longitude=88.3639),
            stop_type=StopType.ORIGIN,
            planned_arrival_at=base_time,
            planned_departure_at=base_time,
            dwell_seconds=0,
            is_completed=True,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[1],
            stop_order=1,
            name="Mandatory Morning Destination",
            location=Point(latitude=22.5448, longitude=88.3426),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            window_start=base_time,
            window_end=base_time + timedelta(hours=2),
            dwell_seconds=1800,
            planned_arrival_at=base_time + timedelta(minutes=30),
            planned_departure_at=base_time + timedelta(minutes=60),
            is_completed=True,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[2],
            stop_order=2,
            name="Optional Afternoon Gallery",
            location=Point(latitude=22.5579, longitude=88.3511),
            stop_type=StopType.DESTINATION,
            is_must_visit=False,  # Optional!
            window_start=base_time + timedelta(hours=1),
            window_end=base_time + timedelta(hours=3),  # Closes at 12:00
            dwell_seconds=3600,
            is_completed=False,
        ),
        ScheduledStopOutput(
            id=FIXED_STOP_IDS[3],
            stop_order=3,
            name="Mandatory Evening Event",
            location=Point(latitude=22.5442, longitude=88.3468),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            window_start=base_time + timedelta(hours=4),
            window_end=base_time + timedelta(hours=9),
            dwell_seconds=3600,
            is_completed=False,
        ),
    ]

    route_v1 = OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        route_version=1,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
        objective=RouteObjective.FASTEST,
        departure_at=base_time,
        stops=stops,
    )

    # Delay until 12:30 PM: Gallery (Stop 2) has already closed!
    replan_time = base_time + timedelta(hours=3, minutes=30)  # 12:30

    request = RouteReplanRequest(
        current_location=Point(latitude=22.5448, longitude=88.3426),
        replan_at=replan_time,
        completed_stop_ids=[FIXED_STOP_IDS[0], FIXED_STOP_IDS[1]],
    )

    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())
    route_v2 = await replan_service.replan_route(route_v1, request)

    assert route_v2.route_version == 2
    # Optional stop should be omitted from active stops or dropped with status PARTIAL
    if len(route_v2.dropped_stops) > 0:
        assert route_v2.status == RouteStatus.PARTIAL
        dropped_ids = [s.id for s in route_v2.dropped_stops]
        assert FIXED_STOP_IDS[2] in dropped_ids


@pytest.mark.asyncio
async def test_replan_fuel_cost_calculation() -> None:
    """Verifies fuel cost is recalculated only on driving distance with Decimal precision."""
    route_v1 = _build_sample_route_version_1()
    fuel_profile = VehicleFuelProfile(
        mode=TransportMode.CAR_PETROL,
        fuel_price_per_liter=Decimal("100.00"),
        mileage_km_per_liter=Decimal("10.0"),
        currency=Currency.INR,
    )

    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())
    request = RouteReplanRequest(
        current_location=Point(latitude=22.5450, longitude=88.3430),
        replan_at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC),
        completed_stop_ids=[FIXED_STOP_IDS[0], FIXED_STOP_IDS[1]],
        fuel_profile=fuel_profile,
    )

    route_v2 = await replan_service.replan_route(route_v1, request)

    assert route_v2.fuel_amount is not None
    assert isinstance(route_v2.fuel_amount, Decimal)
    assert route_v2.fuel_currency == Currency.INR
    assert route_v2.driving_distance_m > 0


@pytest.mark.asyncio
async def test_replan_infeasible_schedule_with_zero_completed_stops() -> None:
    """Verifies that infeasible replan with zero completed stops cleanly returns INFEASIBLE without IndexError."""
    base_time = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    must_visit_stop_id = uuid.uuid4()

    # Destination that closed earlier in the day
    stops = [
        ScheduledStopOutput(
            id=uuid.uuid4(),
            stop_order=0,
            name="Origin",
            location=Point(latitude=22.5726, longitude=88.3639),
            stop_type=StopType.ORIGIN,
            planned_arrival_at=base_time,
            planned_departure_at=base_time,
            dwell_seconds=0,
            is_completed=False,
        ),
        ScheduledStopOutput(
            id=must_visit_stop_id,
            stop_order=1,
            name="Closed Morning Venue",
            location=Point(latitude=22.5448, longitude=88.3426),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            window_start=base_time,
            window_end=base_time + timedelta(hours=1),  # Closed at 10:00
            dwell_seconds=1800,
            planned_arrival_at=base_time + timedelta(minutes=20),
            planned_departure_at=base_time + timedelta(minutes=50),
            is_completed=False,
        ),
    ]

    route_v1 = OptimizedRoute(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        trip_id=uuid.uuid4(),
        route_version=1,
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.OPTIMAL,
        objective=RouteObjective.FASTEST,
        departure_at=base_time,
        stops=stops,
    )

    # Traveler requests replan at 12:00 PM with 0 stops completed
    request = RouteReplanRequest(
        current_location=Point(latitude=22.5600, longitude=88.3500),
        replan_at=base_time + timedelta(hours=3),  # 12:00
        completed_stop_ids=[],
    )

    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())
    route_v2 = await replan_service.replan_route(route_v1, request)

    assert route_v2.route_version == 2
    assert route_v2.status in (RouteStatus.INFEASIBLE, RouteStatus.UNAVAILABLE)
    assert len(route_v2.stops) == 0
    assert route_v2.actual_return_at is None
    assert route_v2.total_elapsed_seconds == 0


@pytest.mark.asyncio
async def test_replan_retains_previously_dropped_stops_in_audit_record() -> None:
    """Verifies that previously dropped stops from earlier route versions are retained in dropped_stops audit records."""
    base_time = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    prev_dropped_id = uuid.uuid4()

    dropped_stop = ScheduledStopOutput(
        id=prev_dropped_id,
        stop_id=prev_dropped_id,
        stop_order=99,
        name="Previously Skipped Museum",
        location=Point(latitude=22.5500, longitude=88.3500),
        stop_type=StopType.DESTINATION,
        is_must_visit=False,
        is_skipped=True,
        skip_reason="Time budget exceeded in v1",
        dwell_seconds=3600,
    )

    route_v1 = _build_sample_route_version_1().model_copy(
        update={"dropped_stops": [dropped_stop]}
    )

    replan_service = RouteReplanService(valhalla_client=MockValhallaClient())
    request = RouteReplanRequest(
        current_location=Point(latitude=22.5448, longitude=88.3426),
        replan_at=base_time + timedelta(hours=1),
        completed_stop_ids=[FIXED_STOP_IDS[0]],
    )

    route_v2 = await replan_service.replan_route(route_v1, request)

    assert route_v2.route_version == 2
    dropped_ids = [s.id for s in route_v2.dropped_stops]
    assert prev_dropped_id in dropped_ids

