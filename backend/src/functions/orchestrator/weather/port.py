"""Abstract port for weather advisory client adapters.

Defines the contract for fetching corridor weather metrics and single stop
forecasts across live providers (Open Meteo) and offline test fixtures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

from contracts.weather import (
    CorridorWeatherAdvisoryReport,
    CorridorWeatherRequest,
    StopWeatherReport,
    WeatherStopInput,
)


class WeatherClientPort(ABC):
    """Abstract port for weather data retrieval along travel corridors."""

    @abstractmethod
    async def get_corridor_weather(
        self, request: CorridorWeatherRequest
    ) -> CorridorWeatherAdvisoryReport:
        """Evaluates weather conditions and risks across all stops in a travel corridor.

        Args:
            request: Corridor weather request with stops, departure date, and timezone.

        Returns:
            Aggregated corridor weather advisory report.
        """
        ...

    @abstractmethod
    async def get_stop_weather(
        self,
        stop: WeatherStopInput,
        target_date: date | datetime,
        timezone: str = "Asia/Kolkata",
    ) -> StopWeatherReport:
        """Retrieves meteorological forecast for a single stop point.

        Args:
            stop: Stop input with location coordinates.
            target_date: Scheduled date or arrival timestamp.
            timezone: Target IANA timezone string.

        Returns:
            Evaluated weather report for the stop.
        """
        ...

