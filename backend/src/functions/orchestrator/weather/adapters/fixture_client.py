"""Deterministic offline fixture adapter for weather client port.

Provides hermetic weather responses without network calls or external APIs,
enabling zero cost local execution, reproducible tests, and continuous integration.
"""

from __future__ import annotations

import json
import os
import zoneinfo
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

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

MAX_FORECAST_HORIZON_DAYS: int = 16
OPEN_METEO_ATTRIBUTION: str = "Weather data by Open Meteo.com"


class FixtureWeatherClient(WeatherClientPort):
    """Offline deterministic fixture adapter implementing WeatherClientPort."""

    def __init__(
        self,
        fixture_dir: Path | str | None = None,
        commercial_license: bool | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        simulate_timeout: bool = False,
        simulate_error: Exception | None = None,
        simulate_unavailable: bool = False,
        enforce_horizon_check: bool = True,
    ) -> None:
        self.commercial_license = commercial_license
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.simulate_timeout = simulate_timeout
        self.simulate_error = simulate_error
        self.simulate_unavailable = simulate_unavailable
        self.enforce_horizon_check = enforce_horizon_check
        self._fixture_dir = self._resolve_fixture_dir(fixture_dir)

    def _resolve_licensing_status(self) -> tuple[CommercialLicenseStatus, bool | None]:
        """Resolves commercial licensing status for fixture client."""
        if self.commercial_license is True:
            return CommercialLicenseStatus.COMMERCIAL_LICENSED, True
        if self.commercial_license is False:
            return CommercialLicenseStatus.NON_COMMERCIAL, False
        return CommercialLicenseStatus.NON_COMMERCIAL, False

    def _resolve_stop_target_date(
        self,
        stop: WeatherStopInput,
        default_departure: date | datetime,
        tz: zoneinfo.ZoneInfo,
    ) -> date:
        """Resolves target local date for a stop considering arrival or departure timestamps."""
        if stop.arrival_time is not None:
            arrival_dt = (
                stop.arrival_time.replace(tzinfo=tz)
                if stop.arrival_time.tzinfo is None
                else stop.arrival_time.astimezone(tz)
            )
            return arrival_dt.date()
        if stop.departure_time is not None:
            dep_dt = (
                stop.departure_time.replace(tzinfo=tz)
                if stop.departure_time.tzinfo is None
                else stop.departure_time.astimezone(tz)
            )
            return dep_dt.date()
        return (
            default_departure.date()
            if isinstance(default_departure, datetime)
            else default_departure
        )

    @classmethod
    def _resolve_fixture_dir(cls, explicit_dir: Path | str | None) -> Path:
        """Resolves the fixture directory using arguments, environment, or relative candidate paths."""
        if explicit_dir is not None:
            return Path(explicit_dir)

        env_dir = os.environ.get("WEATHER_FIXTURE_DIR")
        if env_dir and env_dir.strip():
            return Path(env_dir.strip())

        current_file = Path(__file__).resolve()
        candidate_paths: list[Path] = []
        if len(current_file.parents) >= 6:
            candidate_paths.append(current_file.parents[5] / "tests" / "fixtures" / "weather")
        candidate_paths.append(current_file.parent.parent / "fixtures")

        for candidate in candidate_paths:
            if candidate.is_dir():
                return candidate

        return candidate_paths[0] if candidate_paths else current_file.parent

    def _load_json_fixture(self, filename: str) -> dict[str, Any]:
        """Loads and parses a JSON fixture file."""
        file_path = self._fixture_dir / filename
        if not file_path.is_file():
            raise FileNotFoundError(
                f"Weather fixture file '{filename}' was not found in directory '{self._fixture_dir}'. "
                "Ensure tests/fixtures/weather exists or configure WEATHER_FIXTURE_DIR."
            )
        with open(file_path, encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))

    def _check_horizon(self, req_date: date, tz_str: str) -> tuple[bool, str | None]:
        """Checks if requested date is within 16 day meteorological forecast horizon."""
        try:
            tz = zoneinfo.ZoneInfo(tz_str)
        except Exception:
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")

        today = datetime.now(tz).date()
        max_date = today + timedelta(days=MAX_FORECAST_HORIZON_DAYS)

        if req_date > max_date:
            days_out = (req_date - today).days
            return (
                False,
                f"Departure date ({req_date}) is {days_out} days out, exceeding the "
                f"{MAX_FORECAST_HORIZON_DAYS} day meteorological forecast horizon. Live forecasts "
                "are unavailable; check back closer to departure for live conditions. "
                "Historical averages are not presented as live forecasts.",
            )

        if req_date < today - timedelta(days=1):
            return (
                False,
                f"Requested date ({req_date}) is in the past. Live forecast models only "
                "provide current and forward looking predictions.",
            )

        return True, None

    async def get_corridor_weather(
        self, request: CorridorWeatherRequest
    ) -> CorridorWeatherAdvisoryReport:
        """Returns deterministic corridor weather report from fixtures."""
        if self.simulate_timeout:
            raise TimeoutError("Simulated weather client socket timeout")

        if self.simulate_error is not None:
            raise self.simulate_error

        licensing_status, is_commercial = self._resolve_licensing_status()

        if self.simulate_unavailable:
            return CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.WEATHER_UNAVAILABLE,
                stops=[
                    StopWeatherReport(
                        stop_name=s.name,
                        latitude=s.latitude,
                        longitude=s.longitude,
                        condition=StopWeatherCondition.UNKNOWN,
                        monsoon_risk=MonsoonRisk.UNKNOWN,
                    )
                    for s in request.stops
                ],
                overall_caution="Weather service temporarily unavailable; proceeding with regional defaults.",
                provider="fixture",
                attribution=OPEN_METEO_ATTRIBUTION,
                licensing_status=licensing_status,
                is_commercial_licensed=is_commercial,
            )

        try:
            tz = zoneinfo.ZoneInfo(request.timezone)
        except Exception:
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")

        # Check forecast horizon policy per stop if enabled
        if self.enforce_horizon_check:
            horizon_checks = [
                (
                    s,
                    self._resolve_stop_target_date(s, request.departure_date, tz),
                    self._check_horizon(
                        self._resolve_stop_target_date(s, request.departure_date, tz),
                        request.timezone,
                    ),
                )
                for s in request.stops
            ]
            valid_checks = [c for c in horizon_checks if c[2][0]]
            invalid_checks = [c for c in horizon_checks if not c[2][0]]

            if not valid_checks:
                # All stops exceed forecast horizon
                return CorridorWeatherAdvisoryReport(
                    status=WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON,
                    stops=[
                        StopWeatherReport(
                            stop_name=s.name,
                            latitude=s.latitude,
                            longitude=s.longitude,
                            condition=StopWeatherCondition.UNKNOWN,
                            monsoon_risk=MonsoonRisk.UNKNOWN,
                            diagnostics={
                                "status": "OUT_OF_FORECAST_HORIZON",
                                "target_date": str(target_dt),
                                "reason": reason,
                            },
                        )
                        for s, target_dt, (_, reason) in horizon_checks
                    ],
                    overall_caution=(
                        "Requested stops are beyond the 16 day meteorological forecast horizon. "
                        "Live forecasts are unavailable; check back closer to departure for live conditions. "
                        "Historical averages are not presented as live forecasts."
                    ),
                    provider="fixture",
                    attribution=OPEN_METEO_ATTRIBUTION,
                    licensing_status=licensing_status,
                    is_commercial_licensed=is_commercial,
                    forecast_horizon_days=MAX_FORECAST_HORIZON_DAYS,
                    diagnostics={"horizon_check_failed": True},
                )

            if invalid_checks:
                # Partial: some stops exceed horizon
                out_stops = [
                    StopWeatherReport(
                        stop_name=s.name,
                        latitude=s.latitude,
                        longitude=s.longitude,
                        condition=StopWeatherCondition.UNKNOWN,
                        monsoon_risk=MonsoonRisk.UNKNOWN,
                        diagnostics={
                            "status": "OUT_OF_FORECAST_HORIZON",
                            "target_date": str(target_dt),
                            "reason": reason,
                        },
                    )
                    for s, target_dt, (_, reason) in invalid_checks
                ]
                stop_names_lower = [s.name.lower() for s in request.stops]
                any_ghat = any(
                    s.is_ghat_section
                    or any(k in s.name.lower() for k in ("ghat", "pass", "mountain", "hill"))
                    for s in request.stops
                )
                if any_ghat or any("monsoon" in name for name in stop_names_lower):
                    valid_sample = self._load_json_fixture("weather_monsoon_ghat.json")
                else:
                    valid_sample = self._load_json_fixture("weather_sunny_corridor.json")

                base_report = CorridorWeatherAdvisoryReport.model_validate(valid_sample)
                combined_stops = base_report.stops[: len(valid_checks)] + out_stops

                has_severe = any(r.monsoon_risk == MonsoonRisk.SEVERE for r in combined_stops)
                has_fog = any(r.condition == StopWeatherCondition.FOGGY for r in combined_stops)
                hazard_prefix = ""
                if has_severe:
                    hazard_prefix = (
                        "Severe monsoon conditions detected along corridor stops. "
                        "Potential travel delays and localized waterlogging expected. "
                    )
                elif has_fog:
                    hazard_prefix = (
                        "Patchy fog expected along sections of the corridor. "
                        "Reduced driving visibility possible during morning hours. "
                    )

                partial_notice = (
                    f"Partial weather advisory: live forecasts available for {len(valid_checks)} of "
                    f"{len(request.stops)} stops; remaining stops are beyond forecast horizon or unavailable."
                )
                return CorridorWeatherAdvisoryReport(
                    status=WeatherAdvisoryStatus.PARTIAL,
                    stops=combined_stops,
                    overall_caution=f"{hazard_prefix}{partial_notice}".strip(),
                    ghat_caution=base_report.ghat_caution if any_ghat else None,
                    provider="fixture",
                    attribution=OPEN_METEO_ATTRIBUTION,
                    licensing_status=licensing_status,
                    is_commercial_licensed=is_commercial,
                    forecast_horizon_days=MAX_FORECAST_HORIZON_DAYS,
                    diagnostics={"partial": True, "valid_stops": len(valid_checks)},
                )

        stop_names_lower = [s.name.lower() for s in request.stops]
        any_ghat = any(
            s.is_ghat_section
            or any(k in s.name.lower() for k in ("ghat", "pass", "mountain", "hill"))
            for s in request.stops
        )

        if "empty" in stop_names_lower:
            data = self._load_json_fixture("weather_empty.json")
            report = CorridorWeatherAdvisoryReport.model_validate(data)
            return report.model_copy(
                update={
                    "licensing_status": licensing_status,
                    "is_commercial_licensed": is_commercial,
                }
            )

        if any_ghat or any("monsoon" in name for name in stop_names_lower):
            data = self._load_json_fixture("weather_monsoon_ghat.json")
            report = CorridorWeatherAdvisoryReport.model_validate(data)
            return report.model_copy(
                update={
                    "licensing_status": licensing_status,
                    "is_commercial_licensed": is_commercial,
                }
            )

        if any("sunny" in name for name in stop_names_lower):
            data = self._load_json_fixture("weather_sunny_corridor.json")
            report = CorridorWeatherAdvisoryReport.model_validate(data)
            return report.model_copy(
                update={
                    "licensing_status": licensing_status,
                    "is_commercial_licensed": is_commercial,
                }
            )

        data = self._load_json_fixture("weather_kolkata_digha.json")
        report = CorridorWeatherAdvisoryReport.model_validate(data)
        return report.model_copy(
            update={"licensing_status": licensing_status, "is_commercial_licensed": is_commercial}
        )

    async def get_stop_weather(
        self,
        stop: WeatherStopInput,
        target_date: date | datetime,
        timezone: str = "Asia/Kolkata",
    ) -> StopWeatherReport:
        """Returns deterministic stop weather evaluated against fixtures."""
        corridor_req = CorridorWeatherRequest(
            departure_date=target_date,
            stops=[stop],
            timezone=timezone,
        )
        report = await self.get_corridor_weather(corridor_req)
        if report.stops:
            return report.stops[0]

        return StopWeatherReport(
            stop_name=stop.name,
            latitude=stop.latitude,
            longitude=stop.longitude,
            condition=StopWeatherCondition.UNKNOWN,
            monsoon_risk=MonsoonRisk.UNKNOWN,
        )
