"""Unit test for API response timing middleware."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from functions.api.app import app


def test_api_timing_middleware_header_and_log(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="api.timing"):
        response = client.get("/health")

    assert response.status_code == 200
    assert "X-Process-Time-Ms" in response.headers

    # Validate header value is a valid positive float string
    duration_val = float(response.headers["X-Process-Time-Ms"])
    assert duration_val >= 0.0

    # Validate that execution time was logged
    log_messages = [record.message for record in caplog.records if record.name == "api.timing"]
    assert any("completed in" in msg and "/health" in msg for msg in log_messages)


def test_cors_middleware_exposes_timing_header() -> None:
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200
    assert "access-control-expose-headers" in response.headers
    exposed = [h.strip() for h in response.headers["access-control-expose-headers"].split(",")]
    assert "X-Process-Time-Ms" in exposed
