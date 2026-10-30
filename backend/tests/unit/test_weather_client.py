"""Unit tests for weather advisory contracts, Open Meteo adapter, fixtures, and factory."""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from config.config import Settings
from contracts.weather import (
    CommercialLicenseStatus,
    CorridorWeatherAdvisoryReport,
    CorridorWeatherRequest,
    MonsoonRisk,
    StopWeatherCondition,
    StopWeatherReport,
    WeatherAdvisoryStatus,
    WeatherStopInput,
)
from functions.orchestrator.weather.adapters.fixture_client import FixtureWeatherClient
from functions.orchestrator.weather.adapters.open_meteo import (
    OPEN_METEO_ATTRIBUTION,
    OpenMeteoWeatherClient,
    evaluate_ghat_caution,
    evaluate_monsoon_risk,
    map_wmo_code_to_condition,
)
from functions.orchestrator.weather.factory import create_weather_client
from functions.orchestrator.weather.port import WeatherClientPort


class TestWeatherContracts:
    """Verifies typed schema validation and boundary constraints for weather contracts."""

    def test_weather_stop_input_valid(self) -> None:
        stop = WeatherStopInput(
            name="Victoria Memorial",
            latitude=22.5448,
            longitude=88.3426,
            is_ghat_section=False,
        )
        assert stop.name == "Victoria Memorial"
        assert stop.latitude == 22.5448
        assert stop.longitude == 88.3426
        assert stop.is_ghat_section is False

    def test_weather_stop_input_coordinate_bounds(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            WeatherStopInput(name="Invalid Lat", latitude=95.0, longitude=88.0)
        assert "latitude" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            WeatherStopInput(name="Invalid Lon", latitude=22.0, longitude=190.0)
        assert "longitude" in str(exc_info.value)

    def test_corridor_weather_request_requires_stops(self) -> None:
        with pytest.raises(ValidationError):
            CorridorWeatherRequest(
                departure_date=date(2026, 11, 1),
                stops=[],  # min_length=1 violated
            )

    def test_stop_weather_report_schema(self) -> None:
        report = StopWeatherReport(
            stop_name="Kolaghat Plaza",
            latitude=22.4285,
            longitude=87.8712,
            temperature_celsius=30.5,
            apparent_temperature_celsius=34.0,
            condition=StopWeatherCondition.SUNNY,
            humidity_percent=70,
            precipitation_probability=10,
            precipitation_mm=0.0,
            weather_code=0,
            monsoon_risk=MonsoonRisk.LOW,
        )
        assert report.stop_name == "Kolaghat Plaza"
        assert report.condition == StopWeatherCondition.SUNNY
        assert report.monsoon_risk == MonsoonRisk.LOW
        assert report.is_day is True

    def test_corridor_weather_advisory_defaults(self) -> None:
        report = CorridorWeatherAdvisoryReport(
            status=WeatherAdvisoryStatus.AVAILABLE,
            stops=[],
        )
        assert report.attribution == "Weather data by Open Meteo.com"
        assert report.provider == "open_meteo"
        assert report.forecast_horizon_days == 16
        assert report.is_commercial_licensed is False


class TestOpenMeteoLogicAndAdapter:
    """Verifies Open Meteo weather code mapping, risk assessment, and HTTP client behavior."""

    def test_map_wmo_code_to_condition(self) -> None:
        assert map_wmo_code_to_condition(0) == StopWeatherCondition.SUNNY
        assert map_wmo_code_to_condition(1) == StopWeatherCondition.SUNNY
        assert map_wmo_code_to_condition(2) == StopWeatherCondition.CLOUDY
        assert map_wmo_code_to_condition(3) == StopWeatherCondition.CLOUDY
        assert map_wmo_code_to_condition(45) == StopWeatherCondition.FOGGY
        assert map_wmo_code_to_condition(48) == StopWeatherCondition.FOGGY
        assert map_wmo_code_to_condition(61) == StopWeatherCondition.RAINY
        assert map_wmo_code_to_condition(65) == StopWeatherCondition.RAINY
        assert map_wmo_code_to_condition(95) == StopWeatherCondition.RAINY
        assert map_wmo_code_to_condition(None) == StopWeatherCondition.UNKNOWN
        assert map_wmo_code_to_condition(999) == StopWeatherCondition.UNKNOWN

    def test_evaluate_monsoon_risk(self) -> None:
        assert evaluate_monsoon_risk(0.0, 10, 0) == MonsoonRisk.LOW
        assert evaluate_monsoon_risk(12.0, 50, 61) == MonsoonRisk.MODERATE
        assert evaluate_monsoon_risk(28.0, 80, 65) == MonsoonRisk.SEVERE
        assert evaluate_monsoon_risk(5.0, 75, 95) == MonsoonRisk.SEVERE

    def test_evaluate_ghat_caution(self) -> None:
        stop_ghat = WeatherStopInput(
            name="Rohini Ghat Pass", latitude=26.85, longitude=88.28, is_ghat_section=True
        )
        stop_plain = WeatherStopInput(name="Plain Plaza", latitude=22.5, longitude=88.3)

        fog_caution = evaluate_ghat_caution(stop_ghat, StopWeatherCondition.FOGGY, MonsoonRisk.LOW)
        assert fog_caution is not None
        assert "fog" in fog_caution.lower()

        rain_caution = evaluate_ghat_caution(
            stop_ghat, StopWeatherCondition.RAINY, MonsoonRisk.SEVERE
        )
        assert rain_caution is not None
        assert "severe rainfall" in rain_caution.lower()

        assert (
            evaluate_ghat_caution(stop_plain, StopWeatherCondition.SUNNY, MonsoonRisk.LOW) is None
        )

    async def test_forecast_horizon_policy_distant_date(self) -> None:
        client = OpenMeteoWeatherClient()
        distant_date = date.today() + timedelta(days=25)
        stop = WeatherStopInput(name="Distant Stop", latitude=22.5, longitude=88.3)
        req = CorridorWeatherRequest(departure_date=distant_date, stops=[stop])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON
        assert report.overall_caution is not None
        assert (
            "exceeding the 16 day meteorological forecast horizon" in report.overall_caution.lower()
        )
        assert "historical averages are not presented" in report.overall_caution.lower()
        assert len(report.stops) == 1
        assert report.stops[0].condition == StopWeatherCondition.UNKNOWN

    async def test_open_meteo_client_mock_successful_response(self) -> None:
        mock_response_data = {
            "latitude": 22.57,
            "longitude": 88.36,
            "hourly": {
                "time": [f"2026-11-01T{h:02d}:00" for h in range(24)],
                "temperature_2m": [25.0 + h * 0.5 for h in range(24)],
                "apparent_temperature": [28.0 + h * 0.5 for h in range(24)],
                "relative_humidity_2m": [60 for _ in range(24)],
                "precipitation_probability": [10 for _ in range(24)],
                "precipitation": [0.0 for _ in range(24)],
                "weather_code": [0 for _ in range(24)],
                "is_day": [1 for _ in range(24)],
            },
        }

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_response_data
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        stop = WeatherStopInput(name="Kolkata Hub", latitude=22.57, longitude=88.36)
        target_date = date.today() + timedelta(days=2)
        req = CorridorWeatherRequest(departure_date=target_date, stops=[stop])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.AVAILABLE
        assert len(report.stops) == 1
        assert report.stops[0].condition == StopWeatherCondition.SUNNY
        assert report.stops[0].temperature_celsius is not None
        assert report.attribution == OPEN_METEO_ATTRIBUTION

    async def test_open_meteo_client_retries_and_degrades_on_5xx(self) -> None:
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http, max_retries=1)
        stop = WeatherStopInput(name="Test Stop", latitude=22.5, longitude=88.3)
        target_date = date.today() + timedelta(days=1)
        req = CorridorWeatherRequest(departure_date=target_date, stops=[stop])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.WEATHER_UNAVAILABLE
        assert report.overall_caution is not None
        assert "temporarily unavailable" in report.overall_caution.lower()
        assert mock_http.get.call_count == 2  # initial + 1 retry

    async def test_open_meteo_client_fails_fast_on_4xx(self) -> None:
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request: Latitude out of range"
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http, max_retries=2)
        stop = WeatherStopInput(name="Bad Stop", latitude=22.5, longitude=88.3)
        target_date = date.today() + timedelta(days=1)
        req = CorridorWeatherRequest(departure_date=target_date, stops=[stop])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.WEATHER_UNAVAILABLE
        assert mock_http.get.call_count == 1  # No retry on 4xx


