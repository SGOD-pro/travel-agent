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
import math
import os
import re
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from config.config import get_settings
from config.db import get_session_factory
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
from functions.decision_router.fixtures import FixtureLayaDecisionAdapter
from functions.evidence_collector.models import SourceObservationModel
from functions.optimizer.solver import ItineraryScheduler
from functions.orchestrator.decision.policy import LayaPolicyValidator
from functions.orchestrator.decision.repository import (
    LayaDecisionRepository,
    compute_canonical_replay_digest,
)

logger = logging.getLogger(__name__)

_DEFAULT_RUNNABLE_CONFIG: RunnableConfig = cast(RunnableConfig, {})


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
    _session_factory: Any
    _offline_fixture_mode: bool


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


_UNAVAILABLE_EXCERPT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:destination\s+)?information\s+(?:is\s+)?unavailable\b", re.IGNORECASE),
    re.compile(r"\b(?:content|details?|data|service|source)\s+(?:is\s+)?unavailable\b", re.IGNORECASE),
    re.compile(r"\b(?:temporarily|currently)\s+unavailable\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:results?|information|details?|data|records?)\s+(?:found|available)\b", re.IGNORECASE),
    re.compile(r"\b(?:page\s+not\s+found|404\s+not\s+found|not\s+found)\b", re.IGNORECASE),
    re.compile(r"\b(?:access\s+denied|403\s+forbidden|unauthorized)\b", re.IGNORECASE),
    re.compile(r"\b(?:failed|unable)\s+to\s+(?:retrieve|load|fetch|find|locate)\b", re.IGNORECASE),
]


def _extract_coordinates_from_obs(obs: Any, artifact_ref: Any) -> tuple[float | None, float | None]:
    """Extracts raw coordinates from observation artifact_ref or fields and validates them as floats."""
    obs_lat_raw: Any = None
    obs_lng_raw: Any = None

    if isinstance(artifact_ref, dict):
        gps = artifact_ref.get("gps")
        if isinstance(gps, dict):
            obs_lat_raw = gps.get("lat") if gps.get("lat") is not None else gps.get("latitude")
            obs_lng_raw = gps.get("lng") if gps.get("lng") is not None else gps.get("longitude")
        elif isinstance(gps, (list, tuple)) and len(gps) >= 2:
            obs_lat_raw, obs_lng_raw = gps[0], gps[1]

        if obs_lat_raw is None and "latitude" in artifact_ref:
            obs_lat_raw = artifact_ref.get("latitude")
        if obs_lng_raw is None and "longitude" in artifact_ref:
            obs_lng_raw = artifact_ref.get("longitude")
        if obs_lat_raw is None and "lat" in artifact_ref:
            obs_lat_raw = artifact_ref.get("lat")
        if obs_lng_raw is None and "lng" in artifact_ref:
            obs_lng_raw = artifact_ref.get("lng")

        if (obs_lat_raw is None or obs_lng_raw is None) and "parsed_observations" in artifact_ref:
            parsed = artifact_ref.get("parsed_observations")
            if isinstance(parsed, list):
                for p in parsed:
                    if isinstance(p, dict):
                        p_lat, p_lng = _extract_coordinates_from_obs(p, p.get("artifact_ref") or p)
                        if p_lat is not None and p_lng is not None:
                            return p_lat, p_lng

    if obs_lat_raw is None and isinstance(obs, dict):
        obs_lat_raw = obs.get("latitude") if obs.get("latitude") is not None else obs.get("lat")
    if obs_lng_raw is None and isinstance(obs, dict):
        obs_lng_raw = obs.get("longitude") if obs.get("longitude") is not None else obs.get("lng")

    if obs_lat_raw is None:
        obs_lat_raw = getattr(obs, "latitude", None)
    if obs_lat_raw is None:
        obs_lat_raw = getattr(obs, "lat", None)
    if obs_lng_raw is None:
        obs_lng_raw = getattr(obs, "longitude", None)
    if obs_lng_raw is None:
        obs_lng_raw = getattr(obs, "lng", None)

    if obs_lat_raw is None or obs_lng_raw is None:
        return None, None

    try:
        lat_f = float(obs_lat_raw)
        lng_f = float(obs_lng_raw)
    except (ValueError, TypeError, OverflowError):
        return None, None

    if math.isnan(lat_f) or math.isnan(lng_f) or math.isinf(lat_f) or math.isinf(lng_f):
        return None, None

    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        return None, None

    return lat_f, lng_f


