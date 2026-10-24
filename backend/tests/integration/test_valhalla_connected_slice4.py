"""Connected integration test suite for Valhalla routing service (AC-3, AC-7).

Runs live smoke tests against an external Valhalla instance when VALHALLA_ENDPOINT_URL is configured.
Skips cleanly when unconfigured or disabled, preserving offline development workflow.
"""

from __future__ import annotations

import pytest

from config.config import settings
from contracts.routing import Point
from contracts.trips import TransportMode
from functions.route_optimizer.valhalla import ValhallaClient


@pytest.fixture
def valhalla_endpoint() -> str | None:
    return settings.VALHALLA_ENDPOINT_URL


@pytest.mark.asyncio
async def test_valhalla_connected_smoke_gate(valhalla_endpoint: str | None) -> None:
    """Smoke test against live Valhalla service if configured; skips cleanly otherwise."""
    if not valhalla_endpoint:
        pytest.skip("VALHALLA_ENDPOINT_URL is not configured; skipping live connected smoke test")

    client = ValhallaClient(valhalla_endpoint)
    is_healthy = await client.check_health()
    if not is_healthy:
        pytest.skip(f"Valhalla service at {valhalla_endpoint} is currently unreachable")

    # 1. Test live /sources_to_targets matrix
    p1 = Point(latitude=22.5726, longitude=88.3639)  # Kolkata Central
    p2 = Point(latitude=22.5800, longitude=88.3700)  # Nearby location
    matrix = await client.sources_to_targets([p1, p2], [p1, p2], TransportMode.CAR_PETROL)

    assert len(matrix.distances_m) == 2
    assert len(matrix.durations_s) == 2
    assert matrix.distances_m[0][0] == 0
    assert matrix.distances_m[0][1] is not None
    assert matrix.distances_m[0][1] > 0

    # 2. Test live /route with turn-by-turn maneuvers and geometries
    route_res = await client.route([p1, p2], TransportMode.CAR_PETROL)
    assert route_res.total_distance_m > 0
    assert route_res.total_travel_seconds > 0
    assert len(route_res.legs) == 1
    assert len(route_res.legs[0].geometry["coordinates"]) >= 2
