"""Unit tests for trip planner weather advisory graph wiring and resilience.

Verifies:
- Graph compilation includes FETCH_WEATHER node between CONSUME_ROUTE_RESULT and COMPUTE_BUDGET
- fetch_weather_node queries WeatherClientPort with actual schedule stops, arrival times, and timezone
- 16-day meteorological horizon handling returns OUT_OF_FORECAST_HORIZON without failing proposal
- Preserves severe monsoon cautions, ghat cautions, partial results, and Open Meteo attribution
- Fail-soft resilience on weather provider outage/timeout with honest diagnostics
- Routing unavailable scenario samples stops without inventing road schedule or coordinates
- Explicit offline fixture mode isolation without network access
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.weather import (
    CorridorWeatherAdvisoryReport,
    CorridorWeatherRequest,
    MonsoonRisk,
    StopWeatherCondition,
    StopWeatherReport,
    WeatherAdvisoryStatus,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningNode,
    TripPlanningState,
    build_trip_planning_graph,
    compile_trip_planning_workflow,
    fetch_weather_node,
    synthesize_proposal_node,
)
from functions.orchestrator.weather.port import WeatherClientPort


def _make_brief_and_schedule(
    departure_date: date | None = None,
    timezone: str = "Asia/Kolkata",
) -> tuple[dict[str, Any], dict[str, Any]]:
    dep_date = departure_date or (date.today() + timedelta(days=2))
    brief = {
        "origin_name": "Kolkata",
        "origin_lat": 22.5726,
        "origin_lng": 88.3639,
        "destinations": [
            {
                "name": "Digha Beach",
                "latitude": 21.6266,
                "longitude": 87.5074,
                "confidence": 1.0,
                "stay_days": 1,
            }
        ],
        "start_date": dep_date.isoformat(),
        "end_date": (dep_date + timedelta(days=2)).isoformat(),
        "timezone": timezone,
    }

    t0 = datetime(dep_date.year, dep_date.month, dep_date.day, 8, 0, tzinfo=UTC)
    t1_arr = datetime(dep_date.year, dep_date.month, dep_date.day, 12, 30, tzinfo=UTC)
    t1_dep = datetime(dep_date.year, dep_date.month, dep_date.day, 16, 0, tzinfo=UTC)

    optimized_schedule = {
        "stops": [
            {
                "position": 0,
                "name": "Kolkata",
                "latitude": 22.5726,
                "longitude": 88.3639,
                "arrival_time": t0.isoformat(),
                "departure_time": t0.isoformat(),
                "stay_duration_minutes": 0,
                "is_hours_assumed": False,
                "hours_status": "ORIGIN",
            },
            {
                "position": 1,
                "name": "Digha Beach",
                "latitude": 21.6266,
                "longitude": 87.5074,
                "arrival_time": t1_arr.isoformat(),
                "departure_time": t1_dep.isoformat(),
                "stay_duration_minutes": 210,
                "is_hours_assumed": False,
                "hours_status": "VERIFIED_HOURS",
            },
        ],
        "legs": [],
        "total_travel_minutes": 270,
        "is_optimal": True,
    }
    return brief, optimized_schedule


class TestTripPlannerWeatherWiring:
    """Verifies LangGraph weather node wiring, contract execution, and resilience."""

    def test_graph_compilation_includes_fetch_weather_node(self) -> None:
        graph = build_trip_planning_graph()
        assert TripPlanningNode.CONSUME_ROUTE_RESULT in graph.nodes
        assert TripPlanningNode.FETCH_WEATHER in graph.nodes
        assert TripPlanningNode.COMPUTE_BUDGET in graph.nodes
        assert TripPlanningNode.SYNTHESIZE_PROPOSAL in graph.nodes

        workflow = compile_trip_planning_workflow()
        assert workflow is not None

    @pytest.mark.asyncio
    async def test_fetch_weather_queries_client_with_schedule_stops_and_times(self) -> None:
        brief, schedule = _make_brief_and_schedule()
        mock_weather_client = MagicMock(spec=WeatherClientPort)

        captured_request: CorridorWeatherRequest | None = None

        async def _fake_get_corridor(req: CorridorWeatherRequest) -> CorridorWeatherAdvisoryReport:
            nonlocal captured_request
            captured_request = req
            return CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.AVAILABLE,
                stops=[
                    StopWeatherReport(
                        stop_name="Kolkata",
                        latitude=22.5726,
                        longitude=88.3639,
                        temperature_celsius=29.0,
                        condition=StopWeatherCondition.SUNNY,
                        monsoon_risk=MonsoonRisk.LOW,
                    ),
                    StopWeatherReport(
                        stop_name="Digha Beach",
                        latitude=21.6266,
                        longitude=87.5074,
                        temperature_celsius=27.5,
                        condition=StopWeatherCondition.SUNNY,
                        monsoon_risk=MonsoonRisk.LOW,
                    ),
                ],
                overall_caution="Favorable sunny weather conditions expected along the corridor.",
                attribution="Weather data by Open Meteo.com",
            )

        mock_weather_client.get_corridor_weather = AsyncMock(side_effect=_fake_get_corridor)

        state: TripPlanningState = {
            "trip_id": "test-trip-001",
            "brief": brief,
            "optimized_schedule": schedule,
            "_weather_client": mock_weather_client,
        }

        res = await fetch_weather_node(state)

        assert res["current_step"] == "weather_fetched"
        assert res["weather_status"] == "AVAILABLE"
        assert len(res["weather_advisories"]) == 2
        assert captured_request is not None
        assert len(captured_request.stops) == 2
        assert captured_request.stops[0].name == "Kolkata"
        assert captured_request.stops[0].arrival_time is not None
        assert captured_request.stops[1].name == "Digha Beach"
        assert captured_request.stops[1].arrival_time is not None
        assert captured_request.timezone == "Asia/Kolkata"

        # Check diagnostics populated
        diag = res["provider_diagnostics"]["weather"]
        assert diag["attribution"] == "Weather data by Open Meteo.com"
        assert diag["overall_caution"] is not None

    @pytest.mark.asyncio
    async def test_fetch_weather_preserves_16_day_horizon_policy(self) -> None:
        distant_date = date.today() + timedelta(days=25)
        brief, schedule = _make_brief_and_schedule(departure_date=distant_date)

        mock_weather_client = MagicMock(spec=WeatherClientPort)
        mock_weather_client.get_corridor_weather = AsyncMock(
            return_value=CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.OUT_OF_FORECAST_HORIZON,
                stops=[
                    StopWeatherReport(
                        stop_name="Kolkata",
                        latitude=22.5726,
                        longitude=88.3639,
                        condition=StopWeatherCondition.UNKNOWN,
                        monsoon_risk=MonsoonRisk.UNKNOWN,
                        diagnostics={"status": "OUT_OF_FORECAST_HORIZON"},
                    ),
                    StopWeatherReport(
                        stop_name="Digha Beach",
                        latitude=21.6266,
                        longitude=87.5074,
                        condition=StopWeatherCondition.UNKNOWN,
                        monsoon_risk=MonsoonRisk.UNKNOWN,
                        diagnostics={"status": "OUT_OF_FORECAST_HORIZON"},
                    ),
                ],
                overall_caution="Requested stops are exceeding the 16 day meteorological forecast horizon. Live forecasts are unavailable; check back closer to departure for live conditions. Historical averages are not presented as live forecasts.",
                attribution="Weather data by Open Meteo.com",
            )
        )

        state: TripPlanningState = {
            "trip_id": "test-trip-distant",
            "brief": brief,
            "optimized_schedule": schedule,
            "_weather_client": mock_weather_client,
        }

        res = await fetch_weather_node(state)

        assert res["weather_status"] == "OUT_OF_FORECAST_HORIZON"
        assert len(res["weather_advisories"]) == 2
        for adv in res["weather_advisories"]:
            assert adv["condition"] == "unknown"
            assert adv["monsoon_risk"] == "unknown"
            assert "16 day" in adv["overall_caution"]
            assert adv["attribution"] == "Weather data by Open Meteo.com"

        # Ensure synthesize proposal integrates the out of horizon advisory
        merged_state = dict(state)
        merged_state.update(res)
        prop_res = synthesize_proposal_node(cast(TripPlanningState, merged_state))
        prop = prop_res["proposal"]
        assert prop["weather_status"] == "OUT_OF_FORECAST_HORIZON"
        assert len(prop["weather_advisories"]) == 2

    @pytest.mark.asyncio
    async def test_fetch_weather_preserves_severe_monsoon_and_ghat_cautions(self) -> None:
        brief, schedule = _make_brief_and_schedule()
        mock_weather_client = MagicMock(spec=WeatherClientPort)

        mock_weather_client.get_corridor_weather = AsyncMock(
            return_value=CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.PARTIAL,
                stops=[
                    StopWeatherReport(
                        stop_name="Kolkata",
                        latitude=22.5726,
                        longitude=88.3639,
                        condition=StopWeatherCondition.CLOUDY,
                        monsoon_risk=MonsoonRisk.MODERATE,
                    ),
                    StopWeatherReport(
                        stop_name="Rohini Ghat Pass",
                        latitude=26.85,
                        longitude=88.28,
                        condition=StopWeatherCondition.RAINY,
                        monsoon_risk=MonsoonRisk.SEVERE,
                        ghat_caution="Severe rainfall warning along ghat section; high risk of waterlogging, rockfall, and slick road surfaces.",
                    ),
                ],
                overall_caution="Severe monsoon conditions detected along corridor stops. Partial weather advisory: live forecasts available for 2 of 2 stops.",
                ghat_caution="Severe rainfall warning along ghat section; high risk of waterlogging, rockfall, and slick road surfaces.",
                attribution="Weather data by Open Meteo.com",
            )
        )

        state: TripPlanningState = {
            "trip_id": "test-trip-severe",
            "brief": brief,
            "optimized_schedule": schedule,
            "_weather_client": mock_weather_client,
        }

        res = await fetch_weather_node(state)

        assert res["weather_status"] == "PARTIAL"
        advisories = res["weather_advisories"]
        assert len(advisories) == 2
        assert advisories[1]["monsoon_risk"] == "severe"
        assert "ghat section" in advisories[1]["ghat_caution"]
        assert res["provider_diagnostics"]["weather"]["ghat_caution"] is not None

    @pytest.mark.asyncio
    async def test_fetch_weather_fails_soft_on_provider_outage(self) -> None:
        brief, schedule = _make_brief_and_schedule()
        mock_weather_client = MagicMock(spec=WeatherClientPort)
        mock_weather_client.get_corridor_weather = AsyncMock(
            side_effect=RuntimeError("Open Meteo 503 Service Unavailable")
        )

        state: TripPlanningState = {
            "trip_id": "test-trip-outage",
            "brief": brief,
            "optimized_schedule": schedule,
            "_weather_client": mock_weather_client,
        }

        res = await fetch_weather_node(state)

        assert res["current_step"] == "weather_fetched"
        assert res["weather_status"] == "WEATHER_UNAVAILABLE"
        assert res["weather_advisories"] == []
        diag = res["provider_diagnostics"]["weather"]
        assert diag["status"] == "WEATHER_UNAVAILABLE"
        assert "503" in diag["error"]
        assert diag["attribution"] == "Weather data by Open Meteo.com"

        # Verify proposal still synthesizes without crashing
        merged_state = dict(state)
        merged_state.update(res)
        prop_res = synthesize_proposal_node(cast(TripPlanningState, merged_state))
        prop = prop_res["proposal"]
        assert prop["weather_status"] == "WEATHER_UNAVAILABLE"
        assert prop["status"] == "ready_for_approval"

    @pytest.mark.asyncio
    async def test_fetch_weather_routing_unavailable_without_inventing_schedule_or_coordinates(
        self,
    ) -> None:
        brief, _ = _make_brief_and_schedule()
        mock_weather_client = MagicMock(spec=WeatherClientPort)

        captured_request: CorridorWeatherRequest | None = None

        async def _fake_get_corridor(req: CorridorWeatherRequest) -> CorridorWeatherAdvisoryReport:
            nonlocal captured_request
            captured_request = req
            return CorridorWeatherAdvisoryReport(
                status=WeatherAdvisoryStatus.AVAILABLE,
                stops=[
                    StopWeatherReport(
                        stop_name="Victoria Memorial",
                        latitude=22.5448,
                        longitude=88.3426,
                        temperature_celsius=29.0,
                        condition=StopWeatherCondition.SUNNY,
                        monsoon_risk=MonsoonRisk.LOW,
                    )
                ],
                overall_caution="Expected weather conditions sampled along the corridor.",
                attribution="Weather data by Open Meteo.com",
            )

        mock_weather_client.get_corridor_weather = AsyncMock(side_effect=_fake_get_corridor)

        # Invariant: routing is unavailable, so optimized_schedule has empty stops
        state: TripPlanningState = {
            "trip_id": "test-trip-no-route",
            "brief": brief,
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_status": "ROUTING_UNAVAILABLE",
            "optimized_schedule": {
                "stops": [],
                "legs": [],
                "total_travel_minutes": 0,
                "is_optimal": False,
            },
            "eligible_stops": [
                {
                    "primary_name": "Victoria Memorial",
                    "point": {"lat": 22.5448, "lng": 88.3426},
                    "is_candidate": False,
                }
            ],
            "_weather_client": mock_weather_client,
        }

        res = await fetch_weather_node(state)

        assert res["weather_status"] == "AVAILABLE"
        assert captured_request is not None
        assert len(captured_request.stops) == 1
        # Critical assertion: arrival_time and departure_time are None, NOT invented
        assert captured_request.stops[0].arrival_time is None
        assert captured_request.stops[0].departure_time is None
        assert captured_request.stops[0].latitude == 22.5448
        assert captured_request.stops[0].longitude == 88.3426

    @pytest.mark.asyncio
    async def test_fetch_weather_no_coordinates_degrades_honestly_without_inventing_coords(
        self,
    ) -> None:
        mock_weather_client = MagicMock(spec=WeatherClientPort)

        state: TripPlanningState = {
            "trip_id": "test-trip-no-coords",
            "brief": {
                "origin_name": "Unknown",
                "origin_lat": 0.0,
                "origin_lng": 0.0,
                "destinations": [],
            },
            "optimized_schedule": {"stops": []},
            "eligible_stops": [],
            "_weather_client": mock_weather_client,
        }

        res = await fetch_weather_node(state)

        assert res["weather_status"] == "WEATHER_UNAVAILABLE"
        assert res["weather_advisories"] == []
        assert "No stops with coordinates" in res["provider_diagnostics"]["weather"]["error"]
        assert mock_weather_client.get_corridor_weather.call_count == 0

    @pytest.mark.asyncio
    async def test_offline_fixture_mode_uses_fixture_client(self) -> None:
        target_date = date.today() + timedelta(days=2)
        brief, schedule = _make_brief_and_schedule(departure_date=target_date)

        state: TripPlanningState = {
            "trip_id": "test-trip-fixture",
            "brief": brief,
            "optimized_schedule": schedule,
            "_offline_fixture_mode": True,
        }

        res = await fetch_weather_node(state)

        assert res["current_step"] == "weather_fetched"
        assert res["weather_status"] in ("AVAILABLE", "PARTIAL", "OUT_OF_FORECAST_HORIZON")
        assert len(res["weather_advisories"]) > 0
        diag = res["provider_diagnostics"]["weather"]
        assert diag["provider"] == "fixture"
        assert diag["attribution"] == "Weather data by Open Meteo.com"
