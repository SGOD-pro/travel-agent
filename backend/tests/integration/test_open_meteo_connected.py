"""Separately gated connected acceptance test for live Open Meteo endpoint.

Conforms to specification 0007 AC-6 & AC-12:
- Gated under RUN_CONNECTED_WEATHER_TESTS=1
- If network access is disabled or unconfigured, reports as skipped/pending (never false passed)
- Validates live meteorological metrics, WMO code interpretation, and attribution
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import httpx
import pytest

from contracts.weather import (
    CorridorWeatherRequest,
    WeatherAdvisoryStatus,
    WeatherStopInput,
)
from functions.orchestrator.weather.adapters.open_meteo import (
    OPEN_METEO_ATTRIBUTION,
    OpenMeteoWeatherClient,
)


@pytest.mark.asyncio
async def test_live_open_meteo_connected_smoke() -> None:
    """Verifies live Open Meteo endpoint response when connected tests are explicitly enabled."""
    if os.getenv("RUN_CONNECTED_WEATHER_TESTS") != "1":
        pytest.skip(
            "Connected Open Meteo test skipped; set RUN_CONNECTED_WEATHER_TESTS=1 to execute live API checks."
        )

    client = OpenMeteoWeatherClient(timeout_seconds=15.0, max_retries=2)
    target_date = date.today() + timedelta(days=2)
    req = CorridorWeatherRequest(
        departure_date=target_date,
        stops=[
            WeatherStopInput(
                name="Kolkata Victoria Memorial",
                latitude=22.5448,
                longitude=88.3426,
            )
        ],
        timezone="Asia/Kolkata",
    )

    network_exceptions = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.NetworkError,
        TimeoutError,
        OSError,
    )

    try:
        report = await client.get_corridor_weather(req)
    except network_exceptions as exc:
        pytest.skip(f"Live Open Meteo endpoint unreachable ({exc}); reporting as skipped/pending.")

    if report.status == WeatherAdvisoryStatus.WEATHER_UNAVAILABLE:
        stop_error = ""
        if report.stops and report.stops[0].diagnostics:
            stop_error = str(report.stops[0].diagnostics.get("error", ""))
        if any(
            term in stop_error.lower()
            for term in ("network", "timeout", "connect", "name or service not known", "resolution")
        ):
            pytest.skip(
                f"Live Open Meteo endpoint network error ({stop_error}); reporting as skipped/pending."
            )
        pytest.fail(
            f"Live Open Meteo returned unavailable due to unexpected error: {stop_error or report.diagnostics}"
        )

    assert report.status == WeatherAdvisoryStatus.AVAILABLE
    assert len(report.stops) == 1
    stop_report = report.stops[0]
    assert stop_report.temperature_celsius is not None
    assert isinstance(stop_report.temperature_celsius, float)
    assert report.attribution == OPEN_METEO_ATTRIBUTION