class TestFixtureWeatherClient:
    """Verifies deterministic fixture client responses, error simulations, and path resolution."""

    @pytest.fixture
    def client(self) -> FixtureWeatherClient:
        return FixtureWeatherClient()

    def test_conforms_to_port(self, client: FixtureWeatherClient) -> None:
        assert isinstance(client, WeatherClientPort)

    async def test_get_corridor_weather_default_kolkata_digha(
        self, client: FixtureWeatherClient
    ) -> None:
        target_date = date.today() + timedelta(days=3)
        req = CorridorWeatherRequest(
            departure_date=target_date,
            stops=[
                WeatherStopInput(name="Kolkata", latitude=22.57, longitude=88.36),
                WeatherStopInput(name="Digha Beach", latitude=21.62, longitude=87.50),
            ],
        )
        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.AVAILABLE
        assert report.provider == "fixture"
        assert len(report.stops) >= 4
        assert report.stops[0].condition == StopWeatherCondition.SUNNY
        assert report.attribution == "Weather data by Open Meteo.com"

    async def test_get_corridor_weather_monsoon_ghat(self, client: FixtureWeatherClient) -> None:
        target_date = date.today() + timedelta(days=2)
        req = CorridorWeatherRequest(
            departure_date=target_date,
            stops=[
                WeatherStopInput(
                    name="Rohini Ghat Mountain Pass",
                    latitude=26.85,
                    longitude=88.28,
                    is_ghat_section=True,
                )
            ],
        )
        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.AVAILABLE
        assert any(s.monsoon_risk == MonsoonRisk.SEVERE for s in report.stops)
        assert report.ghat_caution is not None
        assert "fog" in report.ghat_caution.lower() or "ghat" in report.ghat_caution.lower()

    async def test_get_corridor_weather_sunny_corridor(self, client: FixtureWeatherClient) -> None:
        target_date = date.today() + timedelta(days=1)
        req = CorridorWeatherRequest(
            departure_date=target_date,
            stops=[WeatherStopInput(name="Sunny Plaza", latitude=23.23, longitude=87.86)],
        )
        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.AVAILABLE
        assert report.overall_caution is not None
        assert "favorable weather" in report.overall_caution.lower()

    async def test_get_corridor_weather_empty_stops(self, client: FixtureWeatherClient) -> None:
        target_date = date.today() + timedelta(days=1)
        req = CorridorWeatherRequest(
            departure_date=target_date,
            stops=[WeatherStopInput(name="empty", latitude=22.0, longitude=88.0)],
        )
        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.AVAILABLE
        assert len(report.stops) == 0

    async def test_get_corridor_weather_horizon_policy(self, client: FixtureWeatherClient) -> None:
        distant_date = date.today() + timedelta(days=30)
        req = CorridorWeatherRequest(
            departure_date=distant_date,
            stops=[WeatherStopInput(name="Digha", latitude=21.62, longitude=87.50)],
        )
        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON
        assert report.overall_caution is not None
        assert "16 day meteorological forecast horizon" in report.overall_caution.lower()

    async def test_simulation_flags(self) -> None:
        timeout_client = FixtureWeatherClient(simulate_timeout=True)
        req = CorridorWeatherRequest(
            departure_date=date.today() + timedelta(days=1),
            stops=[WeatherStopInput(name="Stop", latitude=22.0, longitude=88.0)],
        )
        with pytest.raises(TimeoutError):
            await timeout_client.get_corridor_weather(req)

        error_client = FixtureWeatherClient(simulate_error=RuntimeError("Provider failure"))
        with pytest.raises(RuntimeError):
            await error_client.get_corridor_weather(req)

        unavail_client = FixtureWeatherClient(simulate_unavailable=True)
        unavail_report = await unavail_client.get_corridor_weather(req)
        assert unavail_report.status == WeatherAdvisoryStatus.WEATHER_UNAVAILABLE

    def test_fixture_directory_resolution_with_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("WEATHER_FIXTURE_DIR", str(tmp_path))
        client = FixtureWeatherClient()
        assert client._fixture_dir == tmp_path

    def test_missing_fixture_raises_descriptive_error(self, tmp_path: Path) -> None:
        client = FixtureWeatherClient(fixture_dir=tmp_path)
        with pytest.raises(FileNotFoundError) as exc_info:
            client._load_json_fixture("nonexistent.json")
        assert "nonexistent.json" in str(exc_info.value)
        assert "Ensure tests/fixtures/weather exists or configure WEATHER_FIXTURE_DIR" in str(
            exc_info.value
        )


