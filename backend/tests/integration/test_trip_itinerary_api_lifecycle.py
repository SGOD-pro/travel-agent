"""PostgreSQL integration tests for spec 0007 Slice 6: Itinerary and Weather REST API lifecycle.

Conforms to spec 0007 AC-8, AC-10, and AC-11 against isolated PostgreSQL:
1. Active planning execution returns HTTP 409 Conflict ("Trip planning is in progress").
2. Published proposal returns HTTP 200 OK with complete schedule, budget lines,
   candidate provenance, weather advisories, and linked route GeoJSON map.
3. Unknown toll costs preserve null amounts and unknown reasons without zero coercion.
4. Assumed daylight visiting hours are explicitly labeled as HOURS_ASSUMED.
5. Weather endpoint returns Open Meteo attribution notice.
6. Tenant isolation is enforced: cross-tenant access returns HTTP 403 Forbidden.
7. Missing resources return HTTP 404 Not Found.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import shapely.geometry
from geoalchemy2.shape import from_shape
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from config.db import get_session
from contracts.routing import (
    StopType,
    TrafficBasis,
)
from functions.api.app import app
from functions.api.models import TripModel, TripVersionModel
from functions.api.repository import SqlAlchemyUnitOfWork
from functions.api.routes.trips import get_uow
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.routing.models import RouteLegModel, RouteModel, RouteStopModel
from middleware.auth import AuthenticatedUser, get_current_user


@pytest.mark.asyncio
async def test_trip_itinerary_api_e2e_lifecycle(session_factory) -> None:
    """Verifies end-to-end HTTP itinerary API contract against live PostgreSQL."""
    owner_id = uuid.uuid4()
    foreign_owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    route_id = uuid.uuid4()
    command_id = uuid.uuid4()
    now = datetime.now(UTC)

    owner_user = AuthenticatedUser(id=owner_id, email="owner@swena.internal")
    foreign_user = AuthenticatedUser(id=foreign_owner_id, email="intruder@swena.internal")

    # Dependency overrides for FastAPI app
    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_uow] = lambda: SqlAlchemyUnitOfWork(session_factory)

    brief_data = {
        "origin_name": "Bengaluru",
        "origin_lat": 12.9716,
        "origin_lng": 77.5946,
        "destinations": [
            {"name": "Mysuru Palace", "latitude": 12.3051, "longitude": 76.6551, "stay_days": 1}
        ],
        "start_date": "2026-11-14",
        "end_date": "2026-11-16",
        "timezone": "Asia/Kolkata",
    }

    proposal_payload = {
        "trip_id": str(trip_id),
        "version": 1,
        "status": "ready_for_approval",
        "schedule": {
            "stops": [
                {
                    "position": 1,
                    "name": "Bengaluru",
                    "latitude": 12.9716,
                    "longitude": 77.5946,
                    "arrival_time": "2026-11-14T09:00:00Z",
                    "departure_time": "2026-11-14T09:30:00Z",
                    "stay_duration_minutes": 30,
                    "is_hours_assumed": False,
                    "hours_status": "CONFIRMED",
                },
                {
                    "position": 2,
                    "name": "Mysuru Palace",
                    "latitude": 12.3051,
                    "longitude": 76.6551,
                    "arrival_time": "2026-11-14T12:30:00Z",
                    "departure_time": "2026-11-14T16:30:00Z",
                    "stay_duration_minutes": 240,
                    "is_hours_assumed": True,
                    "hours_status": "HOURS_ASSUMED",
                },
            ],
            "legs": [
                {
                    "from_stop": "Bengaluru",
                    "to_stop": "Mysuru Palace",
                    "duration_minutes": 180,
                    "distance_km": 145.0,
                }
            ],
            "total_travel_minutes": 180,
            "is_optimal": True,
        },
        "budget_summary": {
            "known_total": {"amount": "4750.00", "currency": "INR"},
            "is_complete": False,
            "unknown_count": 1,
            "unknown_categories": ["tolls_and_fees"],
        },
        "budget_lines": [
            {
                "category": "transportation",
                "amount": "3750.00",
                "currency": "INR",
                "basis": "fuel_estimate_car_petrol",
                "quantity": "1",
                "estimated": True,
                "unknown_reason": None,
            },
            {
                "category": "activities",
                "amount": "1000.00",
                "currency": "INR",
                "basis": "entry_fee_mysuru_palace",
                "quantity": "1",
                "estimated": False,
                "unknown_reason": None,
            },
            {
                "category": "tolls_and_fees",
                "amount": None,
                "currency": "INR",
                "basis": "nh275_expressway_toll",
                "quantity": "1",
                "estimated": False,
                "unknown_reason": "Toll rates unverified for NH 275 expressway",
            },
        ],
        "editorial_recommendations": [
            {
                "name": "Ranganathittu Bird Sanctuary",
                "category": "nature",
                "fit_score": 0.94,
                "uncertainty": "EDITORIAL_DISCOVERY",
                "description": "Nesting waterfowl sanctuary along Kaveri river",
                "source": "KSTDC Tourism Portal",
            }
        ],
        "excluded_candidates": [],
        "weather_advisories": [
            {
                "stop_name": "Bengaluru",
                "latitude": 12.9716,
                "longitude": 77.5946,
                "temperature_celsius": 24.5,
                "condition": "sunny",
                "humidity_percent": 55,
                "monsoon_risk": "low",
            },
            {
                "stop_name": "Mysuru Palace",
                "latitude": 12.3051,
                "longitude": 76.6551,
                "temperature_celsius": 27.2,
                "condition": "cloudy",
                "humidity_percent": 65,
                "monsoon_risk": "low",
            },
        ],
        "weather_status": "AVAILABLE",
        "validated_candidates": [],
        "discovered_places": [],
        "route_id": str(route_id),
        "route_status": "FEASIBLE",
        "routing_status": "OPTIMAL",
        "route_diagnostics": [],
        "provider_diagnostics": {
            "weather": {"provider": "open_meteo", "horizon_days": 16}
        },
        "generated_at": now.isoformat(),
    }

    # 1. Seed draft trip and active execution in PostgreSQL
    async with session_factory() as session:
        trip = TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="planning",
            current_route_id=None,
            itinerary_json=None,
            created_at=now,
            updated_at=now,
        )
        trip_ver = TripVersionModel(
            trip_id=trip_id,
            version=1,
            brief_json=brief_data,
            schema_version=1,
            command_id=command_id,
            created_at=now,
        )
        active_exec = ExecutionModel(
            id=execution_id,
            owner_id=owner_id,
            workflow="trip_planner",
            status="RUNNING",
            trip_id=trip_id,
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add_all([trip, trip_ver, active_exec])
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 2. Test 409 Conflict when planning is in progress
        app.dependency_overrides[get_current_user] = lambda: owner_user
        res_conflict = await client.get(f"/api/v1/trips/{trip_id}/itinerary")
        assert res_conflict.status_code == 409
        assert "Trip planning is in progress" in res_conflict.json()["detail"]

        # 3. Seed Route, RouteStops, and RouteLegs, and update Trip with published proposal
        async with session_factory() as session:
            # Mark execution complete
            exec_row = (await session.execute(
                select(ExecutionModel).where(ExecutionModel.id == execution_id)
            )).scalar_one()
            exec_row.status = "SUCCEEDED"

            # Create route
            route = RouteModel(
                id=route_id,
                owner_id=owner_id,
                execution_id=execution_id,
                trip_id=trip_id,
                route_version=1,
                status="FEASIBLE",
                solver_status="OPTIMAL",
                objective="FASTEST",
                departure_at=now,
                total_distance_m=145000,
                total_travel_seconds=10800,
                created_at=now,
                updated_at=now,
            )
            stop_1 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=route_id,
                stop_order=1,
                name="Bengaluru",
                stop_type=StopType.ORIGIN.value,
                location=from_shape(shapely.geometry.Point(77.5946, 12.9716), srid=4326),
                dwell_seconds=1800,
                wait_seconds=0,
                is_skipped=False,
            )
            stop_2 = RouteStopModel(
                id=uuid.uuid4(),
                route_id=route_id,
                stop_order=2,
                name="Mysuru Palace",
                stop_type=StopType.DESTINATION.value,
                location=from_shape(shapely.geometry.Point(76.6551, 12.3051), srid=4326),
                dwell_seconds=14400,
                wait_seconds=0,
                is_skipped=False,
            )
            leg_1 = RouteLegModel(
                id=uuid.uuid4(),
                route_id=route_id,
                leg_order=1,
                from_stop_id=stop_1.id,
                to_stop_id=stop_2.id,
                transport_mode="car_petrol",
                departure_at=now,
                arrival_at=now + timedelta(hours=3),
                distance_m=145000,
                travel_seconds=10800,
                traffic_basis=TrafficBasis.HISTORICAL_SPEEDS.value,
                geometry={"type": "LineString", "coordinates": [[77.5946, 12.9716], [76.6551, 12.3051]]},
            )
            session.add_all([route, stop_1, stop_2, leg_1])

            # Update trip with published proposal
            trip_row = (await session.execute(
                select(TripModel).where(TripModel.id == trip_id)
            )).scalar_one()
            trip_row.current_route_id = route_id
            trip_row.itinerary_json = proposal_payload
            trip_row.state = "proposed"
            await session.commit()

        # 4. Request published itinerary as owner -> HTTP 200 OK
        app.dependency_overrides[get_current_user] = lambda: owner_user
        res_itinerary = await client.get(f"/api/v1/trips/{trip_id}/itinerary")
        assert res_itinerary.status_code == 200
        itinerary_data = res_itinerary.json()

        assert itinerary_data["trip_id"] == str(trip_id)
        assert itinerary_data["version"] == 1
        assert itinerary_data["state"] == "proposed"
        assert itinerary_data["status"] == "ready_for_approval"
        assert itinerary_data["route_id"] == str(route_id)

        # Verify GeoJSON route map features
        map_geojson = itinerary_data["map_geojson"]
        assert map_geojson is not None
        assert map_geojson["type"] == "FeatureCollection"
        features = map_geojson["features"]
        assert len(features) >= 2  # stops and legs
        stop_names = [f["properties"].get("name") for f in features if f["properties"].get("feature_type") == "stop"]
        assert "Bengaluru" in stop_names
        assert "Mysuru Palace" in stop_names

        # Verify AC-3: HOURS_ASSUMED label preserved
        stops = itinerary_data["schedule"]["stops"]
        mysuru_stop = next(s for s in stops if s["name"] == "Mysuru Palace")
        assert mysuru_stop["is_hours_assumed"] is True
        assert mysuru_stop["hours_status"] == "HOURS_ASSUMED"

        # Verify AC-7: Zero-coercion guarantee on unknown budget items
        budget_lines = itinerary_data["budget_lines"]
        toll_line = next(b for b in budget_lines if b["category"] == "tolls_and_fees")
        assert toll_line["amount"] is None
        assert "Toll rates unverified" in toll_line["unknown_reason"]

        # Verify AC-11: Candidate provenance and uncertainty tag
        recs = itinerary_data["editorial_recommendations"]
        assert len(recs) == 1
        assert recs[0]["uncertainty"] == "EDITORIAL_DISCOVERY"
        assert recs[0]["name"] == "Ranganathittu Bird Sanctuary"

        # 5. Request published itinerary as foreign tenant -> HTTP 403 Forbidden
        app.dependency_overrides[get_current_user] = lambda: foreign_user
        res_forbidden = await client.get(f"/api/v1/trips/{trip_id}/itinerary")
        assert res_forbidden.status_code == 403
        assert "belongs to another tenant" in res_forbidden.json()["detail"]

        # 6. Request weather advisory as owner -> HTTP 200 OK
        app.dependency_overrides[get_current_user] = lambda: owner_user
        res_weather = await client.get(f"/api/v1/trips/{trip_id}/weather")
        assert res_weather.status_code == 200
        weather_data = res_weather.json()
        assert weather_data["trip_id"] == str(trip_id)
        assert weather_data["weather_status"] == "AVAILABLE"
        assert weather_data["attribution"] == "Weather data by Open Meteo.com"
        assert len(weather_data["stops"]) == 2

        # 7. Request weather advisory as foreign tenant -> HTTP 403 Forbidden
        app.dependency_overrides[get_current_user] = lambda: foreign_user
        res_weather_403 = await client.get(f"/api/v1/trips/{trip_id}/weather")
        assert res_weather_403.status_code == 403
        assert "belongs to another tenant" in res_weather_403.json()["detail"]

        # 8. Request GET /api/v1/trips/{trip_id} as owner -> HTTP 200 OK with route_id and status
        app.dependency_overrides[get_current_user] = lambda: owner_user
        res_trip = await client.get(f"/api/v1/trips/{trip_id}")
        assert res_trip.status_code == 200
        trip_data = res_trip.json()
        assert trip_data["id"] == str(trip_id)
        assert trip_data["current_route_id"] == str(route_id)
        assert trip_data["state"] == "proposed"
        assert trip_data["status"] == "proposed"

        # 9. Request GET /api/v1/trips/{trip_id} as foreign tenant -> HTTP 403 Forbidden
        app.dependency_overrides[get_current_user] = lambda: foreign_user
        res_trip_403 = await client.get(f"/api/v1/trips/{trip_id}")
        assert res_trip_403.status_code == 403
        assert "belongs to another tenant" in res_trip_403.json()["detail"]

        # 10. Non-existent trip -> HTTP 404 Not Found
        missing_id = uuid.uuid4()
        res_404 = await client.get(f"/api/v1/trips/{missing_id}/itinerary")
        assert res_404.status_code == 404
        assert "not found" in res_404.json()["detail"]

        res_weather_404 = await client.get(f"/api/v1/trips/{missing_id}/weather")
        assert res_weather_404.status_code == 404
        assert "not found" in res_weather_404.json()["detail"]
