"""Unit tests for weather advisory contracts, Open Meteo adapter, fixtures, and factory."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from config.config import Settings
from contracts.weather import (
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
