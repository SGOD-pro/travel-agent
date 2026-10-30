"""Pydantic domain contracts for weather advisory integration.

Defines schemas for:
- Stop weather inputs and reports
- Corridor weather requests and aggregated advisories
- Weather conditions, monsoon risk, and advisory status enums
- Provider attribution and commercial licensing metadata

Terms and attribution notice:
Free usage of Open Meteo API is strictly for non commercial purposes with required attribution:
"Weather data by Open Meteo.com". Commercial deployments require an enterprise license key.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StopWeatherCondition(StrEnum):
    """Categorical classification of weather conditions at a stop."""

    SUNNY = "sunny"
    CLOUDY = "cloudy"
    RAINY = "rainy"
    FOGGY = "foggy"
    UNKNOWN = "unknown"


class MonsoonRisk(StrEnum):
    """Categorical monsoon hazard level based on precipitation intensity."""

    LOW = "low"
    MODERATE = "moderate"
    SEVERE = "severe"


class WeatherAdvisoryStatus(StrEnum):
    """Operational status of the corridor weather advisory."""

    AVAILABLE = "AVAILABLE"
    OUT_OF_FORECAST_HORIZON = "OUT_OF_FORECAST_HORIZON"
    WEATHER_UNAVAILABLE = "WEATHER_UNAVAILABLE"


class WeatherStopInput(BaseModel):
    """Point location input for corridor weather sampling."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = Field(..., min_length=1, description="Stop or landmark label")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude coordinate")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude coordinate")
    arrival_time: datetime | None = Field(
        default=None, description="Optional anticipated arrival timestamp"
    )
    departure_time: datetime | None = Field(
        default=None, description="Optional anticipated departure timestamp"
    )
    is_ghat_section: bool = Field(
        default=False, description="Flag indicating hilly terrain or ghat mountain pass"
    )


class StopWeatherReport(BaseModel):
    """Evaluated weather forecast report for a single itinerary stop."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    stop_name: str = Field(..., min_length=1, description="Stop identifier or name")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude coordinate")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude coordinate")
    temperature_celsius: float | None = Field(
        default=None, description="Ambient temperature in degrees Celsius"
    )
    apparent_temperature_celsius: float | None = Field(
        default=None, description="Feels like temperature in degrees Celsius"
    )
    condition: StopWeatherCondition = Field(
        default=StopWeatherCondition.UNKNOWN, description="Weather condition category"
    )
    humidity_percent: int | None = Field(
        default=None, ge=0, le=100, description="Relative humidity percentage"
    )
    precipitation_probability: int | None = Field(
        default=None, ge=0, le=100, description="Probability of precipitation percentage"
    )
    precipitation_mm: float | None = Field(
        default=None, ge=0.0, description="Estimated precipitation volume in millimeters"
    )
    weather_code: int | None = Field(
        default=None, description="Standard WMO weather interpretation code"
    )
    monsoon_risk: MonsoonRisk = Field(
        default=MonsoonRisk.LOW, description="Evaluated monsoon hazard severity"
    )
    ghat_caution: str | None = Field(
        default=None, description="Specific driving advisory for ghat or mountain terrain"
    )
    forecast_time: datetime | None = Field(
        default=None, description="Timestamp corresponding to sampled forecast"
    )
    is_day: bool = Field(default=True, description="Flag indicating daylight hours")


class CorridorWeatherRequest(BaseModel):
    """Request payload for evaluating weather metrics along a travel corridor."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    trip_id: str | None = Field(default=None, description="Associated trip identifier")
    departure_date: date | datetime = Field(
        ..., description="Scheduled departure date or start timestamp"
    )
    stops: list[WeatherStopInput] = Field(
        ..., min_length=1, description="Ordered list of corridor stops to sample"
    )
    timezone: str = Field(
        default="Asia/Kolkata", description="Target IANA timezone string for forecast alignment"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Optional execution context metadata"
    )


class CorridorWeatherAdvisoryReport(BaseModel):
    """Aggregated corridor weather report with stop forecasts and regional advisories."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: WeatherAdvisoryStatus = Field(
        default=WeatherAdvisoryStatus.AVAILABLE,
        description="Advisory availability status",
    )
    stops: list[StopWeatherReport] = Field(
        default_factory=list, description="Evaluated weather reports per stop"
    )
    overall_caution: str | None = Field(
        default=None, description="General regional caution or monsoon warning"
    )
    ghat_caution: str | None = Field(
        default=None, description="Aggregated caution for mountain pass or ghat driving"
    )
    provider: str = Field(
        default="open_meteo", description="Identifier of the weather service adapter"
    )
    attribution: str = Field(
        default="Weather data by Open Meteo.com",
        description="Mandatory provider attribution notice for non commercial usage",
    )
    is_commercial_licensed: bool = Field(
        default=False,
        description="Flag indicating whether a commercial Open Meteo subscription is active",
    )
    forecast_horizon_days: int = Field(
        default=16,
        description="Maximum forecast horizon supported by live meteorological model",
    )
    diagnostics: dict[str, Any] = Field(
        default_factory=dict, description="Diagnostic provider or execution details"
    )

