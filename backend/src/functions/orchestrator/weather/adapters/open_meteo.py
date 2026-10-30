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
    CommercialLicenseStatus,
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
    """Evaluates monsoon hazard severity from precipitation volume and probability.

    If precipitation volume, probability, and weather codes are all missing or None,
    evaluates to MonsoonRisk.UNKNOWN rather than guessing low risk.
    """
    if precipitation_mm is None and precip_prob is None and code is None:
        return MonsoonRisk.UNKNOWN

    precip = precipitation_mm if precipitation_mm is not None else 0.0
    prob = precip_prob if precip_prob is not None else 0

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
        commercial_license: bool | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.commercial_license = commercial_license
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._http_client = http_client

    def _mask_secrets(self, text: str) -> str:
        """Sanitizes text by masking sensitive API keys to prevent secret leaks."""
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "***")
        return text

    def _resolve_licensing_status(self) -> tuple[CommercialLicenseStatus, bool | None]:
        """Resolves licensing status without inferring commercial rights from key presence alone."""
        if self.commercial_license is True:
            return CommercialLicenseStatus.COMMERCIAL_LICENSED, True
        if self.commercial_license is False:
            return CommercialLicenseStatus.NON_COMMERCIAL, False
        if self.api_key:
            return CommercialLicenseStatus.UNKNOWN, None
        return CommercialLicenseStatus.NON_COMMERCIAL, False

    def _resolve_timezone(self, tz_str: str) -> zoneinfo.ZoneInfo:
        """Safely parses and resolves IANA timezone identifier."""
        try:
            return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            logger.warning("Unrecognized timezone '%s'; falling back to Asia/Kolkata", tz_str)
            return zoneinfo.ZoneInfo("Asia/Kolkata")

    def _resolve_stop_target_datetime(
        self,
        stop: WeatherStopInput,
        default_departure_date: date | datetime,
        tz: zoneinfo.ZoneInfo,
    ) -> tuple[date, int, bool]:
        """Resolves target local date, hour, and whether arrival was explicitly specified.

        Returns:
            (target_date, target_hour, is_arrival_explicit)
        """
        if stop.arrival_time is not None:
            arrival_dt = (
                stop.arrival_time.replace(tzinfo=tz)
                if stop.arrival_time.tzinfo is None
                else stop.arrival_time.astimezone(tz)
            )
            return arrival_dt.date(), arrival_dt.hour, True

        if stop.departure_time is not None:
            dep_dt = (
                stop.departure_time.replace(tzinfo=tz)
                if stop.departure_time.tzinfo is None
                else stop.departure_time.astimezone(tz)
            )
            return dep_dt.date(), dep_dt.hour, True

        base_date = (
            default_departure_date.date()
            if isinstance(default_departure_date, datetime)
            else default_departure_date
        )
        return base_date, 12, False

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
                f"Date ({target_date}) is {days_out} days out, exceeding the "
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
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Open Meteo response body is not a JSON dictionary")
                    return cast(dict[str, Any], payload)

                if response.status_code >= 500:
                    last_error = RuntimeError(
                        f"Open Meteo returned server error {response.status_code}"
                    )
                else:
                    # 4xx client errors should fail fast without retrying
                    raise ValueError(
                        f"Open Meteo API rejected request with HTTP {response.status_code}"
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc

            if attempt < self.max_retries:
                backoff = 0.25 * (2**attempt)
                await asyncio.sleep(backoff)

        cause_desc = (
            f": {type(last_error).__name__} ({self._mask_secrets(str(last_error))})"
            if last_error
            else ""
        )
        raise RuntimeError(
            f"Open Meteo API request failed after {self.max_retries + 1} attempts{cause_desc}"
        ) from last_error

    def _extract_stop_weather_from_response(
        self,
        stop: WeatherStopInput,
        data: Any,
        stop_date: date,
        stop_hour: int,
        tz: zoneinfo.ZoneInfo,
        is_arrival_explicit: bool,
    ) -> StopWeatherReport:
        """Safely parses and validates provider response shapes and array lengths before indexing."""
        if not isinstance(data, dict):
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.UNKNOWN,
                diagnostics={"error": "Malformed provider response: root is not a dictionary"},
            )

        hourly = data.get("hourly")
        if not isinstance(hourly, dict):
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.UNKNOWN,
                diagnostics={"error": "Missing or invalid hourly dictionary in provider response"},
            )

        times = hourly.get("time")
        if not isinstance(times, list) or len(times) == 0:
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.UNKNOWN,
                diagnostics={"error": "Missing or empty time array in provider response"},
            )

        # Match exact ISO hour prefix if possible (e.g. 2026-11-01T00 for midnight or 2026-11-01T14)
        target_iso_prefix = f"{stop_date.isoformat()}T{stop_hour:02d}:"
        selected_idx = min(stop_hour, len(times) - 1)
        matched_exact_hour = False
        for idx, time_str in enumerate(times):
            if isinstance(time_str, str) and time_str.startswith(target_iso_prefix):
                selected_idx = idx
                matched_exact_hour = True
                break

        def _get_hourly_val(key: str) -> Any:
            arr = hourly.get(key)
            if isinstance(arr, list) and 0 <= selected_idx < len(arr):
                return arr[selected_idx]
            return None

        temp = _get_hourly_val("temperature_2m")
        apparent_temp = _get_hourly_val("apparent_temperature")
        humidity = _get_hourly_val("relative_humidity_2m")
        precip_prob = _get_hourly_val("precipitation_probability")
        precip_mm = _get_hourly_val("precipitation")
        weather_code = _get_hourly_val("weather_code")
        is_day_val = _get_hourly_val("is_day")

        condition = map_wmo_code_to_condition(weather_code)
        monsoon = evaluate_monsoon_risk(precip_mm, precip_prob, weather_code)
        ghat_adv = evaluate_ghat_caution(stop, condition, monsoon)

        forecast_time: datetime | None = None
        if 0 <= selected_idx < len(times) and isinstance(times[selected_idx], str):
            try:
                forecast_time = datetime.fromisoformat(times[selected_idx]).replace(tzinfo=tz)
            except Exception:
                forecast_time = None

        stop_diagnostics: dict[str, Any] = {}
        if not is_arrival_explicit:
            stop_diagnostics["timing_note"] = (
                "Arrival time omitted; defaulted to departure date midday (12:00 local time)"
            )
        if not matched_exact_hour:
            stop_diagnostics["hour_match"] = "exact hour not found; using closest available index"

        missing_fields: list[str] = [
            f
            for f in ("temperature_2m", "relative_humidity_2m", "precipitation", "weather_code")
            if _get_hourly_val(f) is None
        ]
        if missing_fields:
            stop_diagnostics["missing_metrics"] = missing_fields

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
            is_day=bool(is_day_val) if is_day_val is not None else True,
            diagnostics=stop_diagnostics,
        )

    async def get_stop_weather(
        self,
        stop: WeatherStopInput,
        target_date: date | datetime,
        timezone: str = "Asia/Kolkata",
    ) -> StopWeatherReport:
        """Retrieves and evaluates meteorological forecast for a single stop."""
        tz = self._resolve_timezone(timezone)
        stop_date, stop_hour, is_arrival_explicit = self._resolve_stop_target_datetime(
            stop, target_date, tz
        )

        is_valid_horizon, reason = self._check_forecast_horizon(stop_date, tz)
        if not is_valid_horizon:
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.UNKNOWN,
                ghat_caution=reason,
                diagnostics={
                    "status": "OUT_OF_FORECAST_HORIZON",
                    "target_date": stop_date.isoformat(),
                    "reason": reason,
                },
            )

        try:
            data = await self._fetch_point_forecast(
                stop.latitude, stop.longitude, stop_date, timezone
            )
            return self._extract_stop_weather_from_response(
                stop, data, stop_date, stop_hour, tz, is_arrival_explicit
            )
        except (httpx.TimeoutException, httpx.NetworkError, RuntimeError, ValueError) as exc:
            safe_err = self._mask_secrets(str(exc))
            logger.warning("Failed to fetch weather for stop '%s': %s", stop.name, safe_err)
            return StopWeatherReport(
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                condition=StopWeatherCondition.UNKNOWN,
                monsoon_risk=MonsoonRisk.UNKNOWN,
                diagnostics={"error": safe_err},
            )

    async def get_corridor_weather(
        self, request: CorridorWeatherRequest
    ) -> CorridorWeatherAdvisoryReport:
        """Evaluates weather conditions across all stops in a travel corridor."""
        licensing_status, is_commercial = self._resolve_licensing_status()

        # Query each stop safely in parallel without letting one stop failure discard others
        tasks = [
            self.get_stop_weather(stop, request.departure_date, request.timezone)
            for stop in request.stops
        ]
        reports = list(await asyncio.gather(*tasks))

        # Evaluate validity of stops
        valid_reports = [
            r
            for r in reports
            if r.condition != StopWeatherCondition.UNKNOWN or r.temperature_celsius is not None
        ]
        horizon_exceeded_reports = [
            r for r in reports if r.diagnostics.get("status") == "OUT_OF_FORECAST_HORIZON"
        ]
        error_reports = [r for r in reports if "error" in r.diagnostics]

        # Determine corridor advisory status
        status: WeatherAdvisoryStatus
        if len(valid_reports) == len(reports):
            status = WeatherAdvisoryStatus.AVAILABLE
        elif len(valid_reports) > 0:
            status = WeatherAdvisoryStatus.PARTIAL
        elif len(horizon_exceeded_reports) == len(reports):
            status = WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON
        else:
            status = WeatherAdvisoryStatus.WEATHER_UNAVAILABLE

        # Synthesize overall caution
        has_severe_monsoon = any(r.monsoon_risk == MonsoonRisk.SEVERE for r in reports)
        has_moderate_monsoon = any(r.monsoon_risk == MonsoonRisk.MODERATE for r in reports)
        has_fog = any(r.condition == StopWeatherCondition.FOGGY for r in reports)
        ghat_cautions = [r.ghat_caution for r in reports if r.ghat_caution]

        overall_caution: str
        if status == WeatherAdvisoryStatus.WEATHER_UNAVAILABLE:
            overall_caution = "Weather forecast service temporarily unavailable; proceeding with regional defaults."
        elif status == WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON:
            overall_caution = (
                "Requested stops are exceeding the 16 day meteorological forecast horizon. "
                "Live forecasts are unavailable; check back closer to departure for live conditions. "
                "Historical averages are not presented as live forecasts."
            )
        elif status == WeatherAdvisoryStatus.PARTIAL:
            overall_caution = (
                f"Partial weather advisory: live forecasts available for {len(valid_reports)} of {len(reports)} stops; "
                "remaining stops are beyond forecast horizon or unavailable."
            )
        elif has_severe_monsoon:
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
        elif all(r.condition == StopWeatherCondition.SUNNY for r in valid_reports):
            overall_caution = "Favorable sunny weather conditions expected along the corridor."
        else:
            overall_caution = "Expected weather conditions sampled along the corridor."

        combined_ghat_caution = " ".join(dict.fromkeys(ghat_cautions)) if ghat_cautions else None

        return CorridorWeatherAdvisoryReport(
            status=status,
            stops=reports,
            overall_caution=overall_caution,
            ghat_caution=combined_ghat_caution,
            provider="open_meteo",
            attribution=OPEN_METEO_ATTRIBUTION,
            licensing_status=licensing_status,
            is_commercial_licensed=is_commercial,
            forecast_horizon_days=MAX_FORECAST_HORIZON_DAYS,
            diagnostics={
                "stops_queried": len(reports),
                "valid_stops": len(valid_reports),
                "horizon_exceeded_stops": len(horizon_exceeded_reports),
                "error_stops": len(error_reports),
            },
        )
