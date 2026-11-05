"""Phase 8 Route Graph & API vertical slice End-to-End tests.

Verifies:
1. Route-only request submission through the public API.
2. Job acceptance (202 Accepted) and completion.
3. Inspection of route legs, stops, and returned OpenStreetMap/OSRM map GeoJSON.
4. Multimodal parking, walking, and return-home behavior.
5. Changed-road condition rerouting and itinerary recalculation according to OpenStreetMap/OSRM.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from config.db import get_session
from functions.api.app import app
from functions.api.service import build_api_composition_workflow
from middleware.auth import AuthenticatedUser, get_current_user
from middleware.rate_limit import public_request_rate_limiter


async def _override_api_dependencies(session: Any) -> None:
    async def override_session():
        yield session

    async def override_user() -> AuthenticatedUser:
        return AuthenticatedUser(id=uuid.UUID(int=1), email="traveler@example.com")

    async def no_limit() -> None:
        return None

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[public_request_rate_limiter] = no_limit


@pytest.mark.asyncio
async def test_route_only_public_api_e2e_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """1. Submit a route-only request through the public API.
    2. Confirm job acceptance and completion.
    3. Inspect route legs and GeoJSON.
    4. Verify parking, walking, and return-home behavior.
    """
    accepted_jobs: list[dict[str, Any]] = []
    job_id = uuid.uuid4()

    async def mock_accept_composition_job(**kwargs: Any) -> dict[str, Any]:
        accepted_jobs.append(kwargs)
        return {
            "job_id": str(job_id),
            "status": "QUEUED",
            "status_url": f"/api/v1/jobs/{job_id}",
        }

    monkeypatch.setattr(
        "functions.api.routes.optimization.accept_composition_job",
        mock_accept_composition_job,
    )
    await _override_api_dependencies(session=object())

    route_payload = {
        "products": ["route"],
        "route_request": {
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Mysuru Palace",
                    "lat": 12.3051,
                    "lng": 76.6551,
                    "stay_minutes": 120,
                    "requires_parking": True,
                    "parking_name": "Mysuru Palace South Parking Hub",
                    "parking_lat": 12.3025,
                    "parking_lng": 76.6560,
                }
            ],
            "start_datetime": "2026-11-06T09:00:00+05:30",
            "return_to_origin": True,
            "vehicle_mileage_kmpl": 15.0,
            "fuel_price_per_liter": 102.5,
        },
    }

    try:
        # Step 1: Submit route-only request through the public API
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": f"route-e2e-{job_id}"},
                json=route_payload,
            )

        # Step 2: Confirm job acceptance
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["status"] == "QUEUED"
        assert data["status_url"] == f"/api/v1/jobs/{job_id}"
        assert len(accepted_jobs) == 1
        assert accepted_jobs[0]["payload"]["products"] == ["route"]

        # Step 3: Execute composition workflow and verify completion
        workflow = build_api_composition_workflow()
        workflow_result = await workflow.ainvoke({"request": accepted_jobs[0]["payload"]})

        product_outputs = workflow_result.get("product_outputs", {})
        assert "route" in product_outputs
        route_data = product_outputs["route"]
        assert route_data["validation_status"] == "VALID"
        optimized_route = route_data["optimized_route"]

        # Step 4: Verify parking, walking, and return-home behavior
        stops = optimized_route.stops
        legs = optimized_route.legs

        # Stops inspection
        stop_names = [s.name for s in stops]
        assert stop_names == [
            "Bengaluru",
            "Mysuru Palace South Parking Hub",
            "Mysuru Palace",
            "Mysuru Palace South Parking Hub",
            "Bengaluru",
        ]
        assert [s.stop_type for s in stops] == [
            "ORIGIN",
            "PARKING_HUB",
            "DESTINATION",
            "PARKING_HUB",
            "ORIGIN",
        ]

        # Legs inspection
        assert len(legs) == 4
        # Leg 0: Drive to Parking Hub
        assert legs[0].from_stop == "Bengaluru"
        assert legs[0].to_stop == "Mysuru Palace South Parking Hub"
        assert legs[0].mode == "car"
        assert legs[0].distance_km > 130.0
        assert legs[0].duration_minutes > 90
        assert legs[0].fuel_cost_inr > 0.0

        # Leg 1: Walk to Palace (Zero fuel, Zero toll)
        assert legs[1].from_stop == "Mysuru Palace South Parking Hub"
        assert legs[1].to_stop == "Mysuru Palace"
        assert legs[1].mode == "walk"
        assert legs[1].fuel_cost_inr == 0.0
        assert legs[1].toll_cost_inr == 0.0

        # Leg 2: Walk back to retrieve vehicle (Zero fuel, Zero toll)
        assert legs[2].from_stop == "Mysuru Palace"
        assert legs[2].to_stop == "Mysuru Palace South Parking Hub"
        assert legs[2].mode == "walk"
        assert legs[2].fuel_cost_inr == 0.0
        assert legs[2].toll_cost_inr == 0.0

        # Leg 3: Drive return to Bengaluru
        assert legs[3].from_stop == "Mysuru Palace South Parking Hub"
        assert legs[3].to_stop == "Bengaluru"
        assert legs[3].mode == "car"
        assert legs[3].fuel_cost_inr > 0.0

        # Verify return-home invariant
        assert optimized_route.return_to_origin is True
        assert stops[-1].name == "Bengaluru"

        # Step 5: Inspect returned map GeoJSON
        geojson = optimized_route.geometry_geojson
        assert geojson is not None
        assert geojson["type"] == "Feature"
        assert geojson["properties"]["provider"] == "osrm"
        assert geojson["properties"]["routing_basis"] == "openstreetmap"
        assert geojson["geometry"]["type"] == "LineString"
        coords = geojson["geometry"]["coordinates"]
        assert len(coords) > 100
        # Validate [longitude, latitude] GeoJSON format
        for pt in coords[:10]:
            assert len(pt) == 2
            assert 70.0 < pt[0] < 85.0
            assert 10.0 < pt[1] < 20.0

    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_changed_road_condition_reroute_recalculates_itinerary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4. Change one road/route condition.
    5. Confirm the itinerary is recalculated according to OpenStreetMap/OSRM not googlemaps.
    """
    accepted_jobs: list[dict[str, Any]] = []

    async def mock_accept_composition_job(**kwargs: Any) -> dict[str, Any]:
        accepted_jobs.append(kwargs)
        return {
            "job_id": str(uuid.uuid4()),
            "status": "QUEUED",
            "status_url": "/api/v1/jobs/test",
        }

    monkeypatch.setattr(
        "functions.api.routes.optimization.accept_composition_job",
        mock_accept_composition_job,
    )
    await _override_api_dependencies(session=object())

    # Baseline direct route (NH 275 expressway)
    direct_payload = {
        "products": ["route"],
        "route_request": {
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Mysuru Palace",
                    "lat": 12.3051,
                    "lng": 76.6551,
                    "stay_minutes": 120,
                }
            ],
            "start_datetime": "2026-11-06T09:00:00+05:30",
            "return_to_origin": False,
        },
    }

    # Changed road condition: highway closed, detour via Kanakapura (12.5463, 77.4208)
    rerouted_payload = {
        "products": ["route"],
        "route_request": {
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Mysuru Palace",
                    "lat": 12.3051,
                    "lng": 76.6551,
                    "stay_minutes": 120,
                }
            ],
            "start_datetime": "2026-11-06T09:00:00+05:30",
            "return_to_origin": False,
            "road_condition": "highway_closure",
            "detour_waypoints": [
                {
                    "name": "Kanakapura Detour",
                    "lat": 12.5463,
                    "lng": 77.4208,
                }
            ],
        },
    }

    try:
        # 1. Run baseline route through API composition workflow
        workflow = build_api_composition_workflow()
        direct_res = await workflow.ainvoke({"request": direct_payload})
        direct_route = direct_res["product_outputs"]["route"]["optimized_route"]

        # 2. Run changed-road route through API composition workflow
        rerouted_res = await workflow.ainvoke({"request": rerouted_payload})
        rerouted_route = rerouted_res["product_outputs"]["route"]["optimized_route"]

        # 3. Confirm recalculation according to OpenStreetMap/OSRM
        assert direct_route.routing_provider == "osrm"
        assert rerouted_route.routing_provider == "osrm"

        # The detour route visits the Kanakapura detour waypoint
        rerouted_stop_names = [s.name for s in rerouted_route.stops]
        assert "Kanakapura Detour" in rerouted_stop_names

        # Detour route has strictly higher distance and duration
        direct_distance = sum(leg.distance_km for leg in direct_route.legs)
        rerouted_distance = sum(leg.distance_km for leg in rerouted_route.legs)
        assert rerouted_distance > direct_distance + 20.0  # > 20 km detour difference

        assert rerouted_route.total_travel_minutes > direct_route.total_travel_minutes + 25

        # GeoJSON LineString coordinates differ and reflect OpenStreetMap detour path
        direct_coords = direct_route.geometry_geojson["geometry"]["coordinates"]
        rerouted_coords = rerouted_route.geometry_geojson["geometry"]["coordinates"]
        assert direct_coords != rerouted_coords
        assert rerouted_route.geometry_geojson["properties"]["provider"] == "osrm"
        assert rerouted_route.geometry_geojson["properties"]["routing_basis"] == "openstreetmap"

    finally:
        app.dependency_overrides.clear()