def _is_valid_source_evidence_for_destination(
    obs: Any,
    dest_name: str,
    dest_lat: float | None = None,
    dest_lng: float | None = None,
) -> bool:
    """Validates that a source observation satisfies canonical requirements for a destination.

    Rejects:
    - NEEDS_BROWSER dynamic rendering placeholders.
    - Missing or non-http/https canonical URLs.
    - Missing content hash.
    - Destination-name-only URLs (where destination name is in URL but nowhere in substantive content).
    - Destination-name-only text (where excerpt text is just the destination name without substance).
    - Sparse excerpt text (<15 characters) unless structured observations are present in artifact_ref.
    - Negative or unavailable destination content (e.g. 'information is unavailable', 404, blocked).
    - Missing source coordinates (must extract valid latitude and longitude).
    - Malformed source coordinates (conversion errors, NaN, inf, or out-of-bounds geographic ranges).
    - Conflicting coordinates (if both destination and observation coordinates exist and haversine distance > 100 km).
    """
    url = getattr(obs, "canonical_url", None) or (obs.get("canonical_url") if isinstance(obs, dict) else "")
    method = getattr(obs, "extraction_method", None) or (obs.get("extraction_method") if isinstance(obs, dict) else "")
    content_hash = getattr(obs, "content_hash", None) or (obs.get("content_hash") if isinstance(obs, dict) else "")
    excerpt = getattr(obs, "excerpt_text", None) or (obs.get("excerpt_text") if isinstance(obs, dict) else "")
    artifact_ref = getattr(obs, "artifact_ref", None) or (obs.get("artifact_ref") if isinstance(obs, dict) else {})

    # 1. Reject NEEDS_BROWSER dynamic rendering placeholders and error/unavailable metadata
    if method == "NEEDS_BROWSER":
        return False
    if isinstance(artifact_ref, dict):
        if artifact_ref.get("browser_reason"):
            return False
        status_val = str(artifact_ref.get("status") or "").upper().strip()
        if status_val in ("UNAVAILABLE", "BLOCKED", "TIMED_OUT", "FAILED", "ERROR", "NOT_FOUND"):
            return False
        if artifact_ref.get("unavailable_reason") or artifact_ref.get("unavailable_sources") or artifact_ref.get("error"):
            return False

    if "[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]" in (excerpt or ""):
        return False

    # 2. Reject missing or non-http/https canonical URLs and missing content hash
    url_str = str(url or "").strip()
    if not (url_str.startswith("http://") or url_str.startswith("https://")):
        return False
    if not content_hash or not str(content_hash).strip():
        return False

    clean_excerpt = str(excerpt or "").strip()
    clean_dest = dest_name.strip()
    if not clean_dest:
        return False

    # 3. Reject destination-name-only text
    if clean_excerpt.lower() == clean_dest.lower():
        return False

    # 4. Reject negative or unavailable content
    for pattern in _UNAVAILABLE_EXCERPT_PATTERNS:
        if pattern.search(clean_excerpt):
            logger.debug(
                "Source observation rejected: excerpt matches unavailability pattern '%s'",
                pattern.pattern,
            )
            return False

    # 5. Destination name must appear as a word token in excerpt or structured metadata
    dest_pattern = re.compile(rf"\b{re.escape(clean_dest)}\b", re.IGNORECASE)
    has_dest_in_excerpt = bool(dest_pattern.search(clean_excerpt))

    has_dest_in_meta = False
    if isinstance(artifact_ref, dict):
        meta_str = " ".join(
            str(v)
            for k, v in artifact_ref.items()
            if k not in ("browser_reason", "html_snippet", "raw_html")
        )
        if dest_pattern.search(meta_str):
            has_dest_in_meta = True

    if not has_dest_in_excerpt and not has_dest_in_meta:
        # Destination-name substring matching in URL alone is rejected
        return False

    # 6. Substantive content check: excerpt must have substance (>= 15 chars) or structured items in artifact_ref
    has_structured = isinstance(artifact_ref, dict) and bool(
        artifact_ref.get("parsed_observations")
        or artifact_ref.get("hotels")
        or artifact_ref.get("fares")
    )
    if len(clean_excerpt) < 15 and not has_structured:
        return False

    # 7. Coordinate extraction and validation (fail-closed on missing or malformed coordinates)
    obs_lat, obs_lng = _extract_coordinates_from_obs(obs, artifact_ref)
    if obs_lat is None or obs_lng is None:
        logger.debug(
            "Observation rejected for destination %s: missing or malformed source coordinates",
            clean_dest,
        )
        return False

    # 8. Coordinate distance check: reject irrelevant observations exceeding 100 km from destination
    if dest_lat is not None and dest_lng is not None:
        try:
            d_lat = float(dest_lat)
            d_lng = float(dest_lng)
        except (ValueError, TypeError, OverflowError):
            logger.debug("Destination coordinates malformed: (%s, %s)", dest_lat, dest_lng)
            return False

        if math.isnan(d_lat) or math.isnan(d_lng) or math.isinf(d_lat) or math.isinf(d_lng):
            return False

        dist = ItineraryScheduler._haversine_distance_km(d_lat, d_lng, obs_lat, obs_lng)
        if dist > 100.0:
            logger.debug(
                "Observation coordinates (%s, %s) exceed 100km distance (%s km) from destination %s (%s, %s)",
                obs_lat,
                obs_lng,
                dist,
                clean_dest,
                d_lat,
                d_lng,
            )
            return False

    return True


