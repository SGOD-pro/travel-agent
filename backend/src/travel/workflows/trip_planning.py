"""LangGraph Workflow for End-to-End Trip Planning & Proposal Generation.

Orchestrates:
1. Brief validation and day allocation.
2. Candidate discovery (POIs, corridors).
3. OR-Tools constraint-based routing & scheduling.
4. Fuel math and road budget compilation (preserving unknown tolls).
5. Proposal synthesis ready for human approval.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from travel.application.services.scheduler import ItineraryScheduler
from travel.domain.money import BudgetCategory, BudgetLine, BudgetSummary, Currency
from travel.domain.routing import RoadBudgetCalculator, RoadSegment, VehicleFuelProfile
from travel.domain.trips import TransportMode, TripBrief


class TripPlanningState(TypedDict, total=False):
    trip_id: str
    version: int
    brief: dict[str, Any]
    discovered_places: list[dict[str, Any]]
    optimized_schedule: dict[str, Any]
    budget_lines: list[dict[str, Any]]
    budget_summary: dict[str, Any]
    proposal: dict[str, Any]
    current_step: str
    status: str
    errors: list[str]


# --- Workflow Nodes ---


def parse_and_validate_brief_node(state: TripPlanningState) -> dict[str, Any]:
    """Validates the input brief against the canonical TripBrief domain contract."""
    brief_data = state["brief"]
    try:
        brief = TripBrief.model_validate(brief_data)
        return {
            "current_step": "brief_validated",
            "status": "in_progress",
            "brief": brief.model_dump(mode="json"),
        }
    except Exception as e:
        return {
            "status": "failed",
            "errors": [f"Brief validation failed: {str(e)}"],
        }


def discover_candidates_node(state: TripPlanningState) -> dict[str, Any]:
    """Discovers transit corridors and destination points."""
    brief = TripBrief.model_validate(state["brief"])
    discovered: list[dict[str, Any]] = []

    for d in brief.destinations:
        discovered.append(
            {
                "name": d.name,
                "lat": d.latitude,
                "lng": d.longitude,
                "confidence": d.confidence,
                "stay_minutes": d.stay_days * 180,  # 3 hours per stay day
            }
        )

    return {
        "current_step": "candidates_discovered",
        "discovered_places": discovered,
    }


def solve_schedule_node(state: TripPlanningState) -> dict[str, Any]:
    """Applies OR-Tools scheduling solver to generate optimal sequence and timestamps."""
    brief = TripBrief.model_validate(state["brief"])
    places = state.get("discovered_places", [])

    start_dt = datetime.combine(brief.start_date, time(hour=9, minute=0), tzinfo=UTC)

    schedule = ItineraryScheduler.solve_schedule(
        origin_name=brief.origin_name,
        origin_lat=brief.origin_lat,
        origin_lng=brief.origin_lng,
        stops=places,
        start_datetime=start_dt,
    )

    return {
        "current_step": "schedule_optimized",
        "optimized_schedule": schedule.model_dump(mode="json"),
    }


def compute_budget_node(state: TripPlanningState) -> dict[str, Any]:
    """Calculates fuel and road transit budget using exact domain math."""
    brief = TripBrief.model_validate(state["brief"])
    schedule_data = state.get("optimized_schedule", {})
    legs_data = schedule_data.get("legs", [])

    mode = brief.preferred_modes[0] if brief.preferred_modes else TransportMode.CAR_PETROL
    profile = VehicleFuelProfile.default_for_mode(mode)

    all_budget_lines: list[BudgetLine] = []

    # 1. Transport & Tolls for each leg
    for leg in legs_data:
        segment = RoadSegment(
            origin_name=leg["from_stop"],
            destination_name=leg["to_stop"],
            distance_km=Decimal(str(leg["distance_km"])),
            duration_minutes=leg["duration_minutes"],
            toll_charges=None,  # Unverified toll -> explicit unknown BudgetLine
            toll_unknown_reason=f"Toll rates unverified between {leg['from_stop']} and {leg['to_stop']}",
        )
        lines = RoadBudgetCalculator.calculate_segment_budget(segment, profile)
        all_budget_lines.extend(lines)

    # 2. Daily Estimated Accommodation
    for d in brief.destinations:
        all_budget_lines.append(
            BudgetLine(
                category=BudgetCategory.ACCOMMODATION,
                amount=Decimal("3500.00"),
                currency=Currency.INR,
                basis=f"hotel_stay_{d.name} ({brief.rooms} rooms, {d.stay_days} nights)",
                quantity=Decimal(str(brief.rooms * d.stay_days)),
                source_ref="benchmark_hotel_estimate",
                estimated=True,
            )
        )

    summary = BudgetSummary.from_lines(all_budget_lines, currency=Currency.INR)

    return {
        "current_step": "budget_computed",
        "budget_lines": [line.model_dump(mode="json") for line in all_budget_lines],
        "budget_summary": summary.model_dump(mode="json"),
    }


def synthesize_proposal_node(state: TripPlanningState) -> dict[str, Any]:
    """Assembles the final validated travel proposal."""
    proposal = {
        "trip_id": state.get("trip_id"),
        "version": state.get("version", 1),
        "schedule": state.get("optimized_schedule"),
        "budget_summary": state.get("budget_summary"),
        "budget_lines": state.get("budget_lines"),
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "ready_for_approval",
    }

    return {
        "current_step": "proposal_synthesized",
        "status": "completed",
        "proposal": proposal,
    }


# --- Graph Construction ---


def build_trip_planning_graph() -> StateGraph[TripPlanningState, Any, Any, Any]:
    graph: StateGraph[TripPlanningState, Any, Any, Any] = StateGraph(TripPlanningState)

    graph.add_node("parse_brief", parse_and_validate_brief_node)
    graph.add_node("discover_candidates", discover_candidates_node)
    graph.add_node("solve_schedule", solve_schedule_node)
    graph.add_node("compute_budget", compute_budget_node)
    graph.add_node("synthesize_proposal", synthesize_proposal_node)

    graph.add_edge(START, "parse_brief")
    graph.add_edge("parse_brief", "discover_candidates")
    graph.add_edge("discover_candidates", "solve_schedule")
    graph.add_edge("solve_schedule", "compute_budget")
    graph.add_edge("compute_budget", "synthesize_proposal")
    graph.add_edge("synthesize_proposal", END)

    return graph


trip_planning_workflow = build_trip_planning_graph().compile()
