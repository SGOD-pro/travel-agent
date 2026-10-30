"""Weather client adapters for live Open Meteo and offline fixtures."""

from functions.orchestrator.weather.adapters.fixture_client import FixtureWeatherClient
from functions.orchestrator.weather.adapters.open_meteo import OpenMeteoWeatherClient

__all__ = [
    "FixtureWeatherClient",
    "OpenMeteoWeatherClient",
]