class TestWeatherClientFactory:
    """Verifies factory instantiation, configuration propagation, and provider isolation."""

    def test_create_fixture_client_by_default(self) -> None:
        client = create_weather_client()
        assert isinstance(client, FixtureWeatherClient)
        assert client.timeout_seconds == 10.0
        assert client.max_retries == 2

    def test_create_open_meteo_client_with_settings(self) -> None:
        settings = Settings(
            WEATHER_PROVIDER="open_meteo",
            WEATHER_TIMEOUT_SECONDS=20.0,
            WEATHER_MAX_RETRIES=3,
            OPEN_METEO_BASE_URL="https://custom.open-meteo.com/v1/forecast",
            OPEN_METEO_API_KEY="commercial_key_123",
        )
        client = create_weather_client(settings=settings)
        assert isinstance(client, OpenMeteoWeatherClient)
        assert client.timeout_seconds == 20.0
        assert client.max_retries == 3
        assert client.base_url == "https://custom.open-meteo.com/v1/forecast"
        assert client.api_key == "commercial_key_123"

    def test_create_weather_client_with_parameter_overrides(self) -> None:
        client = create_weather_client(
            provider="open_meteo",
            timeout_seconds=5.0,
            max_retries=1,
            base_url="https://test.open-meteo.com",
            api_key="override_key",
        )
        assert isinstance(client, OpenMeteoWeatherClient)
        assert client.timeout_seconds == 5.0
        assert client.max_retries == 1
        assert client.base_url == "https://test.open-meteo.com"
        assert client.api_key == "override_key"

    def test_create_fixture_client_with_settings_fixture_dir(self, tmp_path: Path) -> None:
        settings = Settings(
            WEATHER_PROVIDER="fixture",
            WEATHER_FIXTURE_DIR=str(tmp_path),
        )
        client = create_weather_client(settings=settings)
        assert isinstance(client, FixtureWeatherClient)
        assert client._fixture_dir == tmp_path

    def test_unknown_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            create_weather_client(provider="unknown_weather_corp")
        assert "unsupported weather provider" in str(exc_info.value).lower()


