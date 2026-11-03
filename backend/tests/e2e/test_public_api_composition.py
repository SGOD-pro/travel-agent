"""Contract-level end-to-end coverage for public product selection boundaries."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from config.db import get_session
from functions.api import service
from functions.api.app import app
from middleware.auth import AuthenticatedUser, get_current_user
from middleware.rate_limit import public_request_rate_limiter


class FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    def __init__(self, *values: Any) -> None:
        self._values = iter(values)

    async def execute(self, statement: Any) -> FakeResult:
        return FakeResult(next(self._values))


async def _override_api_dependencies(session: Any) -> None:
    async def override_session():
        yield session

    async def override_user() -> AuthenticatedUser:
        return AuthenticatedUser(id=uuid.UUID(int=1), email="test@example.com")

    async def no_limit() -> None:
        return None

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[public_request_rate_limiter] = no_limit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("products", "expected"),
    [
        (["media"], ["media"]),
        (["travel"], ["travel"]),
        (["route"], ["route"]),
        (["media", "travel"], ["media", "travel"]),
        (["media", "route"], ["media", "route"]),
        (["travel", "route"], ["travel", "route"]),
        (["media", "travel", "route"], ["media", "travel", "route"]),
    ],
)
async def test_composition_runs_only_explicitly_selected_products(
    monkeypatch: pytest.MonkeyPatch,
    products: list[str],
    expected: list[str],
) -> None:
    called: list[str] = []

    async def record_product(
        name: str,
        state: service.CompositionState,
    ) -> dict[str, Any]:
        called.append(name)
        outputs = dict(state.get("product_outputs", {}))
        outputs[name] = {"completed": True}
        return {"product_outputs": outputs}

    async def media(state: service.CompositionState) -> dict[str, Any]:
        return await record_product("media", state)

    async def travel(state: service.CompositionState) -> dict[str, Any]:
        return await record_product("travel", state)

    async def route(state: service.CompositionState) -> dict[str, Any]:
        return await record_product("route", state)

    monkeypatch.setattr(service, "_run_media_product", media)
    monkeypatch.setattr(service, "_run_travel_product", travel)
    monkeypatch.setattr(service, "_run_route_product", route)
    graph = service.build_api_composition_workflow()

    result = await graph.ainvoke({"request": {"products": products}})

    assert called == expected
    assert list(result["product_outputs"]) == expected


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"products": ["media"]}, "media_url"),
        ({"products": ["travel"]}, "brief"),
        ({"products": ["route"]}, "route_request or a travel brief"),
        (
            {"products": ["route", "media"], "media_url": "https://example.com"},
            "ordered media, travel, then route",
        ),
    ],
)
def test_composition_rejects_missing_inputs_and_invalid_order(
    payload: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        service.validate_composition_request(payload)
    assert message in str(exc_info.value.detail)


def test_public_api_routes_are_registered() -> None:
    paths = app.openapi()["paths"]

    assert "post" in paths["/api/v1/trips"]
    assert "post" in paths["/api/v1/trips/{trip_id}/plans"]
    assert "post" in paths["/api/v1/discovery/media"]
    assert "post" in paths["/api/v1/optimization/routes"]
    assert "get" in paths["/api/v1/jobs/{job_id}"]
    assert "post" in paths["/api/v1/jobs/{job_id}/cancel"]
    assert "get" in paths["/api/v1/jobs/{job_id}/events"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "products",
    [
        ["media", "travel"],
        ["media", "route"],
        ["travel", "route"],
        ["media", "travel", "route"],
    ],
)
async def test_composition_passes_verified_media_places_only_to_requested_products(
    monkeypatch: pytest.MonkeyPatch,
    products: list[str],
) -> None:
    import functions.orchestrator.graphs.trip_planner.graph as planner_module
    import workflows.media_locations.graph as media_module
    import workflows.route_optimizer.graph as route_module

    media_place = {
        "primary_name": "Abbey Falls",
        "locality": "Karnataka",
        "latitude": 12.4542,
        "longitude": 75.7196,
        "confidence": 0.91,
        "verification_status": "verified",
    }
    second_media_place = {
        "primary_name": "Coorg",
        "locality": "Karnataka",
        "latitude": 12.4244,
        "longitude": 75.7382,
        "confidence": 0.88,
        "verification_status": "verified",
    }
    calls: list[str] = []
    travel_briefs: list[dict[str, Any]] = []
    travel_endpoints: list[int | None] = []
    route_requests: list[dict[str, Any]] = []

    class FakeMediaGraph:
        async def ainvoke(self, _: dict[str, Any]) -> dict[str, Any]:
            calls.append("media")
            return {"verified_places": [media_place, second_media_place]}

    class FakeRouteGraph:
        async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
            calls.append("route")
            route_requests.append(state["request"])
            return {"validation_status": "VALID", "optimized_route": {"completed": True}}

    async def fake_travel_workflow(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("travel")
        travel_briefs.append(state["brief"])
        travel_endpoints.append(state.get("end_node_index"))
        return {"status": "completed", "proposal": {"completed": True}}

    monkeypatch.setattr(media_module, "create_media_location_graph", FakeMediaGraph)
    monkeypatch.setattr(route_module, "create_route_optimizer_graph", FakeRouteGraph)
    monkeypatch.setattr(
        planner_module,
        "trip_planning_workflow",
        SimpleNamespace(ainvoke=fake_travel_workflow),
    )

    brief = {
        "origin_name": "Bengaluru",
        "origin_lat": 12.9716,
        "origin_lng": 77.5946,
        "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
    }
    route_request = {
        "origin_name": "Bengaluru",
        "origin_lat": 12.9716,
        "origin_lng": 77.5946,
        "destinations": [{"name": "Mysuru", "lat": 12.2958, "lng": 76.6394}],
        "start_datetime": "2026-11-01T09:00:00+05:30",
    }
    request = {
        "products": products,
        "media_url": "https://media.example/clip.mp4",
        "brief": brief,
        "route_request": route_request,
    }

    result = await service.build_api_composition_workflow().ainvoke({"request": request})

    assert calls == products
    assert list(result["product_outputs"]) == products
    if "travel" in products and "media" in products:
        assert travel_briefs[0]["destinations"][0]["name"] == "Abbey Falls"
        assert travel_briefs[0]["destinations"][-1]["name"] == "Coorg"
        assert len(travel_briefs[0]["destinations"]) == 2
        assert travel_endpoints[0] == 2
        assert result["product_outputs"]["travel"]["destination_source"] == "verified_media"
    if "route" in products and "media" in products:
        assert route_requests[0]["destinations"][0]["name"] == "Abbey Falls"
        assert route_requests[0]["destinations"][-1]["name"] == "Coorg"
    if "route" in products and "media" not in products:
        assert route_requests[0]["destinations"] == route_request["destinations"]


def test_unverified_media_does_not_replace_user_entered_destinations() -> None:
    brief = {
        "destinations": [
            {"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394},
        ],
    }

    merged = service._merge_verified_places(
        brief,
        {"media": {"verified_places": []}},
    )

    assert merged == brief


def test_verified_media_places_replace_manual_destinations_in_returned_order() -> None:
    brief = {
        "destinations": [
            {"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394},
        ],
    }

    merged = service._merge_verified_places(
        brief,
        {
            "media": {
                "verified_places": [
                    {
                        "primary_name": "Abbey Falls",
                        "latitude": 12.4542,
                        "longitude": 75.7196,
                        "verification_status": "verified",
                    },
                    {
                        "primary_name": "Coorg",
                        "latitude": 12.4244,
                        "longitude": 75.7382,
                        "verification_status": "verified",
                    },
                ]
            }
        },
    )

    assert [place["name"] for place in merged["destinations"]] == ["Abbey Falls", "Coorg"]


@pytest.mark.asyncio
async def test_media_composition_falls_back_to_entered_destinations_when_none_are_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.orchestrator.graphs.trip_planner.graph as planner_module
    import workflows.media_locations.graph as media_module

    seen_briefs: list[dict[str, Any]] = []
    seen_endpoints: list[int | None] = []

    class FakeMediaGraph:
        async def ainvoke(self, _: dict[str, Any]) -> dict[str, Any]:
            return {"verified_places": []}

    async def fake_travel_workflow(state: dict[str, Any]) -> dict[str, Any]:
        seen_briefs.append(state["brief"])
        seen_endpoints.append(state.get("end_node_index"))
        return {"status": "completed", "proposal": {"completed": True}}

    monkeypatch.setattr(media_module, "create_media_location_graph", FakeMediaGraph)
    monkeypatch.setattr(
        planner_module,
        "trip_planning_workflow",
        SimpleNamespace(ainvoke=fake_travel_workflow),
    )

    brief = {
        "origin_name": "Bengaluru",
        "origin_lat": 12.9716,
        "origin_lng": 77.5946,
        "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
    }

    result = await service.build_api_composition_workflow().ainvoke(
        {
            "request": {
                "products": ["media", "travel"],
                "media_url": "https://media.example/clip.mp4",
                "brief": brief,
            }
        }
    )

    assert seen_briefs[0]["destinations"] == brief["destinations"]
    assert seen_endpoints[0] is None
    assert result["product_outputs"]["travel"]["destination_source"] == "trip_brief"


@pytest.mark.asyncio
async def test_media_and_route_api_accept_only_selected_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted: list[dict[str, Any]] = []
    job_id = uuid.uuid4()

    async def accept(**kwargs: Any) -> dict[str, Any]:
        accepted.append(kwargs)
        return {
            "job_id": str(job_id),
            "status": "QUEUED",
            "status_url": f"/api/v1/jobs/{job_id}",
        }

    monkeypatch.setattr("functions.api.routes.discovery.accept_composition_job", accept)
    monkeypatch.setattr("functions.api.routes.optimization.accept_composition_job", accept)
    await _override_api_dependencies(session=object())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            media = await client.post(
                "/api/v1/discovery/media",
                headers={"Idempotency-Key": "media-only"},
                json={
                    "media_url": "https://www.instagram.com/reel/example/",
                    "products": ["media"],
                },
            )
            route = await client.post(
                "/api/v1/optimization/routes",
                headers={"Idempotency-Key": "route-only"},
                json={
                    "products": ["route"],
                    "route_request": {
                        "origin_name": "Bengaluru",
                        "origin_lat": 12.9716,
                        "origin_lng": 77.5946,
                        "destinations": [
                            {
                                "name": "Mysuru",
                                "lat": 12.2958,
                                "lng": 76.6394,
                                "stay_minutes": 120,
                            }
                        ],
                        "start_datetime": "2026-11-01T09:00:00+05:30",
                    },
                },
            )
        assert media.status_code == 202
        assert route.status_code == 202
        assert accepted[0]["payload"]["products"] == ["media"]
        assert accepted[1]["payload"]["products"] == ["route"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_trip_plan_acceptance_carries_owned_brief_and_requested_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = uuid.UUID(int=1)
    trip_id = uuid.uuid4()
    brief = {
        "origin_name": "Bengaluru",
        "origin_lat": 12.9716,
        "origin_lng": 77.5946,
        "destinations": [
            {
                "name": "Mysuru",
                "latitude": 12.2958,
                "longitude": 76.6394,
                "stay_days": 1,
            }
        ],
        "start_date": "2026-11-01",
        "end_date": "2026-11-02",
    }
    trip = SimpleNamespace(
        id=trip_id,
        owner_id=owner_id,
        current_version=1,
    )
    version = SimpleNamespace(brief_json=brief)
    accepted: list[dict[str, Any]] = []
    job_id = uuid.uuid4()

    async def accept(**kwargs: Any) -> dict[str, Any]:
        accepted.append(kwargs)
        return {
            "job_id": str(job_id),
            "status": "QUEUED",
            "status_url": f"/api/v1/jobs/{job_id}",
        }

    monkeypatch.setattr("functions.api.routes.trips.accept_composition_job", accept)
    await _override_api_dependencies(FakeSession(trip, version))
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/trips/{trip_id}/plans",
                headers={"Idempotency-Key": "travel-only"},
                json={"products": ["travel"]},
            )
        assert response.status_code == 202
        assert accepted[0]["payload"]["products"] == ["travel"]
        assert accepted[0]["payload"]["brief"] == brief
        assert accepted[0]["owner_id"] == owner_id
    finally:
        app.dependency_overrides.clear()
