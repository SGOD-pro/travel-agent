"""Integration regression test suite for standalone route replan concurrency.

Conforms strictly to specification 0006 AC-6 and prompt requirements:
1. Competing branches are NOT allowed. A route version may have at most one child replan version.
2. Concurrent replans against the same parent route conflict atomically at acceptance and persistence.
3. Attempting to replan a stale parent route after a child has been generated returns HTTP 409 Conflict.
4. Database partial unique index uq_routes_parent_route_id enforces single-child replans at persistence.
"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import shapely.geometry
from geoalchemy2.shape import from_shape
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.config import settings
from config.db import get_session
from contracts.money import Currency
from contracts.routing import (
    OptimizedRoute,
    Point,
    RouteObjective,
    RouteStatus,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
)
from functions.api.app import app
from functions.orchestrator.execution.models import ExecutionModel  # noqa: F401
from functions.orchestrator.places.models import CanonicalPlaceModel  # noqa: F401
from functions.orchestrator.routing.models import RouteModel, RouteStopModel
from functions.orchestrator.routing.repository import (
    RouteRepository,
    RouteVersionConflictError,
)
from middleware.auth import AuthenticatedUser, get_current_user

# Resilient DNS caching for PostgreSQL connectivity
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        return _orig_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _resilient_getaddrinfo


@pytest.fixture
async def session_factory() -> Any:
    assert settings.DATABASE_URL, "DATABASE_URL must be configured"
    url = settings.DATABASE_URL
    connect_args: dict[str, Any] = {
        "command_timeout": 60,
        "timeout": 30,
        "statement_cache_size": 0,
    }
    if (
        "sslmode=require" in url
        or "ssl=require" in url
        or "aivencloud.com" in url
        or settings.DB_SSL
    ):
        connect_args["ssl"] = "require"
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]+", "", url)
        if "?" not in url and "&" in url:
            url = url.replace("&", "?", 1)

    engine = create_async_engine(
        url,
        connect_args=connect_args,
        poolclass=NullPool,
    )
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_standalone_route_replan_concurrency_regression(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that competing replans against a standalone route conflict atomically on PostgreSQL."""
    owner_id = uuid.uuid4()
    now = datetime.now(UTC)
    route_id = uuid.uuid4()
    stop1_id = uuid.uuid4()
    stop2_id = uuid.uuid4()

    # 1. Seed initial standalone route v1 in PostgreSQL (trip_id is NULL)
    async with session_factory() as session:
        pt1 = from_shape(shapely.geometry.Point(88.3639, 22.5726), srid=4326)
        pt2 = from_shape(shapely.geometry.Point(88.3426, 22.5448), srid=4326)

        stop1 = RouteStopModel(
            id=stop1_id,
            route_id=route_id,
            place_id=None,
            stop_order=1,
            name="Origin Point",
            location=pt1,
            stop_type="ORIGIN",
            is_must_visit=True,
            is_completed=False,
            is_skipped=False,
            dwell_seconds=0,
            wait_seconds=0,
        )
        stop2 = RouteStopModel(
            id=stop2_id,
            route_id=route_id,
            place_id=None,
            stop_order=2,
            name="Destination Point",
            location=pt2,
            stop_type="DESTINATION",
            is_must_visit=True,
            is_completed=False,
            is_skipped=False,
            dwell_seconds=1800,
            wait_seconds=0,
        )
        route_v1 = RouteModel(
            id=route_id,
            owner_id=owner_id,
            trip_id=None,  # Standalone route
            execution_id=None,
            parent_route_id=None,
            route_version=1,
            status="FEASIBLE",
            solver_status="OPTIMAL",
            objective="FASTEST",
            departure_at=now,
            total_distance_m=12500,
            total_travel_seconds=1200,
            total_dwell_seconds=1800,
            total_wait_seconds=0,
            total_elapsed_seconds=3000,
            driving_distance_m=12500,
            walking_distance_m=0,
            fuel_amount=None,
            fuel_currency="INR",
            warnings=[],
            assumptions=[],
            stops=[stop1, stop2],
            legs=[],
            restrictions=[],
        )
        session.add(route_v1)
        await session.commit()

    async def override_get_session() -> Any:
        async with session_factory() as session:
            yield session

    current_user = AuthenticatedUser(id=owner_id, email="concurrency-test@swena.internal")

    async def override_get_current_user() -> AuthenticatedUser:
        return current_user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 2. Fire two concurrent replan requests against route_v1 with differing payloads
            replan_req_a = {
                "current_location": {"latitude": 22.5600, "longitude": 88.3500},
                "completed_stop_ids": [],
                "expected_previous_version": 1,
                "replan_at": (now + timedelta(minutes=10)).isoformat(),
            }
            replan_req_b = {
                "current_location": {"latitude": 22.5650, "longitude": 88.3550},
                "completed_stop_ids": [str(stop1_id)],
                "expected_previous_version": 1,
                "replan_at": (now + timedelta(minutes=15)).isoformat(),
            }

            resp_a, resp_b = await asyncio.gather(
                client.post(
                    f"/api/v1/optimization/routes/{route_id}/replans",
                    headers={"Idempotency-Key": f"conc-replan-a-{uuid.uuid4()}"},
                    json=replan_req_a,
                ),
                client.post(
                    f"/api/v1/optimization/routes/{route_id}/replans",
                    headers={"Idempotency-Key": f"conc-replan-b-{uuid.uuid4()}"},
                    json=replan_req_b,
                ),
            )

            # Exactly one request must succeed with 202 Accepted, and one must conflict with 409
            status_codes = sorted([resp_a.status_code, resp_b.status_code])
            assert status_codes == [202, 409], (
                f"Expected [202, 409], got resp_a={resp_a.status_code} ({resp_a.text}), "
                f"resp_b={resp_b.status_code} ({resp_b.text})"
            )

            conflict_resp = resp_a if resp_a.status_code == 409 else resp_b
            assert "competing replan" in conflict_resp.json()["detail"].lower()

            # 3. Simulate persistence of child route v2
            child_route_id = uuid.uuid4()
            async with session_factory() as session:
                route_v2 = RouteModel(
                    id=child_route_id,
                    owner_id=owner_id,
                    trip_id=None,
                    execution_id=None,
                    parent_route_id=route_id,
                    route_version=2,
                    status="FEASIBLE",
                    solver_status="OPTIMAL",
                    objective="FASTEST",
                    departure_at=now + timedelta(minutes=10),
                    total_distance_m=11000,
                    total_travel_seconds=1000,
                    total_dwell_seconds=1800,
                    total_wait_seconds=0,
                    total_elapsed_seconds=2800,
                    driving_distance_m=11000,
                    walking_distance_m=0,
                    fuel_amount=None,
                    fuel_currency="INR",
                    warnings=[],
                    assumptions=[],
                    stops=[],
                    legs=[],
                    restrictions=[],
                )
                session.add(route_v2)
                await session.commit()

            # 4. Attempting to replan route_v1 now that child v2 exists must fail with 409 Stale Version
            stale_replan_resp = await client.post(
                f"/api/v1/optimization/routes/{route_id}/replans",
                headers={"Idempotency-Key": f"stale-replan-{uuid.uuid4()}"},
                json=replan_req_a,
            )
            assert stale_replan_resp.status_code == 409
            detail = stale_replan_resp.json()["detail"]
            assert "stale version" in detail.lower()
            assert str(child_route_id) in detail

            # 5. Persistence regression: attempting to insert another route with parent_route_id == route_id
            # must be rejected atomically by PostgreSQL's uq_routes_parent_route_id constraint
            competing_route_domain = OptimizedRoute(
                id=uuid.uuid4(),
                owner_id=owner_id,
                trip_id=None,
                execution_id=None,
                route_version=2,
                status=RouteStatus.FEASIBLE,
                solver_status=SolverStatus.OPTIMAL,
                objective=RouteObjective.FASTEST,
                departure_at=now,
                total_distance_m=10000,
                total_travel_seconds=900,
                total_dwell_seconds=0,
                total_wait_seconds=0,
                total_elapsed_seconds=900,
                driving_distance_m=10000,
                walking_distance_m=0,
                fuel_currency=Currency.INR,
                stops=[
                    ScheduledStopOutput(
                        id=uuid.uuid4(),
                        stop_id=uuid.uuid4(),
                        place_id=None,
                        stop_order=1,
                        name="Competing Stop",
                        location=Point(latitude=22.56, longitude=88.35),
                        stop_type=StopType.ORIGIN,
                        is_must_visit=True,
                        is_completed=False,
                        is_skipped=False,
                        dwell_seconds=0,
                        wait_seconds=0,
                    )
                ],
                legs=[],
                restrictions=[],
                dropped_stops=[],
                warnings=[],
                assumptions=[],
                diagnostics=[],
            )

            async with session_factory() as session:
                repo = RouteRepository(session)
                with pytest.raises(RouteVersionConflictError) as exc_info:
                    await repo.save_route(
                        competing_route_domain,
                        owner_id=owner_id,
                        trip_id=None,
                        parent_route_id=route_id,  # route_id already has child_route_id
                        route_version=2,
                    )
                assert f"Route {route_id} has already been replanned into a child route" in str(
                    exc_info.value
                )

    finally:
        app.dependency_overrides.clear()
