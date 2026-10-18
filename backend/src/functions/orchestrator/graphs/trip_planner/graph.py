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
from enum import StrEnum
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from config.config import get_settings
from contracts.browser import BrowserTaskPayload, BrowserWaitUntil
from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionPort,
    LayaDecisionRequest,
)
from contracts.money import BudgetCategory, BudgetLine, BudgetSummary, Currency
from contracts.routing import RoadBudgetCalculator, RoadSegment, VehicleFuelProfile
from contracts.trips import TransportMode, TripBrief
from functions.decision_router.client import RemoteLayaDecisionAdapter
from functions.optimizer.solver import ItineraryScheduler
from functions.orchestrator.decision.policy import LayaPolicyValidator
from functions.orchestrator.decision.repository import (
    LayaDecisionRepository,
    compute_canonical_replay_digest,
)

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
    laya_decisions: list[dict[str, Any]]
    evidence_routing_action: str
    static_search_completed: bool
    required_evidence_satisfied: bool
    remaining_search_budget: int
    _laya_adapter: LayaDecisionPort
    _db_session: Any


class TripPlanningNode(StrEnum):
    """Canonical node identifiers for the trip planning workflow graph."""

    PARSE_BRIEF = "parse_brief"
    DISCOVER_CANDIDATES = "discover_candidates"
    ROUTE_EVIDENCE_DECISION = "route_evidence_decision"
    AWAIT_EVIDENCE_RESULTS = "await_evidence_results"
    ESCALATE_DYNAMIC_EVIDENCE = "escalate_dynamic_evidence"
    SOLVE_SCHEDULE = "solve_schedule"
    COMPUTE_BUDGET = "compute_budget"
    SYNTHESIZE_PROPOSAL = "synthesize_proposal"


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
    pending_tasks: list[dict[str, Any]] = list(state.get("pending_tasks", []))
    newly_dispatched: list[str] = []

    for req in extracted_requests:
        if len(newly_dispatched) >= available_slots:
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
    if newly_dispatched:
        result["current_step"] = "dynamic_evidence_escalated"

    return result


