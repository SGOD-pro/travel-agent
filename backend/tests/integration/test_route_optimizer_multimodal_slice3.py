"""Integration test suite for Slice 3: Multimodal vehicle continuity and parking hub service.

Tests spatial PostGIS queries, parking hub injection from real canonical_places,
and atomic multimodal route persistence in PostgreSQL. Conforms to spec 0005 AC-4 and AC-6.
"""

from __future__ import annotations

import socket
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import functions.api.models  # noqa: F401 - register TripModel in metadata
import functions.orchestrator.evidence.models  # noqa: F401 - register ClaimModel in metadata
import functions.orchestrator.execution.models  # noqa: F401 - register ExecutionModel in metadata
import functions.orchestrator.places.models  # noqa: F401 - register CanonicalPlaceModel in metadata
from contracts.routing import (
    Point,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    SolverStatus,
    StopType,
)
from contracts.trips import TransportMode
from functions.api.models import TripModel
from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import CoordinatePoint
from functions.orchestrator.routing.repository import RouteRepository
from functions.route_optimizer.continuity import VehicleContinuityTracker
from functions.route_optimizer.multimodal import MultimodalTransitionService

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


@pytest.mark.asyncio
class TestMultimodalVehicleContinuityAndParkingIntegration:
    """Integration test suite executing spatial PostGIS and route persistence on PostgreSQL."""

    async def test_find_verified_parking_near_spatial_query(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifies PostGIS spatial query finds verified public parking within threshold."""
        rand_offset = float(uuid.uuid4().int % 500) * 0.001 + 0.1
        target_lat = 22.5726 + rand_offset
        target_lng = 88.3639 + rand_offset
        target_pt = CoordinatePoint(lng=target_lng, lat=target_lat)

        verified_parking_id = uuid.uuid4()
        candidate_parking_id = uuid.uuid4()
        restricted_parking_id = uuid.uuid4()
        distant_parking_id = uuid.uuid4()
        bicycle_parking_id = uuid.uuid4()

        inserted_ids = [
            verified_parking_id,
            candidate_parking_id,
            restricted_parking_id,
            distant_parking_id,
            bicycle_parking_id,
        ]

        async with session_factory() as session:
            async with session.begin():
                # 1. Verified public parking within ~350m
                p1 = CanonicalPlaceModel(
                    id=verified_parking_id,
                    primary_name="New Market Verified Municipal Parking",
                    locality="Kolkata",
                    point=WKTElement(f"POINT({target_lng + 0.003} {target_lat + 0.001})", srid=4326),
                    provider_ids={"category": "parking", "amenity": "parking"},
                    access_status="PUBLIC",
                    is_candidate=False,
                )
                session.add(p1)

                # 2. Candidate unverified parking within ~200m (should be excluded)
                p2 = CanonicalPlaceModel(
                    id=candidate_parking_id,
                    primary_name="Unverified Street Parking Lot",
                    locality="Kolkata",
                    point=WKTElement(f"POINT({target_lng + 0.001} {target_lat + 0.001})", srid=4326),
                    provider_ids={"category": "parking"},
                    access_status="PUBLIC",
                    is_candidate=True,
                )
                session.add(p2)

                # 3. Restricted parking within ~250m (should be excluded)
                p3 = CanonicalPlaceModel(
                    id=restricted_parking_id,
                    primary_name="Private Government Officer Parking",
                    locality="Kolkata",
                    point=WKTElement(f"POINT({target_lng + 0.002} {target_lat + 0.001})", srid=4326),
                    provider_ids={"category": "parking"},
                    access_status="RESTRICTED",
                    is_candidate=False,
                )
                session.add(p3)

                # 4. Distant verified parking at ~3000m (should be excluded by 1000m radius)
                p4 = CanonicalPlaceModel(
                    id=distant_parking_id,
                    primary_name="Distant Suburb Parking",
                    locality="Kolkata",
                    point=WKTElement(f"POINT({target_lng + 0.030} {target_lat + 0.030})", srid=4326),
                    provider_ids={"category": "parking"},
                    access_status="PUBLIC",
                    is_candidate=False,
                )
                session.add(p4)

                # 5. Nearby bicycle parking stand at ~250m (should be excluded by bicycle filter)
                p5 = CanonicalPlaceModel(
                    id=bicycle_parking_id,
                    primary_name="Metro Station Bicycle Stand Parking",
                    locality="Kolkata",
                    point=WKTElement(f"POINT({target_lng + 0.002} {target_lat + 0.002})", srid=4326),
                    provider_ids={"amenity": "bicycle_parking"},
                    access_status="PUBLIC",
                    is_candidate=False,
                )
                session.add(p5)

        try:
            # Query using CanonicalPlacesRepository
            async with session_factory() as session:
                repo = CanonicalPlacesRepository(session)
                results = await repo.find_verified_parking_near(target_pt, max_distance_meters=1000.0)

                result_ids = [place.id for place, _ in results]
                assert verified_parking_id in result_ids
                assert candidate_parking_id not in result_ids
                assert restricted_parking_id not in result_ids
                assert distant_parking_id not in result_ids
                assert bicycle_parking_id not in result_ids

                # Verify distance is accurate and under 1000m
                for place, distance_m in results:
                    if place.id == verified_parking_id:
                        assert 100.0 <= distance_m <= 600.0
        finally:
            # Teardown: delete inserted test rows to prevent database residue
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(CanonicalPlaceModel).where(
                            CanonicalPlaceModel.id.in_(inserted_ids)
                        )
                    )

    async def test_multimodal_transition_service_with_real_database(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifies MultimodalTransitionService finds real parking place and injects PARKING_HUB."""
        # Use unique coordinates to avoid proximity collisions with previously inserted rows
        rand_offset = float(uuid.uuid4().int % 500) * 0.001 + 0.1
        heritage_lat = 23.1000 + rand_offset
        heritage_lng = 88.7000 + rand_offset

        parking_id = uuid.uuid4()
        async with session_factory() as session:
            async with session.begin():
                parking_place = CanonicalPlaceModel(
                    id=parking_id,
                    primary_name="Heritage Dedicated Gate Parking",
                    locality="IsolatedSector",
                    point=WKTElement(f"POINT({heritage_lng + 0.002} {heritage_lat + 0.001})", srid=4326),
                    provider_ids={"amenity": "parking"},
                    access_status="PUBLIC",
                    is_candidate=False,
                )
                session.add(parking_place)

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = MultimodalTransitionService(places_repo=repo, search_radius_meters=1000.0)

            start_stop = RouteStopInput(
                id=uuid.uuid4(),
                name="Sector Station",
                location=Point(latitude=heritage_lat + 0.02, longitude=heritage_lng + 0.02),
                stop_type=StopType.ORIGIN,
            )
            ped_stop = RouteStopInput(
                id=uuid.uuid4(),
                name="Heritage Precinct",
                location=Point(latitude=heritage_lat, longitude=heritage_lng),
                stop_type=StopType.DESTINATION,
                is_pedestrian_only=True,
                dwell_seconds=3600,
            )

            request = RouteRequest(
                departure_at=datetime(2026, 10, 23, 9, 30, tzinfo=UTC),
                stops=[start_stop, ped_stop],
                transport_mode=TransportMode.CAR_PETROL,
                is_round_trip=True,
            )

            route = await service.plan_multimodal_route(request)

            assert route.status == RouteStatus.FEASIBLE
            assert route.solver_status == SolverStatus.FEASIBLE

            # Verify PARKING_HUB stop was injected with real canonical_places ID
            hub_stops = [s for s in route.stops if s.stop_type == StopType.PARKING_HUB]
            assert len(hub_stops) == 1
            assert hub_stops[0].place_id == parking_id

            # Verify vehicle continuity
            tracker = VehicleContinuityTracker()
            violations = tracker.validate_route(route)
            assert len(violations) == 0

    async def test_multimodal_missing_parking_fails_closed_in_db(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifies AC-4 and AC-5: Missing parking hub in isolated zone fails closed."""
        # Isolated coordinates in Sundarbans wilderness (zero parking)
        isolated_lat = 21.9497
        isolated_lng = 88.9000

        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = MultimodalTransitionService(places_repo=repo, search_radius_meters=1000.0)

            start = RouteStopInput(
                id=uuid.uuid4(),
                name="Forest Base Camp",
                location=Point(latitude=22.0000, longitude=88.8500),
                stop_type=StopType.ORIGIN,
            )
            ped_stop = RouteStopInput(
                id=uuid.uuid4(),
                name="Mangrove Eco Boardwalk",
                location=Point(latitude=isolated_lat, longitude=isolated_lng),
                stop_type=StopType.DESTINATION,
                is_pedestrian_only=True,
            )

            request = RouteRequest(
                departure_at=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),
                stops=[start, ped_stop],
                transport_mode=TransportMode.CAR_PETROL,
            )

            route = await service.plan_multimodal_route(request)

            assert route.status == RouteStatus.INFEASIBLE
            assert route.solver_status == SolverStatus.INFEASIBLE
            assert any("NO_VERIFIED_PARKING_HUB" in diag for diag in route.diagnostics)

    async def test_multimodal_route_persistence_in_db(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verifies multimodal routes, stops, and legs persist to PostgreSQL with high fidelity."""
        owner_id = uuid.uuid4()
        trip_id = uuid.uuid4()

        # 1. Create Trip row to satisfy foreign key
        async with session_factory() as session:
            async with session.begin():
                trip = TripModel(
                    id=trip_id,
                    owner_id=owner_id,
                    current_version=1,
                    state="draft",
                )
                session.add(trip)

        # 2. Create verified parking place with isolated coordinates
        rand_offset = float(uuid.uuid4().int % 500) * 0.001 + 0.1
        base_lat = 23.3000 + rand_offset
        base_lng = 88.8000 + rand_offset

        parking_id = uuid.uuid4()
        async with session_factory() as session:
            async with session.begin():
                p = CanonicalPlaceModel(
                    id=parking_id,
                    primary_name="Park Street Multimodal Hub",
                    locality="IsolatedSector4",
                    point=WKTElement(f"POINT({base_lng + 0.002} {base_lat + 0.001})", srid=4326),
                    provider_ids={"category": "parking"},
                    access_status="PUBLIC",
                    is_candidate=False,
                )
                session.add(p)

        # 3. Plan multimodal route
        async with session_factory() as session:
            repo = CanonicalPlacesRepository(session)
            service = MultimodalTransitionService(places_repo=repo)

            start = RouteStopInput(
                id=uuid.uuid4(),
                name="Park Street Metro",
                location=Point(latitude=base_lat + 0.02, longitude=base_lng + 0.02),
                stop_type=StopType.ORIGIN,
            )
            dest = RouteStopInput(
                id=uuid.uuid4(),
                name="Pedestrian Heritage Lane",
                location=Point(latitude=base_lat, longitude=base_lng),
                stop_type=StopType.DESTINATION,
                is_pedestrian_only=True,
            )

            request = RouteRequest(
                owner_id=owner_id,
                trip_id=trip_id,
                departure_at=datetime(2026, 10, 23, 11, 0, tzinfo=UTC),
                stops=[start, dest],
                transport_mode=TransportMode.CAR_PETROL,
                is_round_trip=True,
            )

            route = await service.plan_multimodal_route(request)
            assert route.status == RouteStatus.FEASIBLE

        # 4. Save route via RouteRepository
        saved_route_id: uuid.UUID
        async with session_factory() as session:
            async with session.begin():
                route_repo = RouteRepository(session)
                saved_model = await route_repo.save_route(route, owner_id=owner_id)
                saved_route_id = saved_model.id

        # 5. Retrieve route and verify multimodal properties
        async with session_factory() as session:
            route_repo = RouteRepository(session)
            loaded_route = await route_repo.get_route_by_id(saved_route_id, owner_id=owner_id)

            assert loaded_route.id == saved_route_id
            assert loaded_route.trip_id == trip_id
            assert loaded_route.driving_distance_m > 0
            assert loaded_route.walking_distance_m > 0

            # Verify PARKING_HUB stop
            hub_stops = [s for s in loaded_route.stops if s.stop_type == StopType.PARKING_HUB]
            assert len(hub_stops) == 1
            assert hub_stops[0].place_id == parking_id

            # Verify walking legs carry parking_hub_id
            walking_legs = [
                leg for leg in loaded_route.legs if leg.transport_mode == TransportMode.WALKING
            ]
            assert len(walking_legs) >= 2  # Walk to dest, walk back to hub
            for w_leg in walking_legs:
                assert w_leg.parking_hub_id == hub_stops[0].id
