"""Unit tests for the benchmark evaluation suite and API route."""

import pytest
from httpx import ASGITransport, AsyncClient

from travel.benchmarks.runner import BenchmarkRunner
from travel.runtime.fastapi.app import app


def test_benchmark_runner_executes_all_50_cases() -> None:
    runner = BenchmarkRunner()
    report = runner.run_all()
    assert report["total_cases"] == 50
    assert report["passed_cases"] == 50
    assert report["failed_cases"] == 0
    assert report["pass_rate_percent"] == 100.0
    assert len(report["results"]) == 50


@pytest.mark.asyncio
async def test_benchmarks_api_route() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/benchmarks")
        assert response.status_code == 200
        data = response.json()
        assert data["total_cases"] == 50
        assert data["passed_cases"] == 50
        assert data["pass_rate_percent"] == 100.0
