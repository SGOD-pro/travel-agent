"""Unit tests for QueryPlanningService conforming to spec 0007 AC-13."""

from __future__ import annotations

from datetime import date

from contracts.trips import DestinationPoint, TripBrief
from functions.orchestrator.evidence.query_planner import (
    DEFAULT_COST_PER_CALL_MINOR,
    DEFAULT_MAX_QUERIES,
    QueryPlanningService,
)


def test_plan_destination_queries_from_trip_brief() -> None:
    brief = TripBrief(
        origin_name="Kolkata",
        origin_lat=22.5726,
        origin_lng=88.3639,
        destinations=[
            DestinationPoint(
                name="Darjeeling",
                latitude=27.0410,
                longitude=88.2663,
                stay_days=2,
            )
        ],
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 4),
        dietary_preferences=["vegetarian"],
    )

    queries = QueryPlanningService.plan_destination_queries(brief, max_queries=4)

    assert len(queries) <= 4
    assert any("Darjeeling" in q and "attractions" in q for q in queries)
    assert any("Darjeeling" in q and "food" in q for q in queries)
    assert any("Darjeeling" in q and "hotels" in q for q in queries)
    assert any("travel advisory" in q or "route conditions" in q for q in queries)


def test_plan_destination_queries_from_dict() -> None:
    brief_dict = {
        "origin_name": "Siliguri",
        "destinations": [{"name": "Gangtok", "latitude": 27.3389, "longitude": 88.6065}],
        "dietary_preferences": [],
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)

    assert len(queries) == 4
    assert any("Gangtok" in q and "attractions" in q for q in queries)
    assert any("Gangtok" in q and "hotels" in q for q in queries)


def test_plan_destination_queries_enforces_ceiling() -> None:
    brief_dict = {
        "origin_name": "Bengaluru",
        "destinations": [
            {"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394},
            {"name": "Coorg", "latitude": 12.3375, "longitude": 75.8069},
        ],
    }

    # Even if requesting 10 queries, capped at 4
    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=10)
    assert len(queries) == DEFAULT_MAX_QUERIES


def test_calculate_collector_budget() -> None:
    queries = [
        "Darjeeling attractions",
        "Darjeeling food",
        "Darjeeling hotels",
        "Kolkata to Darjeeling route",
    ]

    budget = QueryPlanningService.calculate_collector_budget(queries)

    assert budget["remaining_tool_calls"] == 4
    assert budget["cost_per_call_minor"] == DEFAULT_COST_PER_CALL_MINOR  # 50
    assert budget["remaining_cost_minor"] == 200
    assert budget["currency"] == "INR"


def test_plan_destination_queries_incorporates_dates_preferences_and_budget() -> None:
    brief_dict = {
        "origin_name": "Kolkata",
        "destinations": [{"name": "Shillong", "latitude": 25.5788, "longitude": 91.8933}],
        "start_date": "2026-11-20",
        "end_date": "2026-11-25",
        "traveler_preferences": ["indigenous khasi cuisine", "pine forest walks"],
        "budget_tier": "LUXURY",
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)

    assert len(queries) == 4
    # Food query incorporates culinary preference
    assert any("Shillong" in q and "cuisine" in q and "food" in q for q in queries)
    # Accommodation query incorporates luxury budget tier and November date context
    assert any("Shillong" in q and "luxury" in q and "hotels" in q and "November" in q for q in queries)
    # Attractions and route conditions present
    assert any("Shillong" in q and "attractions" in q for q in queries)
    assert any("road travel advisory" in q for q in queries)

