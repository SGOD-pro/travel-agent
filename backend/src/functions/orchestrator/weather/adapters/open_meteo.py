"""Open Meteo HTTP client adapter for corridor weather advisories.

Conforms to specification 0007 AC-6:
- Consumes Open Meteo forecast API under non commercial terms
- Requires mandatory provider attribution: "Weather data by Open Meteo.com"
- Enforces 16 day forecast horizon policy without presenting historical averages as live forecasts
- Handles timezone offsets, missing coordinates, and transient provider outages
- Bounded retries with exponential backoff on network failures
"""

from __future__ import annotations

import asyncio
import logging
import zoneinfo
from datetime import date, datetime, timedelta
from typing import Any, cast

import httpx

from contracts.weather import (
    CorridorWeatherAdvisoryReport,
    CorridorWeatherRequest,
    MonsoonRisk,
    StopWeatherCondition,
    StopWeatherReport,
    WeatherAdvisoryStatus,
    WeatherStopInput,
)
from functions.orchestrator.weather.port import WeatherClientPort

logger = logging.getLogger(__name__)

# Maximum forecast horizon supported by Open Meteo live numerical weather models
MAX_FORECAST_HORIZON_DAYS: int = 16

# Required provider attribution string per Open Meteo terms of service
OPEN_METEO_ATTRIBUTION: str = "Weather data by Open Meteo.com"


def map_wmo_code_to_condition(code: int | None) -> StopWeatherCondition:
    """Maps standard WMO weather codes to categorical StopWeatherCondition."""
    if code is None:
        return StopWeatherCondition.UNKNOWN
    if code in (0, 1):
        return StopWeatherCondition.SUNNY
    if code in (2, 3):
        return StopWeatherCondition.CLOUDY
    if code in (45, 48):
        return StopWeatherCondition.FOGGY
    if code in (
        51,
        53,
        55,
        56,
        57,
        61,
        63,
        65,
        66,
        67,
        71,
        73,
        75,
        77,
        80,
        81,
        82,
        85,
        86,
        95,
        96,
        99,
    ):
        return StopWeatherCondition.RAINY
    return StopWeatherCondition.UNKNOWN


def evaluate_monsoon_risk(
    precipitation_mm: float | None,
    precip_prob: int | None,
    code: int | None,
) -> MonsoonRisk:
    """Evaluates monsoon hazard severity from precipitation volume and probability."""
    precip = precipitation_mm or 0.0
    prob = precip_prob or 0

    if precip >= 25.0 or (prob >= 70 and code in (63, 65, 81, 82, 95, 96, 99)):
        return MonsoonRisk.SEVERE
    if precip >= 10.0 or (prob >= 40 and code in (53, 55, 61, 63, 80, 81, 95)):
        return MonsoonRisk.MODERATE
    return MonsoonRisk.LOW


def evaluate_ghat_caution(
    stop: WeatherStopInput,
    condition: StopWeatherCondition,
    monsoon_risk: MonsoonRisk,
) -> str | None:
    """Evaluates mountain pass and ghat road advisories based on conditions."""
    name_lower = stop.name.lower()
    is_ghat = stop.is_ghat_section or any(
        k in name_lower for k in ("ghat", "pass", "hill", "mountain", "valley")
    )

    if is_ghat:
        if condition == StopWeatherCondition.FOGGY:
            return "Dense fog and reduced visibility expected along ghat section; reduce speed and engage fog lamps."
        if monsoon_risk == MonsoonRisk.SEVERE:
            return "Severe rainfall warning along ghat section; high risk of waterlogging, rockfall, and slick road surfaces."
        if monsoon_risk == MonsoonRisk.MODERATE:
            return "Wet road surfaces and reduced traction expected along ghat section; exercise caution on hairpin turns."
        return "Mountainous ghat route with winding turns; maintain moderate speed."

    if condition == StopWeatherCondition.FOGGY:
        return "Patchy fog may cause reduced visibility along early morning driving sectors."

    return None