async def route_evidence_decision_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
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
    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    session_factory = cast(
        Any,
        state.get("_session_factory") or configurable.get("session_factory"),
    )
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
    adapter = state.get("_laya_adapter")

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

    # 2. Extract canonical destination requirements: identity and coordinates
    destinations_info: list[dict[str, Any]] = []
    for d in destinations:
        if isinstance(d, dict):
            name = d.get("name")
            if name and str(name).strip():
                destinations_info.append({
                    "name": str(name).strip(),
                    "lat": d.get("latitude"),
                    "lng": d.get("longitude"),
                })
        elif hasattr(d, "name") and d.name:
            destinations_info.append({
                "name": str(d.name).strip(),
                "lat": getattr(d, "latitude", None),
                "lng": getattr(d, "longitude", None),
            })

    if not destinations_info and primary_destination:
        destinations_info.append({"name": primary_destination.strip(), "lat": None, "lng": None})

    if not destinations_info:
        for p in discovered_places:
            if isinstance(p, dict) and p.get("name"):
                destinations_info.append({
                    "name": str(p["name"]).strip(),
                    "lat": p.get("lat") or p.get("latitude"),
                    "lng": p.get("lng") or p.get("longitude"),
                })
            elif hasattr(p, "name") and p.name:
                destinations_info.append({
                    "name": str(p.name).strip(),
                    "lat": getattr(p, "lat", None) or getattr(p, "latitude", None),
                    "lng": getattr(p, "lng", None) or getattr(p, "longitude", None),
                })

    # 3. Check evidence prerequisites via fail-closed authoritative validation
    db_session = state.get("_db_session")
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode")
        or configurable.get("offline_fixture_mode")
        or isinstance(adapter, FixtureLayaDecisionAdapter)
        or getattr(adapter, "is_fixture", False)
    )
    db_queried = False
    db_lookup_failed = False
    persisted_rows: list[Any] = []

    if db_session is not None:
        try:
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
            logger.error("Failed querying source observations from db_session: %s", obs_exc)
            db_lookup_failed = True
    elif not is_offline_fixture:
        try:
            factory = session_factory if session_factory is not None else get_session_factory()
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
            logger.error("Failed querying source observations from database factory: %s", obs_exc)
            db_lookup_failed = True

    if db_lookup_failed:
        # Handle unavailable persistence through execution failure / retry semantics without scheduling
        raise RuntimeError("Persistence unavailable during authoritative evidence validation")

    valid_persisted_count = 0
    validated_evidence_satisfied = False

    if db_queried:
        matched_destinations: set[str] = set()
        for row in persisted_rows:
            for dest in destinations_info:
                if _is_valid_source_evidence_for_destination(
                    row, dest["name"], dest.get("lat"), dest.get("lng")
                ):
                    matched_destinations.add(dest["name"])
                    valid_persisted_count += 1

        if destinations_info:
            validated_evidence_satisfied = len(matched_destinations) == len(destinations_info)
        else:
            validated_evidence_satisfied = valid_persisted_count > 0

    elif is_offline_fixture:
        # Offline or fixture mode: inspect task_outputs in memory strictly when explicitly isolated
        offline_matched: set[str] = set()
        for output in task_outputs.values():
            if isinstance(output, dict):
                obs_list = output.get("observations") or output.get("source_observations") or []
                if isinstance(obs_list, list):
                    for obs in obs_list:
                        for dest in destinations_info:
                            if _is_valid_source_evidence_for_destination(
                                obs, dest["name"], dest.get("lat"), dest.get("lng")
                            ):
                                offline_matched.add(dest["name"])
                                valid_persisted_count += 1

        if destinations_info:
            validated_evidence_satisfied = len(offline_matched) == len(destinations_info) and valid_persisted_count > 0
        else:
            validated_evidence_satisfied = valid_persisted_count > 0

    has_evidence_task_output = any(
        isinstance(output, dict) and (output.get("search_completed") or "observations" in output)
        for output in task_outputs.values()
    )
    static_search_completed = bool(
        state.get("static_search_completed")
        or has_evidence_task_output
        or valid_persisted_count > 0
    )

    # Current authoritative validation sets required_evidence_satisfied;
    # a stale state flag must not override current authoritative validation.
    required_evidence_satisfied = validated_evidence_satisfied

    remaining_search_budget = int(state.get("remaining_search_budget", 1 if not static_search_completed else 0))

    # 4. Determine explicit workflow phase
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
            elif not is_offline_fixture:
                try:
                    factory = session_factory if session_factory is not None else get_session_factory()
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
