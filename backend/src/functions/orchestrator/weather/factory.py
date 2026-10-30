"""Factory for constructing weather advisory client adapters based on configuration."""

from __future__ import annotations

from pathlib import Path

import httpx

from config.config import Settings, get_settings
from functions.orchestrator.weather.adapters.fixture_client import FixtureWeatherClient
from functions.orchestrator.weather.adapters.open_meteo import OpenMeteoWeatherClient
from functions.orchestrator.weather.port import WeatherClientPort


def create_weather_client(
    settings: Settings | None = None,
    provider: str | None = None,
    commercial_license: bool | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    fixture_dir: Path | str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> WeatherClientPort:
    """Creates and returns the configured weather client adapter.

    Args:
        settings: Application settings instance (defaults to singleton get_settings()).
        provider: Optional explicit provider override ("fixture", "open_meteo").
        commercial_license: Optional explicit commercial license status flag.
        timeout_seconds: Optional socket timeout in seconds.
        max_retries: Optional maximum retry attempts.
        fixture_dir: Optional custom directory containing JSON fixture files.
        base_url: Optional custom Open Meteo API base URL.
        api_key: Optional commercial API key for Open Meteo.
        http_client: Optional injected httpx AsyncClient for testing.

    Returns:
        Instance conforming to WeatherClientPort.

    Raises:
        ValueError: For unsupported or invalid provider identifiers.
    """
    effective_settings = settings or get_settings()
    effective_provider = (
        (provider if provider is not None else effective_settings.WEATHER_PROVIDER).strip().lower()
    )

    effective_commercial_license = (
        commercial_license
        if commercial_license is not None
        else effective_settings.OPEN_METEO_COMMERCIAL_LICENSE
    )
    effective_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else effective_settings.WEATHER_TIMEOUT_SECONDS
    )
    effective_retries = (
        max_retries if max_retries is not None else effective_settings.WEATHER_MAX_RETRIES
    )
    effective_base_url = (
        base_url if base_url is not None else effective_settings.OPEN_METEO_BASE_URL
    )
    effective_api_key = api_key if api_key is not None else effective_settings.OPEN_METEO_API_KEY

    if effective_provider == "fixture":
        resolved_fixture_dir = fixture_dir
        if resolved_fixture_dir is None and effective_settings.WEATHER_FIXTURE_DIR:
            resolved_fixture_dir = Path(effective_settings.WEATHER_FIXTURE_DIR)
        return FixtureWeatherClient(
            fixture_dir=resolved_fixture_dir,
            commercial_license=effective_commercial_license,
            timeout_seconds=effective_timeout,
            max_retries=effective_retries,
        )

    if effective_provider == "open_meteo":
        return OpenMeteoWeatherClient(
            base_url=effective_base_url,
            api_key=effective_api_key,
            commercial_license=effective_commercial_license,
            timeout_seconds=effective_timeout,
            max_retries=effective_retries,
            http_client=http_client,
        )

    raise ValueError(
        f"Unsupported weather provider '{effective_provider}'. "
        "Supported providers: 'fixture', 'open_meteo'."
    )