class OpenMeteoWeatherClient(WeatherClientPort):
    """Asynchronous client adapter for the Open Meteo weather forecast API."""

    def __init__(
        self,
        base_url: str = "https://api.open-meteo.com/v1/forecast",
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._http_client = http_client

    def _resolve_timezone(self, tz_str: str) -> zoneinfo.ZoneInfo:
        """Safely parses and resolves IANA timezone identifier."""
        try:
            return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            logger.warning("Unrecognized timezone '%s'; falling back to Asia/Kolkata", tz_str)
            return zoneinfo.ZoneInfo("Asia/Kolkata")

    def _check_forecast_horizon(
        self, target_date: date, tz: zoneinfo.ZoneInfo
    ) -> tuple[bool, str | None]:
        """Checks if target date lies within the 16 day forecast horizon."""
        today = datetime.now(tz).date()
        max_date = today + timedelta(days=MAX_FORECAST_HORIZON_DAYS)

        if target_date > max_date:
            days_out = (target_date - today).days
            return (
                False,
                f"Departure date ({target_date}) is {days_out} days out, exceeding the "
                f"{MAX_FORECAST_HORIZON_DAYS} day meteorological forecast horizon. Live forecasts "
                "are unavailable; check back closer to departure for live conditions. "
                "Historical averages are not presented as live forecasts.",
            )

        if target_date < today - timedelta(days=1):
            return (
                False,
                f"Requested date ({target_date}) is in the past. Live forecast models only "
                "provide current and forward looking predictions.",
            )

        return True, None

    async def _fetch_point_forecast(
        self,
        lat: float,
        lon: float,
        target_date: date,
        tz_str: str,
    ) -> dict[str, Any]:
        """Performs HTTP request with bounded retries to Open Meteo forecast endpoint."""
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,is_day",
            "timezone": tz_str,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        }
        if self.api_key:
            params["apikey"] = self.api_key

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self._http_client is not None:
                    response = await self._http_client.get(
                        self.base_url,
                        params=params,
                        timeout=self.timeout_seconds,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            self.base_url,
                            params=params,
                            timeout=self.timeout_seconds,
                        )

                if response.status_code == 200:
                    return cast(dict[str, Any], response.json())

                if response.status_code >= 500:
                    last_error = RuntimeError(
                        f"Open Meteo returned server error {response.status_code}"
                    )
                else:
                    # 4xx client errors should fail fast without retrying
                    raise ValueError(
                        f"Open Meteo API rejected request with HTTP {response.status_code}: {response.text}"
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc

            if attempt < self.max_retries:
                backoff = 0.25 * (2**attempt)
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"Open Meteo API request failed after {self.max_retries + 1} attempts"
        ) from last_error

    async def get_stop_weather(
        self,
        stop: WeatherStopInput,
        target_date: date | datetime,
        timezone: str = "Asia/Kolkata",
    ) -> StopWeatherReport:
        """Retrieves and evaluates meteorological forecast for a single stop."""
        tz = self._resolve_timezone(timezone)
        req_date = target_date.date() if isinstance(target_date, datetime) else target_date

        is_valid_horizon, reason = self._check_forecast_horizon(req_date, tz)
        if not is_valid_horizon:
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.LOW,
                ghat_caution=reason,
            )

        data = await self._fetch_point_forecast(stop.latitude, stop.longitude, req_date, timezone)
        hourly = data.get("hourly", {})
        times: list[str] = hourly.get("time", [])

        if not times:
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.LOW,
            )

        # Select index based on arrival_time if provided, otherwise default to midday (12:00)
        selected_idx = min(12, len(times) - 1)
        if stop.arrival_time is not None:
            arrival_dt = stop.arrival_time
            if arrival_dt.tzinfo is None:
                arrival_dt = arrival_dt.replace(tzinfo=tz)
            else:
                arrival_dt = arrival_dt.astimezone(tz)
            arrival_iso_prefix = arrival_dt.strftime("%Y-%m-%dT%H")
            for idx, time_str in enumerate(times):
                if time_str.startswith(arrival_iso_prefix):
                    selected_idx = idx
                    break

        temp = hourly.get("temperature_2m", [None])[selected_idx]
        apparent_temp = hourly.get("apparent_temperature", [None])[selected_idx]
        humidity = hourly.get("relative_humidity_2m", [None])[selected_idx]
        precip_prob = hourly.get("precipitation_probability", [None])[selected_idx]
        precip_mm = hourly.get("precipitation", [None])[selected_idx]
        weather_code = hourly.get("weather_code", [None])[selected_idx]
        is_day_val = hourly.get("is_day", [1])[selected_idx]

        condition = map_wmo_code_to_condition(weather_code)
        monsoon = evaluate_monsoon_risk(precip_mm, precip_prob, weather_code)
        ghat_adv = evaluate_ghat_caution(stop, condition, monsoon)

        forecast_time: datetime | None = None
        if selected_idx < len(times):
            try:
                forecast_time = datetime.fromisoformat(times[selected_idx]).replace(tzinfo=tz)
            except Exception:
                forecast_time = None

        return StopWeatherReport(
            stop_name=stop.name,
            latitude=stop.latitude,
            longitude=stop.longitude,
            temperature_celsius=float(temp) if temp is not None else None,
            apparent_temperature_celsius=float(apparent_temp)
            if apparent_temp is not None
            else None,
            condition=condition,
            humidity_percent=int(humidity) if humidity is not None else None,
            precipitation_probability=int(precip_prob) if precip_prob is not None else None,
            precipitation_mm=float(precip_mm) if precip_mm is not None else None,
            weather_code=int(weather_code) if weather_code is not None else None,
            monsoon_risk=monsoon,
            ghat_caution=ghat_adv,
            forecast_time=forecast_time,
            is_day=bool(is_day_val),
        )

    async def get_corridor_weather(
        self, request: CorridorWeatherRequest
    ) -> CorridorWeatherAdvisoryReport:
        """Evaluates weather conditions across all stops in a travel corridor."""
        tz = self._resolve_timezone(request.timezone)
        req_date = (
            request.departure_date.date()
            if isinstance(request.departure_date, datetime)
            else request.departure_date
        )

        is_valid_horizon, horizon_reason = self._check_forecast_horizon(req_date, tz)
        if not is_valid_horizon:
            return CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON,
                stops=[
                    StopWeatherReport(
                        stop_name=s.name,
                        latitude=s.latitude,
                        longitude=s.longitude,
                        condition=StopWeatherCondition.UNKNOWN,
                        monsoon_risk=MonsoonRisk.LOW,
                    )
                    for s in request.stops
                ],
                overall_caution=horizon_reason,
                provider="open_meteo",
                attribution=OPEN_METEO_ATTRIBUTION,
                is_commercial_licensed=bool(self.api_key),
                forecast_horizon_days=MAX_FORECAST_HORIZON_DAYS,
                diagnostics={"horizon_check_failed": True, "target_date": str(req_date)},
            )

        reports: list[StopWeatherReport] = []
        try:
            # Query each stop concurrently
            tasks = [
                self.get_stop_weather(stop, req_date, request.timezone) for stop in request.stops
            ]
            reports = list(await asyncio.gather(*tasks))

        except Exception as exc:
            logger.error("Failed to retrieve corridor weather from Open Meteo: %s", exc)
            return CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.WEATHER_UNAVAILABLE,
                stops=[
                    StopWeatherReport(
                        stop_name=s.name,
                        latitude=s.latitude,
                        longitude=s.longitude,
                        condition=StopWeatherCondition.UNKNOWN,
                        monsoon_risk=MonsoonRisk.LOW,
                    )
                    for s in request.stops
                ],
                overall_caution=(
                    "Weather forecast service temporarily unavailable; proceeding with regional defaults."
                ),
                provider="open_meteo",
                attribution=OPEN_METEO_ATTRIBUTION,
                is_commercial_licensed=bool(self.api_key),
                forecast_horizon_days=MAX_FORECAST_HORIZON_DAYS,
                diagnostics={"error": str(exc)},
            )

        # Synthesize corridor level caution
        has_severe_monsoon = any(r.monsoon_risk == MonsoonRisk.SEVERE for r in reports)
        has_moderate_monsoon = any(r.monsoon_risk == MonsoonRisk.MODERATE for r in reports)
        has_fog = any(r.condition == StopWeatherCondition.FOGGY for r in reports)
        ghat_cautions = [r.ghat_caution for r in reports if r.ghat_caution]

        overall_caution: str
        if has_severe_monsoon:
            overall_caution = (
                "Severe monsoon conditions detected along corridor stops. "
                "Potential travel delays and localized waterlogging expected."
            )
        elif has_moderate_monsoon:
            overall_caution = (
                "Moderate rain showers expected along the travel corridor. "
                "Carry umbrellas and anticipate wet roads."
            )
        elif has_fog:
            overall_caution = (
                "Patchy fog expected along sections of the corridor. "
                "Reduced driving visibility possible during morning hours."
            )
        else:
            overall_caution = "Favorable weather conditions expected along the corridor."

        combined_ghat_caution = " ".join(dict.fromkeys(ghat_cautions)) if ghat_cautions else None

        return CorridorWeatherAdvisoryReport(
            status=WeatherAdvisoryStatus.AVAILABLE,
            stops=reports,
            overall_caution=overall_caution,
            ghat_caution=combined_ghat_caution,
            provider="open_meteo",
            attribution=OPEN_METEO_ATTRIBUTION,
            is_commercial_licensed=bool(self.api_key),
            forecast_horizon_days=MAX_FORECAST_HORIZON_DAYS,
            diagnostics={"stops_queried": len(reports)},
        )