async def route_evidence_decision_node(state: TripPlanningState) -> dict[str, Any]:
    """Evaluates evidence collection strategy via remote LAYA router and deterministic policy gating.

    Conforms to spec 0004 Slice 5:
    - Placed between discover_candidates and escalate_dynamic_evidence.
    - If DISPATCH_EVIDENCE_SEARCH is approved, enqueues static evidence search task in pending_tasks.
    - If DISPATCH_BROWSER_ESCALATION is approved, passes to escalate_dynamic_evidence.
    - If PROCEED_DETERMINISTIC_EVALUATION is approved, advances directly to schedule solving without task generation.
    - Updates TripPlanningState with laya_decisions list.
    """
    raw_exec_id = state.get("execution_id") or state.get("trip_id") or "default-execution"
    try:
        exec_uuid = uuid.UUID(str(raw_exec_id))
    except ValueError:
        exec_uuid = uuid.uuid4()

    trip_uuid: uuid.UUID | None = None
    if state.get("trip_id"):
        try:
            trip_uuid = uuid.UUID(str(state["trip_id"]))
        except ValueError:
            trip_uuid = None

    context_version = int(str(state.get("version") or state.get("context_version") or 1))
    brief = state.get("brief", {})
    if isinstance(brief, dict):
        destinations = brief.get("destinations", [])
        primary_destination = (
            brief.get("destination_name")
            or (destinations[0].get("name") if destinations and isinstance(destinations[0], dict) else None)
            or (destinations[0].name if destinations and hasattr(destinations[0], "name") else None)
            or ""
        )
    else:
        destinations = getattr(brief, "destinations", [])
        primary_destination = getattr(brief, "destination_name", "") or ""
    dest_name = primary_destination or (destinations[0].get("name") if destinations and isinstance(destinations[0], dict) else "Unknown")

    discovered_places = state.get("discovered_places", [])
    task_outputs = state.get("task_outputs", {})
    already_dispatched = set(state.get("dispatched_browser_urls", []))
    laya_decisions = list(state.get("laya_decisions", []))

    # 1. Inspect unhandled browser requests
    extracted_requests: list[dict[str, Any]] = []
    for output in task_outputs.values():
        if isinstance(output, dict):
            reqs = output.get("browser_requests", [])
            if isinstance(reqs, list):
                extracted_requests.extend(reqs)
    if "browser_requests" in state and isinstance(state["browser_requests"], list):
        extracted_requests.extend(state["browser_requests"])

    unhandled_browser_requests = len(
        [r for r in extracted_requests if r.get("canonical_url", r.get("url")) not in already_dispatched]
    )

    max_slots = 3
    try:
        max_slots = int(get_settings().BROWSER_MAX_ESCALATION_TASKS)
    except Exception:
        max_slots = 3
    remaining_browser_slots = max(0, max_slots - len(already_dispatched))

    # 2. Check search completion & required destination evidence prerequisites
    # Validates actual required evidence: non-placeholder, destination-relevant, confirmed persisted
    db_session = state.get("_db_session")
    valid_persisted_count = 0
    validated_evidence_satisfied = False
    db_queried = False

    # Extract required destination names
    required_dest_names: list[str] = []
    for d in destinations:
        if isinstance(d, dict) and d.get("name"):
            required_dest_names.append(d["name"].strip())
        elif hasattr(d, "name") and d.name:
            required_dest_names.append(d.name.strip())
    if not required_dest_names and primary_destination:
        required_dest_names.append(primary_destination.strip())
    if not required_dest_names:
        for p in discovered_places:
            if isinstance(p, dict) and p.get("name"):
                required_dest_names.append(p["name"].strip())
            elif hasattr(p, "name") and p.name:
                required_dest_names.append(p.name.strip())

    persisted_rows: list[Any] = []
    if db_session is not None:
        try:
            from sqlalchemy import select

            from functions.evidence_collector.models import SourceObservationModel

            stmt = select(
                SourceObservationModel.id,
                SourceObservationModel.canonical_url,
                SourceObservationModel.excerpt_text,
                SourceObservationModel.extraction_method,
                SourceObservationModel.content_hash,
                SourceObservationModel.artifact_ref,
            ).where(
                SourceObservationModel.execution_id == exec_uuid,
                SourceObservationModel.extraction_method != "NEEDS_BROWSER",
            )
            res = await db_session.execute(stmt)
            persisted_rows = list(res.all())
            db_queried = True
        except Exception as obs_exc:
            logger.debug("Failed querying source observations from db_session: %s", obs_exc)
    else:
        try:
            from sqlalchemy import select

            from config.db import get_session_factory
            from functions.evidence_collector.models import SourceObservationModel

            factory = get_session_factory()
            async with factory() as session:
                stmt = select(
                    SourceObservationModel.id,
                    SourceObservationModel.canonical_url,
                    SourceObservationModel.excerpt_text,
                    SourceObservationModel.extraction_method,
                    SourceObservationModel.content_hash,
                    SourceObservationModel.artifact_ref,
                ).where(
                    SourceObservationModel.execution_id == exec_uuid,
                    SourceObservationModel.extraction_method != "NEEDS_BROWSER",
                )
                res = await session.execute(stmt)
                persisted_rows = list(res.all())
                db_queried = True
        except Exception as obs_exc:
            logger.debug("Failed querying source observations from database factory: %s", obs_exc)

    if db_queried:
        matched_destinations: set[str] = set()
        for row in persisted_rows:
            url_str = (row.canonical_url or "").lower()
            text_str = (row.excerpt_text or "").lower()
            meta_str = str(row.artifact_ref or {}).lower()
            for dest in required_dest_names:
                d_lower = dest.lower()
                if d_lower in url_str or d_lower in text_str or d_lower in meta_str:
                    matched_destinations.add(dest)

        valid_persisted_count = len(persisted_rows)
        if required_dest_names:
            validated_evidence_satisfied = len(matched_destinations) == len(required_dest_names)
        else:
            validated_evidence_satisfied = valid_persisted_count > 0
    else:
        # Offline or disconnected fallback: inspect task_outputs in memory
        offline_matched: set[str] = set()
        offline_valid_count = 0
        for output in task_outputs.values():
            if isinstance(output, dict):
                obs_list = output.get("observations") or output.get("source_observations") or []
                if isinstance(obs_list, list):
                    for obs in obs_list:
                        if not isinstance(obs, dict):
                            continue
                        # Exclude JS placeholders
                        if obs.get("extraction_method") == "NEEDS_BROWSER":
                            continue
                        if not obs.get("canonical_url"):
                            continue
                        url_str = str(obs.get("canonical_url", "")).lower()
                        text_str = str(obs.get("excerpt_text", "")).lower()
                        offline_valid_count += 1
                        for dest in required_dest_names:
                            d_lower = dest.lower()
                            if d_lower in url_str or d_lower in text_str:
                                offline_matched.add(dest)

        valid_persisted_count = offline_valid_count
        if required_dest_names:
            validated_evidence_satisfied = len(offline_matched) == len(required_dest_names) and offline_valid_count > 0
        else:
            validated_evidence_satisfied = offline_valid_count > 0

    has_evidence_task_output = any(
        isinstance(output, dict) and (output.get("search_completed") or "observations" in output)
        for output in task_outputs.values()
    )
    static_search_completed = bool(
        state.get("static_search_completed")
        or has_evidence_task_output
        or valid_persisted_count > 0
    )
    if state.get("required_evidence_satisfied") is not None:
        required_evidence_satisfied = bool(state["required_evidence_satisfied"])
    else:
        required_evidence_satisfied = validated_evidence_satisfied

    remaining_search_budget = int(state.get("remaining_search_budget", 1 if not static_search_completed else 0))

    # 3. Determine explicit workflow phase
    if not static_search_completed:
        phase = DecisionPhase.SEARCH_COMPLETION
    elif unhandled_browser_requests > 0 and remaining_browser_slots > 0:
        phase = DecisionPhase.BROWSER_ELIGIBILITY
    else:
        phase = DecisionPhase.EVALUATION

    # 4. Calculate eligible choices via LayaPolicyValidator
    eligible_choices = LayaPolicyValidator.compute_eligible_choices(
        phase=phase,
        parent_status=state.get("status", "RUNNING").upper(),
        static_search_completed=static_search_completed,
        required_evidence_satisfied=required_evidence_satisfied,
        unhandled_browser_requests=unhandled_browser_requests,
        remaining_browser_slots=remaining_browser_slots,
        remaining_search_budget=remaining_search_budget,
    )

    destinations = brief.get("destinations", [])
    dest_name = (
        brief.get("destination_name")
        or (destinations[0].get("name") if destinations else "Unknown")
    )
    sanitized_context = {
        "execution_id": str(exec_uuid),
        "phase": phase.value,
        "category": "lodging",
        "destination": dest_name,
        "static_search_completed": static_search_completed,
        "candidate_places_count": len(discovered_places),
        "unhandled_browser_requests": unhandled_browser_requests,
        "browser_escalation_slots_remaining": remaining_browser_slots,
        "sanitized_summary": f"Discovered {len(discovered_places)} places. Unhandled dynamic requests: {unhandled_browser_requests}.",
    }

    request_digest, idempotency_key = compute_canonical_replay_digest(
        execution_id=exec_uuid,
        phase=phase,
        context_version=context_version,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        question_version="evidence_routing:v1",
        eligible_actions=eligible_choices if eligible_choices else [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
        sanitized_context=sanitized_context,
    )

    # 5. Resolve decision
    envelope: DecisionEnvelope

    decision_req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=phase,
        execution_id=exec_uuid,
        trip_id=trip_uuid,
        context_version=context_version,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        question_version="evidence_routing:v1",
        policy_derived_eligible_choices=eligible_choices if eligible_choices else [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
        budget_remaining=remaining_browser_slots,
        context=sanitized_context,
        idempotency_key=idempotency_key,
    )

    if len(eligible_choices) == 0:
        envelope = LayaPolicyValidator.handle_zero_choices(
            decision_req,
            required_evidence_satisfied=required_evidence_satisfied,
        )

    elif len(eligible_choices) == 1:
        # Deterministic singleton bypass
        envelope = LayaPolicyValidator.create_singleton_decision(
            decision_req,
            eligible_choices[0],
        )

    else:
        # Multi-choice evaluation (Phase 2)
        # Check result reuse in state or database
        reused: DecisionEnvelope | None = None
        for recorded in laya_decisions:
            if (
                recorded.get("idempotency_key") == idempotency_key
                and recorded.get("phase") == phase.value
                and recorded.get("action") in [c.value for c in eligible_choices]
            ):
                reused = DecisionEnvelope(
                    decision_id=uuid.UUID(str(recorded["decision_id"])),
                    decision_type=DecisionType.EVIDENCE_ROUTING,
                    phase=phase,
                    execution_id=exec_uuid,
                    trip_id=trip_uuid,
                    idempotency_key=idempotency_key,
                    allowed_choice=DecisionAction(recorded["action"]),
                    status=DecisionStatus(recorded["status"]),
                    confidence=recorded.get("confidence"),
                    answer_confidence=recorded.get("answer_confidence"),
                    model_version=recorded.get("model_version"),
                    policy_version=recorded.get("policy_version", LayaPolicyValidator.POLICY_VERSION),
                    rationale=recorded.get("rationale"),
                    latency_ms=recorded.get("latency_ms", 0),
                    request_digest=request_digest,
                    created_at=recorded.get("created_at") or datetime.now(UTC).isoformat(),
                )
                break

        if reused is None:
            if db_session is not None:
                try:
                    repo = LayaDecisionRepository(session=db_session)
                    reused = await repo.find_reusable_decision(decision_req, active_phase=phase)
                except Exception as repo_exc:
                    logger.debug("Database reusable decision lookup missed: %s", repo_exc)
            else:
                try:
                    from config.db import get_session_factory

                    factory = get_session_factory()
                    async with factory() as session:
                        repo = LayaDecisionRepository(session=session)
                        reused = await repo.find_reusable_decision(decision_req, active_phase=phase)
                except Exception as repo_exc:
                    logger.debug("Database reusable decision lookup missed: %s", repo_exc)

        if reused is not None:
            envelope = reused
        else:
            adapter = state.get("_laya_adapter")
            owns_adapter = False
            if adapter is None:
                settings = get_settings()
                adapter = RemoteLayaDecisionAdapter(settings=settings)
                owns_adapter = True

            try:
                envelope = await adapter.query_decision(decision_req)
            except Exception as exc:
                envelope = LayaPolicyValidator.resolve_fallback(
                    decision_req,
                    DecisionStatus.FALLBACK_ERROR,
                    rationale=f"Fallback: {str(exc)}",
                    latency_ms=0,
                    required_evidence_satisfied=required_evidence_satisfied,
                    unhandled_browser_requests=unhandled_browser_requests,
                    remaining_browser_slots=remaining_browser_slots,
                    remaining_search_budget=remaining_search_budget,
                )
            finally:
                if owns_adapter and hasattr(adapter, "aclose"):
                    await adapter.aclose()

    # 6. Database persistence when session provided
    if state.get("_db_session"):
        repo = LayaDecisionRepository(session=state["_db_session"])
        await repo.persist_decision(
            envelope=envelope,
            context_version=context_version,
            allowed_choices=eligible_choices,
        )

    # 7. Record decision telemetry in state
    record = {
        "decision_id": str(envelope.decision_id),
        "phase": envelope.phase.value,
        "action": envelope.allowed_choice.value,
        "status": envelope.status.value,
        "confidence": envelope.confidence,
        "answer_confidence": envelope.answer_confidence,
        "model_version": envelope.model_version,
        "policy_version": envelope.policy_version,
        "rationale": envelope.rationale,
        "latency_ms": envelope.latency_ms,
        "idempotency_key": envelope.idempotency_key,
        "request_digest": envelope.request_digest,
        "created_at": envelope.created_at,
        "context_version": context_version,
        "allowed_choices": [a.value for a in eligible_choices],
    }
    updated_decisions = list(laya_decisions)
    updated_decisions.append(record)

    # 8. Action enactment
    if envelope.allowed_choice == DecisionAction.DISPATCH_EVIDENCE_SEARCH:
        dest_hash = hashlib.sha256(dest_name.encode("utf-8")).hexdigest()
        search_task = {
            "worker": "EVIDENCE",
            "action": "collect",
            "payload": {
                "queries": [
                    f"{dest_name} attractions activities",
                    f"{dest_name} lodging hotels",
                ],
                "budget": {
                    "cost_per_call_minor": 50,
                    "currency": "INR",
                    "remaining_tool_calls": 5,
                    "remaining_cost_minor": 500,
                },
                "destination": dest_name,
                "origin": brief.get("origin_name", ""),
                "categories": ["attractions", "lodging", "transit"],
            },
            "idempotency_key": f"{exec_uuid}:collect:{dest_hash[:16]}",
            "deadline_seconds": 60,
            "max_attempts": 3,
        }
        return {
            "pending_tasks": [search_task],
            "current_step": "evidence_search_routed",
            "evidence_routing_action": DecisionAction.DISPATCH_EVIDENCE_SEARCH.value,
            "laya_decisions": updated_decisions,
        }

    elif envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION:
        return {
            "pending_tasks": [],
            "current_step": "browser_escalation_routed",
            "evidence_routing_action": DecisionAction.DISPATCH_BROWSER_ESCALATION.value,
            "laya_decisions": updated_decisions,
        }

    else:
        return {
            "pending_tasks": [],
            "current_step": "deterministic_evaluation_routed",
            "evidence_routing_action": DecisionAction.PROCEED_DETERMINISTIC_EVALUATION.value,
            "laya_decisions": updated_decisions,
        }


def await_evidence_results_node(state: TripPlanningState) -> dict[str, Any]:
    """Clears dispatched pending tasks and yields control back to route_evidence_decision on resume."""
    return {
        "pending_tasks": [],
        "current_step": "awaiting_evidence_results",
    }


def route_evidence_decision_edge(state: TripPlanningState) -> str:
    """Routes based on evidence decision action."""
    action = state.get("evidence_routing_action")
    if action == DecisionAction.DISPATCH_BROWSER_ESCALATION.value:
        return TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE
    elif action == DecisionAction.DISPATCH_EVIDENCE_SEARCH.value:
        return TripPlanningNode.AWAIT_EVIDENCE_RESULTS
    return TripPlanningNode.SOLVE_SCHEDULE


def escalate_dynamic_evidence_edge(state: TripPlanningState) -> str:
    """Routes to await_evidence_results if browser tasks are pending, else to solve_schedule."""
    if state.get("pending_tasks"):
        return TripPlanningNode.AWAIT_EVIDENCE_RESULTS
    return TripPlanningNode.SOLVE_SCHEDULE


# --- Graph Construction ---


def build_trip_planning_graph() -> StateGraph[TripPlanningState, Any, Any, Any]:
    graph: StateGraph[TripPlanningState, Any, Any, Any] = StateGraph(TripPlanningState)

    graph.add_node(TripPlanningNode.PARSE_BRIEF, parse_and_validate_brief_node)
    graph.add_node(TripPlanningNode.DISCOVER_CANDIDATES, discover_candidates_node)
    graph.add_node(TripPlanningNode.ROUTE_EVIDENCE_DECISION, route_evidence_decision_node)
    graph.add_node(TripPlanningNode.AWAIT_EVIDENCE_RESULTS, await_evidence_results_node)
    graph.add_node(TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE, escalate_dynamic_evidence_node)
    graph.add_node(TripPlanningNode.SOLVE_SCHEDULE, solve_schedule_node)
    graph.add_node(TripPlanningNode.COMPUTE_BUDGET, compute_budget_node)
    graph.add_node(TripPlanningNode.SYNTHESIZE_PROPOSAL, synthesize_proposal_node)

    graph.add_edge(START, TripPlanningNode.PARSE_BRIEF)
    graph.add_edge(TripPlanningNode.PARSE_BRIEF, TripPlanningNode.DISCOVER_CANDIDATES)
    graph.add_edge(TripPlanningNode.DISCOVER_CANDIDATES, TripPlanningNode.ROUTE_EVIDENCE_DECISION)
    graph.add_conditional_edges(
        TripPlanningNode.ROUTE_EVIDENCE_DECISION,
        route_evidence_decision_edge,
        {
            TripPlanningNode.AWAIT_EVIDENCE_RESULTS: TripPlanningNode.AWAIT_EVIDENCE_RESULTS,
            TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE: TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE,
            TripPlanningNode.SOLVE_SCHEDULE: TripPlanningNode.SOLVE_SCHEDULE,
        },
    )
    graph.add_conditional_edges(
        TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE,
        escalate_dynamic_evidence_edge,
        {
            TripPlanningNode.AWAIT_EVIDENCE_RESULTS: TripPlanningNode.AWAIT_EVIDENCE_RESULTS,
            TripPlanningNode.SOLVE_SCHEDULE: TripPlanningNode.SOLVE_SCHEDULE,
        },
    )
    graph.add_edge(TripPlanningNode.AWAIT_EVIDENCE_RESULTS, TripPlanningNode.ROUTE_EVIDENCE_DECISION)
    graph.add_edge(TripPlanningNode.SOLVE_SCHEDULE, TripPlanningNode.COMPUTE_BUDGET)
    graph.add_edge(TripPlanningNode.COMPUTE_BUDGET, TripPlanningNode.SYNTHESIZE_PROPOSAL)
    graph.add_edge(TripPlanningNode.SYNTHESIZE_PROPOSAL, END)

    return graph


def compile_trip_planning_workflow(checkpointer: Any | None = None) -> Any:
    """Compiles the trip planning StateGraph with the required yield/interrupt boundaries."""
    return build_trip_planning_graph().compile(
        checkpointer=checkpointer,
        interrupt_before=[TripPlanningNode.AWAIT_EVIDENCE_RESULTS],
    )


trip_planning_workflow = compile_trip_planning_workflow()