class TestSlice2Corrections:
    """Verifies targeted corrections for Slice 2 findings: timing, missing data, bounds safety, tests, and licensing."""

    async def test_finding_1_multi_day_trip_and_exact_hour_timing(self) -> None:
        """Finding 1: Forecast each stop for its actual local arrival date and hour, not departure midday."""
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        dep_date = date.today() + timedelta(days=2)
        arrival_stop1 = datetime(dep_date.year, dep_date.month, dep_date.day, 10, 0, tzinfo=tz)
        arrival_stop2 = datetime(
            dep_date.year, dep_date.month, dep_date.day, 14, 0, tzinfo=tz
        ) + timedelta(days=1)

        stop1 = WeatherStopInput(
            name="Stop Day 1", latitude=22.5, longitude=88.3, arrival_time=arrival_stop1
        )
        stop2 = WeatherStopInput(
            name="Stop Day 2", latitude=23.5, longitude=87.3, arrival_time=arrival_stop2
        )

        mock_http = AsyncMock(spec=httpx.AsyncClient)

        def mock_get(url: str, params: dict[str, Any], timeout: float) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            target_date_str = params["start_date"]
            resp.json.return_value = {
                "hourly": {
                    "time": [f"{target_date_str}T{h:02d}:00" for h in range(24)],
                    "temperature_2m": [20.0 + h for h in range(24)],
                    "relative_humidity_2m": [50 for _ in range(24)],
                    "precipitation_probability": [5 for _ in range(24)],
                    "precipitation": [0.0 for _ in range(24)],
                    "weather_code": [0 for _ in range(24)],
                    "is_day": [1 for _ in range(24)],
                }
            }
            return resp

        mock_http.get.side_effect = mock_get

        client = OpenMeteoWeatherClient(http_client=mock_http)
        req = CorridorWeatherRequest(departure_date=dep_date, stops=[stop1, stop2])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.AVAILABLE
        assert len(report.stops) == 2

        # Stop 1 arrived at 10:00 on Day 1 -> temperature should be 20.0 + 10 = 30.0
        assert report.stops[0].temperature_celsius == 30.0
        assert report.stops[0].forecast_time is not None
        assert report.stops[0].forecast_time.hour == 10
        assert report.stops[0].forecast_time.date() == dep_date

        # Stop 2 arrived at 14:00 on Day 2 -> temperature should be 20.0 + 14 = 34.0
        assert report.stops[1].temperature_celsius == 34.0
        assert report.stops[1].forecast_time is not None
        assert report.stops[1].forecast_time.hour == 14
        assert report.stops[1].forecast_time.date() == dep_date + timedelta(days=1)

    async def test_finding_1_midnight_arrival_hour(self) -> None:
        """Finding 1: Midnight arrival (00:00) resolves to hour 0 without defaulting to midday."""
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        dep_date = date.today() + timedelta(days=2)
        midnight_time = datetime(dep_date.year, dep_date.month, dep_date.day, 0, 0, tzinfo=tz)
        stop = WeatherStopInput(
            name="Midnight Stop", latitude=22.5, longitude=88.3, arrival_time=midnight_time
        )

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hourly": {
                "time": [f"{dep_date.isoformat()}T{h:02d}:00" for h in range(24)],
                "temperature_2m": [18.0 + h for h in range(24)],
                "relative_humidity_2m": [80 for _ in range(24)],
                "precipitation_probability": [0 for _ in range(24)],
                "precipitation": [0.0 for _ in range(24)],
                "weather_code": [0 for _ in range(24)],
                "is_day": [0 for _ in range(24)],
            }
        }
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        stop_report = await client.get_stop_weather(stop, dep_date)

        assert stop_report.temperature_celsius == 18.0  # hour 0 temperature
        assert stop_report.forecast_time is not None
        assert stop_report.forecast_time.hour == 0

    async def test_finding_1_missing_arrival_time_fallback_with_diagnostic(self) -> None:
        """Finding 1: When arrival is missing, fallback to departure date midday with honest diagnostic note."""
        dep_date = date.today() + timedelta(days=1)
        stop = WeatherStopInput(
            name="No Arrival Stop", latitude=22.5, longitude=88.3, arrival_time=None
        )

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hourly": {
                "time": [f"{dep_date.isoformat()}T{h:02d}:00" for h in range(24)],
                "temperature_2m": [22.0 + h for h in range(24)],
                "relative_humidity_2m": [60 for _ in range(24)],
                "precipitation_probability": [10 for _ in range(24)],
                "precipitation": [0.0 for _ in range(24)],
                "weather_code": [1 for _ in range(24)],
                "is_day": [1 for _ in range(24)],
            }
        }
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        stop_report = await client.get_stop_weather(stop, dep_date)

        assert stop_report.temperature_celsius == 34.0  # hour 12 midday: 22 + 12
        assert "timing_note" in stop_report.diagnostics
        assert "midday (12:00 local time)" in stop_report.diagnostics["timing_note"]

    def test_finding_2_missing_weather_metrics_evaluates_unknown_risk(self) -> None:
        """Finding 2: Missing precipitation and weather codes evaluate to MonsoonRisk.UNKNOWN, never LOW."""
        risk = evaluate_monsoon_risk(precipitation_mm=None, precip_prob=None, code=None)
        assert risk == MonsoonRisk.UNKNOWN

    async def test_finding_2_missing_metrics_represented_in_diagnostics(self) -> None:
        """Finding 2: Stop reports record missing_metrics in diagnostics when provider payload lacks fields."""
        dep_date = date.today() + timedelta(days=1)
        stop = WeatherStopInput(name="Missing Metric Stop", latitude=22.5, longitude=88.3)

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Omit precipitation and relative_humidity_2m
        mock_resp.json.return_value = {
            "hourly": {
                "time": [f"{dep_date.isoformat()}T{h:02d}:00" for h in range(24)],
                "temperature_2m": [25.0 for _ in range(24)],
                "weather_code": [0 for _ in range(24)],
            }
        }
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        stop_report = await client.get_stop_weather(stop, dep_date)

        assert stop_report.temperature_celsius == 25.0
        assert stop_report.humidity_percent is None
        assert stop_report.precipitation_mm is None
        assert "missing_metrics" in stop_report.diagnostics
        assert "relative_humidity_2m" in stop_report.diagnostics["missing_metrics"]
        assert "precipitation" in stop_report.diagnostics["missing_metrics"]

    async def test_finding_3_empty_or_malformed_provider_payload_no_crash(self) -> None:
        """Finding 3: Validate provider response shapes before indexing without raising IndexError or KeyError."""
        dep_date = date.today() + timedelta(days=1)
        stop = WeatherStopInput(name="Malformed Stop", latitude=22.5, longitude=88.3)

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hourly": {"time": []}}  # Empty time array
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        stop_report = await client.get_stop_weather(stop, dep_date)

        assert stop_report.condition == StopWeatherCondition.UNKNOWN
        assert stop_report.monsoon_risk == MonsoonRisk.UNKNOWN
        assert "error" in stop_report.diagnostics
        assert "empty time array" in stop_report.diagnostics["error"].lower()

    async def test_finding_3_short_hourly_arrays_no_index_error(self) -> None:
        """Finding 3: Truncated hourly arrays do not trigger IndexError; missing elements return None."""
        dep_date = date.today() + timedelta(days=1)
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        arrival = datetime(dep_date.year, dep_date.month, dep_date.day, 18, 0, tzinfo=tz)
        stop = WeatherStopInput(
            name="Short Array Stop", latitude=22.5, longitude=88.3, arrival_time=arrival
        )

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # time has 24 entries, but temperature_2m only has 3 entries
        mock_resp.json.return_value = {
            "hourly": {
                "time": [f"{dep_date.isoformat()}T{h:02d}:00" for h in range(24)],
                "temperature_2m": [21.0, 22.0, 23.0],  # Truncated!
                "weather_code": [0 for _ in range(24)],
            }
        }
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        stop_report = await client.get_stop_weather(stop, dep_date)

        # Should not raise IndexError
        assert stop_report.temperature_celsius is None
        assert "missing_metrics" in stop_report.diagnostics
        assert "temperature_2m" in stop_report.diagnostics["missing_metrics"]

    async def test_finding_3_partial_status_preserves_valid_stops(self) -> None:
        """Finding 3: Corridor with 1 valid stop and 1 failing stop returns PARTIAL and preserves valid stop."""
        dep_date = date.today() + timedelta(days=1)
        stop_ok = WeatherStopInput(name="Valid Stop", latitude=22.5, longitude=88.3)
        stop_bad = WeatherStopInput(name="Bad Stop", latitude=23.5, longitude=87.3)

        mock_http = AsyncMock(spec=httpx.AsyncClient)

        def mock_get(url: str, params: dict[str, Any], timeout: float) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if params["latitude"] == 22.5:
                resp.json.return_value = {
                    "hourly": {
                        "time": [f"{dep_date.isoformat()}T{h:02d}:00" for h in range(24)],
                        "temperature_2m": [28.0 for _ in range(24)],
                        "relative_humidity_2m": [60 for _ in range(24)],
                        "precipitation_probability": [0 for _ in range(24)],
                        "precipitation": [0.0 for _ in range(24)],
                        "weather_code": [0 for _ in range(24)],
                        "is_day": [1 for _ in range(24)],
                    }
                }
            else:
                resp.json.return_value = {"hourly": "not a dictionary"}
            return resp

        mock_http.get.side_effect = mock_get

        client = OpenMeteoWeatherClient(http_client=mock_http)
        req = CorridorWeatherRequest(departure_date=dep_date, stops=[stop_ok, stop_bad])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.PARTIAL
        assert len(report.stops) == 2
        assert report.stops[0].condition == StopWeatherCondition.SUNNY
        assert report.stops[0].temperature_celsius == 28.0
        assert report.stops[1].condition == StopWeatherCondition.UNKNOWN
        assert "error" in report.stops[1].diagnostics

    def test_finding_5_commercial_licensing_resolution(self) -> None:
        """Finding 5: Commercial licensing is explicitly configured or unknown; never inferred from key alone."""
        # 1. No key, no flag -> NON_COMMERCIAL
        c1 = OpenMeteoWeatherClient(api_key=None, commercial_license=None)
        status1, is_comm1 = c1._resolve_licensing_status()
        assert status1 == CommercialLicenseStatus.NON_COMMERCIAL
        assert is_comm1 is False

        # 2. Key present, no flag -> UNKNOWN (cannot infer commercial rights from key alone)
        c2 = OpenMeteoWeatherClient(api_key="om_test_key_xyz", commercial_license=None)
        status2, is_comm2 = c2._resolve_licensing_status()
        assert status2 == CommercialLicenseStatus.UNKNOWN
        assert is_comm2 is None

        # 3. Explicit commercial_license=True
        c3 = OpenMeteoWeatherClient(api_key="om_comm_key", commercial_license=True)
        status3, is_comm3 = c3._resolve_licensing_status()
        assert status3 == CommercialLicenseStatus.COMMERCIAL_LICENSED
        assert is_comm3 is True

        # 4. Explicit commercial_license=False
        c4 = OpenMeteoWeatherClient(api_key="om_free_key", commercial_license=False)
        status4, is_comm4 = c4._resolve_licensing_status()
        assert status4 == CommercialLicenseStatus.NON_COMMERCIAL
        assert is_comm4 is False

    async def test_finding_1_multi_day_trip_horizon_partial(self) -> None:
        """Finding 1: Multi-day trip where stop 1 is within horizon and stop 2 exceeds 16-day horizon."""
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        dep_date = date.today() + timedelta(days=2)
        arrival_stop1 = datetime(dep_date.year, dep_date.month, dep_date.day, 12, 0, tzinfo=tz)
        # Stop 2 arrives 20 days after today, exceeding the 16-day horizon
        future_dt = datetime.now(tz) + timedelta(days=20)
        arrival_stop2 = datetime(future_dt.year, future_dt.month, future_dt.day, 14, 0, tzinfo=tz)

        stop1 = WeatherStopInput(
            name="Near Stop", latitude=22.5, longitude=88.3, arrival_time=arrival_stop1
        )
        stop2 = WeatherStopInput(
            name="Far Stop", latitude=23.5, longitude=87.3, arrival_time=arrival_stop2
        )

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hourly": {
                "time": [f"{dep_date.isoformat()}T{h:02d}:00" for h in range(24)],
                "temperature_2m": [28.0 for _ in range(24)],
                "relative_humidity_2m": [60 for _ in range(24)],
                "precipitation_probability": [0 for _ in range(24)],
                "precipitation": [0.0 for _ in range(24)],
                "weather_code": [0 for _ in range(24)],
                "is_day": [1 for _ in range(24)],
            }
        }
        mock_http.get.return_value = mock_resp

        client = OpenMeteoWeatherClient(http_client=mock_http)
        req = CorridorWeatherRequest(departure_date=dep_date, stops=[stop1, stop2])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.PARTIAL
        assert len(report.stops) == 2
        assert report.stops[0].condition == StopWeatherCondition.SUNNY
        assert report.stops[0].temperature_celsius == 28.0
        assert report.stops[1].condition == StopWeatherCondition.UNKNOWN
        assert report.stops[1].diagnostics.get("status") == "OUT_OF_FORECAST_HORIZON"
        assert "16 day meteorological forecast horizon" in str(
            report.stops[1].diagnostics.get("reason")
        )

    async def test_finding_5_secret_masking_no_key_leakage(self) -> None:
        """Finding 5: API keys must not be leaked into diagnostics, cautions, or stop reports."""
        secret_key = "super_secret_open_meteo_key_98765"
        dep_date = date.today() + timedelta(days=1)
        stop = WeatherStopInput(name="Secret Stop", latitude=22.5, longitude=88.3)

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        # Simulate network error where raw exception text embeds URL with query param
        mock_http.get.side_effect = httpx.ConnectError(
            f"Failed to connect to https://api.open-meteo.com/v1/forecast?apikey={secret_key}&lat=22.5"
        )

        client = OpenMeteoWeatherClient(api_key=secret_key, http_client=mock_http, max_retries=0)
        req = CorridorWeatherRequest(departure_date=dep_date, stops=[stop])

        report = await client.get_corridor_weather(req)
        assert report.status == WeatherAdvisoryStatus.WEATHER_UNAVAILABLE

        # Check serialized payload and all string representations
        report_dump = report.model_dump_json()
        assert secret_key not in report_dump
        assert secret_key not in str(report.diagnostics)
        assert secret_key not in str(report.stops[0].diagnostics)
        assert "***" in str(report.stops[0].diagnostics.get("error"))
