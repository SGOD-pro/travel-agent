"""LangGraph Workflow for End-to-End Trip Planning & Proposal Generation.

Orchestrates:
1. Brief validation and day allocation.
2. Candidate discovery (POIs, corridors).
3. OR-Tools constraint-based routing & scheduling.
4. Fuel math and road budget compilation (preserving unknown tolls).
5. Proposal synthesis ready for human approval.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from contracts.browser import BrowserTaskPayload, BrowserWaitUntil
from contracts.money import BudgetCategory, BudgetLine, BudgetSummary, Currency
from contracts.routing import RoadBudgetCalculator, RoadSegment, VehicleFuelProfile
from contracts.trips import TransportMode, TripBrief
from functions.optimizer.solver import ItineraryScheduler

logger = logging.getLogger(__name__)


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
    execution_id: str
    task_outputs: dict[str, Any]
    browser_requests: list[dict[str, Any]]
    pending_tasks: list[dict[str, Any]]
    dispatched_browser_urls: list[str]


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


def escalate_dynamic_evidence_node(state: TripPlanningState) -> dict[str, Any]:
    """Inspects completed evidence outputs, enforces replay-safe budget limits, and enqueues browser tasks.

    Conforms to spec 0003 AC-1:
    - Inspects state['task_outputs'] for emitted browser_requests records.
    - Enforces a maximum 3-task escalation ceiling per execution.
    - Deduplicates URLs already present in state['dispatched_browser_urls'].
    - Takes unhandled requests in emission order up to available slots.
    - Enqueues pending_tasks specifications with deterministic idempotency keys:
      {execution_id}:render:{canonical_url_hash[:16]}.
    """
    task_outputs = state.get("task_outputs", {})
    already_dispatched = set(state.get("dispatched_browser_urls", []))

    # Resolve max escalation budget from environment or settings
    max_slots = 3
    env_max = os.environ.get("BROWSER_MAX_ESCALATION_TASKS")
    if env_max and env_max.isdigit():
        max_slots = int(env_max)
    else:
        try:
            from config.config import get_settings

            max_slots = int(get_settings().BROWSER_MAX_ESCALATION_TASKS)
        except Exception:
            max_slots = 3

    available_slots = max(0, max_slots - len(already_dispatched))

    if available_slots <= 0:
        return {
            "pending_tasks": [],
            "dispatched_browser_urls": list(already_dispatched),
        }

    # Extract browser_requests in emission order
    extracted_requests: list[dict[str, Any]] = []
    for output in task_outputs.values():
        if isinstance(output, dict):
            reqs = output.get("browser_requests", [])
            if isinstance(reqs, list):
                extracted_requests.extend(reqs)

    # Check top-level state if present
    if "browser_requests" in state and isinstance(state["browser_requests"], list):
        extracted_requests.extend(state["browser_requests"])

    raw_exec_id = state.get("execution_id") or state.get("trip_id")
    if raw_exec_id:
        execution_id = str(raw_exec_id)
    else:
        logger.warning(
            "Neither execution_id nor trip_id found in LangGraph state; using static fallback key prefix"
        )
        execution_id = "default-execution"
    pending_tasks: list[dict[str, Any]] = []
    newly_dispatched: list[str] = []

    for req in extracted_requests:
        if len(pending_tasks) >= available_slots:
            break

        url = req.get("url")
        canonical_url = req.get("canonical_url", url)
        if not canonical_url or not url:
            continue

        if canonical_url in already_dispatched or canonical_url in newly_dispatched:
            continue
        if url in already_dispatched or url in newly_dispatched:
            continue

        canonical_url_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        idempotency_key = f"{execution_id}:render:{canonical_url_hash[:16]}"

        raw_wait_until = req.get("wait_until", BrowserWaitUntil.NETWORKIDLE)
        try:
            wait_until = (
                raw_wait_until
                if isinstance(raw_wait_until, BrowserWaitUntil)
                else BrowserWaitUntil(str(raw_wait_until).lower())
            )
        except ValueError:
            wait_until = BrowserWaitUntil.NETWORKIDLE

        placeholder_id = req.get("placeholder_observation_id")
        if placeholder_id is not None and not isinstance(placeholder_id, uuid.UUID):
            try:
                placeholder_id = uuid.UUID(str(placeholder_id))
            except ValueError:
                placeholder_id = None

        payload = BrowserTaskPayload(
            url=url,
            canonical_url=canonical_url,
            category=req.get("category", "lodging"),
            reason=req.get("reason", "DYNAMIC_JAVASCRIPT_REQUIRED"),
            wait_until=wait_until,
            timeout_seconds=int(req.get("timeout_seconds", 20)),
            selector_target=req.get("selector_target"),
            placeholder_observation_id=placeholder_id,
        )

        task_spec = {
            "worker": "BROWSER",
            "action": "render",
            "payload": payload.model_dump(mode="json"),
            "idempotency_key": idempotency_key,
            "deadline_seconds": int(req.get("deadline_seconds", 60)),
            "max_attempts": int(req.get("max_attempts", 3)),
        }

        pending_tasks.append(task_spec)
        newly_dispatched.append(canonical_url)

    updated_dispatched = list(already_dispatched | set(newly_dispatched))
    result: dict[str, Any] = {
        "pending_tasks": pending_tasks,
        "dispatched_browser_urls": updated_dispatched,
    }
    if pending_tasks:
        result["current_step"] = "dynamic_evidence_escalated"

    return result


# --- Graph Construction ---


def build_trip_planning_graph() -> StateGraph[TripPlanningState, Any, Any, Any]:
    graph: StateGraph[TripPlanningState, Any, Any, Any] = StateGraph(TripPlanningState)

    graph.add_node("parse_brief", parse_and_validate_brief_node)
    graph.add_node("discover_candidates", discover_candidates_node)
    graph.add_node("escalate_dynamic_evidence", escalate_dynamic_evidence_node)
    graph.add_node("solve_schedule", solve_schedule_node)
    graph.add_node("compute_budget", compute_budget_node)
    graph.add_node("synthesize_proposal", synthesize_proposal_node)

    graph.add_edge(START, "parse_brief")
    graph.add_edge("parse_brief", "discover_candidates")
    graph.add_edge("discover_candidates", "escalate_dynamic_evidence")
    graph.add_edge("escalate_dynamic_evidence", "solve_schedule")
    graph.add_edge("solve_schedule", "compute_budget")
    graph.add_edge("compute_budget", "synthesize_proposal")
    graph.add_edge("synthesize_proposal", END)

    return graph


trip_planning_workflow = build_trip_planning_graph().compile()
