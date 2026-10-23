"""Unit test suite for Slice 3: Multimodal vehicle continuity and parking hub service.

Conforms to specification 0005 AC-4 and AC-5:
- Injects verified PARKING_HUB stops for pedestrian-only destinations.
- Enforces strict vehicle continuity (driving legs depart only from vehicle location).
- Fails closed safely with NO_VERIFIED_PARKING_HUB when no verified parking exists.
- Chains pedestrian walking legs between consecutive venues in the same cluster.
- Segregates driving and walking distances with fuel costs computed strictly on driving.
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
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
    VehicleFuelProfile,
)
from contracts.trips import TransportMode
from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.schemas import CoordinatePoint
from functions.route_optimizer.continuity import (
    VehicleContinuityTracker,
    VehicleContinuityViolationError,
)
from functions.route_optimizer.multimodal import MultimodalTransitionService


def _create_mock_place(
    name: str = "Central Parking Facility",
    lat: float = 22.5726,
    lng: float = 88.3639,
    category: str = "parking",
    is_candidate: bool = False,
    access_status: str = "PUBLIC",
) -> CanonicalPlaceModel:
    """Creates a mock CanonicalPlaceModel for testing."""
    place = CanonicalPlaceModel()
    place.id = uuid.uuid4()
    place.primary_name = name
    place.locality = "Kolkata"
    place.is_candidate = is_candidate
    place.access_status = access_status
    place.provider_ids = {"category": category, "osm_id": f"way/{uuid.uuid4().hex[:8]}"}
    place.point = CoordinatePoint(lng=lng, lat=lat)
    return place


# --- VehicleContinuityTracker Unit Tests ---


def test_vehicle_continuity_tracker_pure_driving_success() -> None:
    """Verifies that normal sequential driving legs satisfy vehicle continuity."""
    tracker = VehicleContinuityTracker()
    start_id = uuid.uuid4()
    stop_b_id = uuid.uuid4()
    stop_c_id = uuid.uuid4()

    origin = ScheduledStopOutput(
        id=start_id,
        stop_order=0,
        name="Hotel Origin",
        location=Point(latitude=22.5700, longitude=88.3600),
        stop_type=StopType.ORIGIN,
    )
    tracker.start_trip(origin, transport_mode=TransportMode.CAR_PETROL)

    leg1 = RouteLegOutput(
        leg_order=0,
        from_stop_id=start_id,
        to_stop_id=stop_b_id,
        from_stop_name="Hotel Origin",
        to_stop_name="Stop B",
        transport_mode=TransportMode.CAR_PETROL,
        departure_at=datetime.now(UTC),
        arrival_at=datetime.now(UTC) + timedelta(minutes=15),
        distance_m=5000,
        travel_seconds=900,
    )
    tracker.step_leg(leg1)

    leg2 = RouteLegOutput(
        leg_order=1,
        from_stop_id=stop_b_id,
        to_stop_id=stop_c_id,
        from_stop_name="Stop B",
        to_stop_name="Stop C",
        transport_mode=TransportMode.CAR_PETROL,
        departure_at=datetime.now(UTC) + timedelta(minutes=45),
        arrival_at=datetime.now(UTC) + timedelta(minutes=60),
        distance_m=4000,
        travel_seconds=900,
    )
    tracker.step_leg(leg2)

    assert tracker.current_stop_id == stop_c_id
    assert tracker.is_vehicle_parked is False


def test_vehicle_continuity_tracker_detects_departure_without_returning_to_car() -> None:
    """Verifies that attempting to drive from a pedestrian stop while car is parked raises an error."""
    tracker = VehicleContinuityTracker()
    hotel_id = uuid.uuid4()
    hub_id = uuid.uuid4()
    pedestrian_id = uuid.uuid4()
    museum_id = uuid.uuid4()

    origin = ScheduledStopOutput(
        id=hotel_id,
        stop_order=0,
        name="Hotel",
        location=Point(latitude=22.5700, longitude=88.3600),
        stop_type=StopType.ORIGIN,
    )
    tracker.start_trip(origin)

    # 1. Drive to Parking Hub
    tracker.step_leg(
        RouteLegOutput(
            leg_order=0,
            from_stop_id=hotel_id,
            to_stop_id=hub_id,
            from_stop_name="Hotel",
            to_stop_name="Parking Hub",
            transport_mode=TransportMode.CAR_PETROL,
            departure_at=datetime.now(UTC),
            arrival_at=datetime.now(UTC) + timedelta(minutes=10),
            distance_m=3000,
            travel_seconds=600,
        )
    )

    # 2. Walk to Pedestrian Precinct
    tracker.step_leg(
        RouteLegOutput(
            leg_order=1,
            from_stop_id=hub_id,
            to_stop_id=pedestrian_id,
            from_stop_name="Parking Hub",
            to_stop_name="Historic Temple",
            transport_mode=TransportMode.WALKING,
            departure_at=datetime.now(UTC) + timedelta(minutes=15),
            arrival_at=datetime.now(UTC) + timedelta(minutes=20),
            distance_m=400,
            travel_seconds=300,
        )
    )
    assert tracker.is_vehicle_parked is True
    assert tracker.vehicle_parked_at_stop_id == hub_id

    # 3. Invalid: Try to drive directly from Historic Temple to Museum without walking back to Hub!
    with pytest.raises(VehicleContinuityViolationError, match="vehicle is parked at stop"):
        tracker.step_leg(
            RouteLegOutput(
                leg_order=2,
                from_stop_id=pedestrian_id,
                to_stop_id=museum_id,
                from_stop_name="Historic Temple",
                to_stop_name="Museum",
                transport_mode=TransportMode.CAR_PETROL,
                departure_at=datetime.now(UTC) + timedelta(hours=1),
                arrival_at=datetime.now(UTC) + timedelta(hours=1, minutes=20),
                distance_m=5000,
                travel_seconds=1200,
            )
        )


def test_vehicle_continuity_tracker_detects_traveler_teleportation() -> None:
    """Verifies that legs starting from a location other than current position raise an error."""
    tracker = VehicleContinuityTracker()
    stop_a = uuid.uuid4()
    stop_b = uuid.uuid4()
    stop_c = uuid.uuid4()

    origin = ScheduledStopOutput(
        id=stop_a,
        stop_order=0,
        name="Stop A",
        location=Point(latitude=22.5700, longitude=88.3600),
        stop_type=StopType.ORIGIN,
    )
    tracker.start_trip(origin)

    with pytest.raises(VehicleContinuityViolationError, match="Traveler continuity violation"):
        tracker.step_leg(
            RouteLegOutput(
                leg_order=0,
                from_stop_id=stop_b,  # Teleportation from Stop B instead of Stop A
                to_stop_id=stop_c,
                from_stop_name="Stop B",
                to_stop_name="Stop C",
                transport_mode=TransportMode.CAR_PETROL,
                departure_at=datetime.now(UTC),
                arrival_at=datetime.now(UTC) + timedelta(minutes=10),
                distance_m=2000,
                travel_seconds=600,
            )
        )


def test_vehicle_continuity_tracker_detects_abandoned_vehicle() -> None:
    """Verifies that an itinerary ending without retrieving the vehicle flags an abandoned vehicle."""
    hotel_id = uuid.uuid4()
    hub_id = uuid.uuid4()
    pedestrian_id = uuid.uuid4()

    hotel_stop = ScheduledStopOutput(
        id=hotel_id,
        stop_order=0,
        name="Hotel",
        location=Point(latitude=22.5700, longitude=88.3600),
        stop_type=StopType.ORIGIN,
    )
    ped_stop = ScheduledStopOutput(
        id=pedestrian_id,
        stop_order=1,
        name="Pedestrian Market",
        location=Point(latitude=22.5720, longitude=88.3620),
        stop_type=StopType.DESTINATION,
    )

    leg1 = RouteLegOutput(
        leg_order=0,
        from_stop_id=hotel_id,
        to_stop_id=hub_id,
        from_stop_name="Hotel",
        to_stop_name="Parking Hub",
        transport_mode=TransportMode.CAR_PETROL,
        departure_at=datetime.now(UTC),
        arrival_at=datetime.now(UTC) + timedelta(minutes=10),
        distance_m=1000,
        travel_seconds=600,
    )
    leg2 = RouteLegOutput(
        leg_order=1,
        from_stop_id=hub_id,
        to_stop_id=pedestrian_id,
        from_stop_name="Parking Hub",
        to_stop_name="Pedestrian Market",
        transport_mode=TransportMode.WALKING,
        departure_at=datetime.now(UTC) + timedelta(minutes=15),
        arrival_at=datetime.now(UTC) + timedelta(minutes=20),
        distance_m=300,
        travel_seconds=300,
    )

    route = OptimizedRoute(
        departure_at=datetime.now(UTC),
        status=RouteStatus.FEASIBLE,
        solver_status=SolverStatus.FEASIBLE,
        objective=RouteObjective.FASTEST,
        total_distance_m=1300,
        total_travel_seconds=900,
        total_dwell_seconds=0,
        total_wait_seconds=0,
        total_elapsed_seconds=900,
        driving_distance_m=1000,
        walking_distance_m=300,
        fuel_amount=Decimal("10.000000"),
        fuel_currency=Currency.INR,
        stops=[hotel_stop, ped_stop],
        legs=[leg1, leg2],
    )

    tracker = VehicleContinuityTracker()
    violations = tracker.validate_route(route)
    assert len(violations) == 1
    assert "Vehicle abandoned" in violations[0]


# --- MultimodalTransitionService Unit Tests ---


@pytest.mark.asyncio
async def test_multimodal_transition_service_injects_parking_hub() -> None:
    """Verifies AC-4: Injects verified parking hub and splits transit into drive/walk/walk-back."""
    mock_hub = _create_mock_place(
        name="Dakshineswar Ghat Parking",
        lat=22.6530,
        lng=88.3570,
    )

    async def mock_search_fn(point: Point, radius: float) -> list[tuple[CanonicalPlaceModel, float]]:
        return [(mock_hub, 250.0)]

    service = MultimodalTransitionService(search_parking_fn=mock_search_fn)

    start_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Kolkata Airport",
        location=Point(latitude=22.6547, longitude=88.4467),
        stop_type=StopType.ORIGIN,
    )
    temple_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Dakshineswar Kali Temple",
        location=Point(latitude=22.6548, longitude=88.3576),
        stop_type=StopType.DESTINATION,
        is_pedestrian_only=True,  # Vehicle restricted precinct!
        dwell_seconds=3600,
    )

    request = RouteRequest(
        departure_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
        stops=[start_stop, temple_stop],
        transport_mode=TransportMode.CAR_PETROL,
        fuel_profile=VehicleFuelProfile.default_for_mode(TransportMode.CAR_PETROL),
        is_round_trip=True,
    )

    route = await service.plan_multimodal_route(request)

    assert route.status == RouteStatus.FEASIBLE
    assert route.solver_status == SolverStatus.FEASIBLE
    assert len(route.diagnostics) == 0

    # Verify stops: Airport -> Parking Hub -> Temple -> [walk back to hub] -> return to Airport
    stop_names = [s.name for s in route.stops]
    assert "Kolkata Airport" in stop_names
    assert "Parking Hub (Dakshineswar Ghat Parking)" in stop_names
    assert "Dakshineswar Kali Temple" in stop_names

    # Check StopType.PARKING_HUB injection
    hub_stops = [s for s in route.stops if s.stop_type == StopType.PARKING_HUB]
    assert len(hub_stops) == 1
    assert hub_stops[0].place_id == mock_hub.id

    # Verify legs sequencing:
    # 1. Drive Airport -> Parking Hub
    # 2. Walk Parking Hub -> Temple
    # 3. Walk Temple -> Parking Hub
    # 4. Drive Parking Hub -> Airport (round trip)
    assert len(route.legs) == 4
    assert route.legs[0].transport_mode == TransportMode.CAR_PETROL
    assert route.legs[1].transport_mode == TransportMode.WALKING
    assert route.legs[1].parking_hub_id == hub_stops[0].id
    assert route.legs[2].transport_mode == TransportMode.WALKING
    assert route.legs[2].parking_hub_id == hub_stops[0].id
    assert route.legs[3].transport_mode == TransportMode.CAR_PETROL

    # Verify distance segregation
    assert route.driving_distance_m > 0
    assert route.walking_distance_m > 0
    assert route.total_distance_m == route.driving_distance_m + route.walking_distance_m

    # Verify fuel calculation: calculated strictly on driving distance!
    expected_km = (Decimal(str(route.driving_distance_m)) / Decimal("1000")).quantize(
        Decimal("0.001")
    )
    expected_liters = (expected_km / Decimal("15.0")).quantize(Decimal("0.000001"))
    expected_fuel = (expected_liters * Decimal("102.50")).quantize(Decimal("0.000001"))
    assert route.fuel_amount == expected_fuel


@pytest.mark.asyncio
async def test_multimodal_transition_service_fails_closed_when_no_parking_hub() -> None:
    """Verifies AC-4 and AC-5: Fails closed with INFEASIBLE and NO_VERIFIED_PARKING_HUB."""

    async def mock_empty_search(point: Point, radius: float) -> list[tuple[CanonicalPlaceModel, float]]:
        return []

    service = MultimodalTransitionService(search_parking_fn=mock_empty_search)

    start_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Hotel",
        location=Point(latitude=22.5700, longitude=88.3600),
        stop_type=StopType.ORIGIN,
    )
    restricted_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Pedestrian Heritage Island",
        location=Point(latitude=22.5800, longitude=88.3700),
        stop_type=StopType.DESTINATION,
        vehicle_restricted=True,
    )

    request = RouteRequest(
        departure_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
        stops=[start_stop, restricted_stop],
        transport_mode=TransportMode.CAR_PETROL,
    )

    route = await service.plan_multimodal_route(request)

    assert route.status == RouteStatus.INFEASIBLE
    assert route.solver_status == SolverStatus.INFEASIBLE
    assert len(route.diagnostics) >= 1
    assert "NO_VERIFIED_PARKING_HUB" in route.diagnostics[0]
    assert "Fails closed without inventing street parking" in route.assumptions


@pytest.mark.asyncio
async def test_multimodal_transition_service_walk_chaining() -> None:
    """Verifies walk chaining across multiple pedestrian destinations sharing a hub."""
    mock_hub = _create_mock_place(
        name="Esplanade Central Parking",
        lat=22.5650,
        lng=88.3520,
    )

    async def mock_search_fn(point: Point, radius: float) -> list[tuple[CanonicalPlaceModel, float]]:
        return [(mock_hub, 200.0)]

    service = MultimodalTransitionService(search_parking_fn=mock_search_fn)

    start_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Hotel",
        location=Point(latitude=22.5400, longitude=88.3400),
        stop_type=StopType.ORIGIN,
    )
    ped1 = RouteStopInput(
        id=uuid.uuid4(),
        name="New Market Pedestrian Street",
        location=Point(latitude=22.5645, longitude=88.3525),
        stop_type=StopType.DESTINATION,
        is_pedestrian_only=True,
        dwell_seconds=1800,
    )
    ped2 = RouteStopInput(
        id=uuid.uuid4(),
        name="Lindsay Street Mall",
        location=Point(latitude=22.5640, longitude=88.3530),
        stop_type=StopType.DESTINATION,
        is_pedestrian_only=True,
        dwell_seconds=1200,
    )

    request = RouteRequest(
        departure_at=datetime(2026, 10, 23, 10, 0, tzinfo=UTC),
        stops=[start_stop, ped1, ped2],
        transport_mode=TransportMode.CAR_PETROL,
    )

    route = await service.plan_multimodal_route(request)

    assert route.status == RouteStatus.FEASIBLE
    # Only 1 parking hub injected for both clustered pedestrian stops!
    hub_stops = [s for s in route.stops if s.stop_type == StopType.PARKING_HUB]
    assert len(hub_stops) == 1

    # Leg trace:
    # 1. Drive Hotel -> Parking Hub
    # 2. Walk Parking Hub -> New Market
    # 3. Walk New Market -> Lindsay Street (walk-chaining without returning to car!)
    # 4. Walk Lindsay Street -> Parking Hub (return to car at the end of pedestrian cluster)
    assert len(route.legs) == 4
    assert route.legs[0].transport_mode == TransportMode.CAR_PETROL
    assert route.legs[1].transport_mode == TransportMode.WALKING
    assert route.legs[2].transport_mode == TransportMode.WALKING
    assert route.legs[2].from_stop_name == "New Market Pedestrian Street"
    assert route.legs[2].to_stop_name == "Lindsay Street Mall"
    assert route.legs[3].transport_mode == TransportMode.WALKING
    assert route.legs[3].from_stop_name == "Lindsay Street Mall"
    assert route.legs[3].to_stop_name == "Parking Hub (Esplanade Central Parking)"


@pytest.mark.asyncio
async def test_multimodal_transition_service_walking_round_trip() -> None:
    """Verifies that pure walking round trips calculate travel times and metrics at walking speeds."""
    service = MultimodalTransitionService()

    start_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Hotel",
        location=Point(latitude=22.5700, longitude=88.3600),
        stop_type=StopType.ORIGIN,
    )
    dest_stop = RouteStopInput(
        id=uuid.uuid4(),
        name="Nearby Cafe",
        location=Point(latitude=22.5730, longitude=88.3620),
        stop_type=StopType.DESTINATION,
        dwell_seconds=900,
    )

    request = RouteRequest(
        departure_at=datetime(2026, 10, 23, 11, 0, tzinfo=UTC),
        stops=[start_stop, dest_stop],
        transport_mode=TransportMode.WALKING,
        is_round_trip=True,
    )

    route = await service.plan_multimodal_route(request)

    assert route.status == RouteStatus.FEASIBLE
    assert route.driving_distance_m == 0
    assert route.walking_distance_m > 0
    assert route.total_distance_m == route.walking_distance_m
    assert route.fuel_amount == Decimal("0.000000")

    # Both legs must be walking
    assert len(route.legs) == 2
    assert route.legs[0].transport_mode == TransportMode.WALKING
    assert route.legs[1].transport_mode == TransportMode.WALKING
    assert route.legs[1].to_stop_name == "Hotel"

