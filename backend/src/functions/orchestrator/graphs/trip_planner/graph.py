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
import zoneinfo
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypedDict, cast

from geoalchemy2.shape import to_shape
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from config.config import get_settings
from config.db import get_session_factory
from contracts.browser import BrowserTaskPayload, BrowserWaitUntil
from contracts.candidate import (
    CandidateEligibilityReport,
    CandidateExistenceStatus,
    CandidateHoursStatus,
    RegisteredCandidate,
)
from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionPort,
    LayaDecisionRequest,
)
from contracts.llm import PlaceProposal
from contracts.money import BudgetCategory, BudgetLine, BudgetSummary, Currency
from contracts.routing import (
    Point,
    RoadBudgetCalculator,
    RoadSegment,
    RouteObjective,
    RouteOptimizerWorkerOutput,
    RouteRequest,
    RouteStatus,
    RouteStopInput,
    StopType,
    VehicleFuelProfile,
)
from contracts.trips import DestinationPoint, TransportMode, TripBrief
from functions.decision_router.client import RemoteLayaDecisionAdapter
from functions.evidence_collector.models import SourceObservationModel
from functions.optimizer.solver import (
    ItineraryScheduler,
    OptimizedSchedule,
    ScheduledLeg,
    ScheduledStop,
)
from functions.orchestrator.decision.policy import LayaPolicyValidator
from functions.orchestrator.decision.repository import (
    LayaDecisionRepository,
    compute_canonical_replay_digest,
)
from functions.orchestrator.evidence.eligibility import (
    DeterministicEvidenceEligibilityGate,
)
from functions.orchestrator.places.candidate_service import (
    CandidateRegistrationService,
)
from functions.orchestrator.places.schemas import CoordinatePoint
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository

logger = logging.getLogger(__name__)

_DEFAULT_RUNNABLE_CONFIG: RunnableConfig = cast(RunnableConfig, {})


class TripPlanningState(TypedDict, total=False):
    trip_id: str
    owner_id: str
    version: int
    brief: dict[str, Any]
    discovered_places: list[dict[str, Any]]
    validated_candidates: list[dict[str, Any]]
    candidate_eligibility: dict[str, Any]
    eligible_stops: list[dict[str, Any]]
    editorial_recommendations: list[dict[str, Any]]
    excluded_candidates: list[dict[str, Any]]
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
    route_id: str | None
    route_status: str | None
    routing_status: str | None
    route_diagnostics: list[str]
    optimizer_task_id: str | None
    optimizer_idempotency_key: str | None
    timezone: str | None
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
    EVALUATE_CANDIDATES = "evaluate_candidates"
    DISPATCH_ROUTE_OPTIMIZER = "dispatch_route_optimizer"
    AWAIT_ROUTE_RESULTS = "await_route_results"
    CONSUME_ROUTE_RESULT = "consume_route_result"
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
    discovered: list[dict[str, Any]] = list(state.get("discovered_places", []))
    existing_names = {d.get("name") for d in discovered if isinstance(d, dict) and d.get("name")}

    for d in brief.destinations:
        if d.name not in existing_names:
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


async def evaluate_candidates_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Registers candidate proposals, validates corridors, and evaluates deterministic evidence eligibility.

    Conforms to spec 0007 AC-1 and AC-3:
    1. Distinguishes explicitly verified brief destinations from unverified proposals.
    2. Registers candidate proposals into canonical places with spatial corridor filtering (100km).
    3. Evaluates evidence eligibility gate enforcing tenant and execution authorization.
    4. Categorizes candidates into:
       - eligible_stops: verified existence places with verified or assumed hours.
       - editorial_recommendations: unverified candidates within corridor.
       - excluded_candidates: contradicted or out-of-corridor candidates.
    """
    brief_data = state.get("brief", {})
    brief = TripBrief.model_validate(brief_data)

    # 1. Build corridor points from origin through brief destinations
    corridor_points = [CoordinatePoint(lat=float(brief.origin_lat), lng=float(brief.origin_lng))]
    for d in brief.destinations:
        corridor_points.append(CoordinatePoint(lat=float(d.latitude), lng=float(d.longitude)))

    # 2. Extract candidate proposals: register brief destinations as well as discovered candidates
    brief_dest_names = {d.name.strip().lower() for d in brief.destinations}
    brief_proposals: list[PlaceProposal] = []
    for d in brief.destinations:
        try:
            brief_proposals.append(
                PlaceProposal(
                    name=d.name,
                    category="destination",
                    estimated_latitude=float(d.latitude),
                    estimated_longitude=float(d.longitude),
                    suggested_stay_minutes=d.stay_days * 180,
                    address=d.name,
                )
            )
        except Exception as e:
            logger.debug("Skipping unparseable brief destination proposal %s: %s", d, e)

    discovered = state.get("discovered_places", [])
    candidate_proposals: list[PlaceProposal] = []
    for p in discovered:
        p_name = p.get("name", "")
        if p_name.strip().lower() in brief_dest_names:
            continue
        try:
            prop = PlaceProposal(
                name=p_name,
                category=p.get("category", "attraction"),
                estimated_latitude=float(p.get("lat", p.get("latitude", 0.0))),
                estimated_longitude=float(p.get("lng", p.get("longitude", 0.0))),
                suggested_stay_minutes=p.get("stay_minutes", 60),
                address=p.get("address"),
            )
            candidate_proposals.append(prop)
        except Exception as e:
            logger.debug("Skipping unparseable candidate proposal %s: %s", p, e)

    proposals = brief_proposals + candidate_proposals

    # Resolve execution and owner IDs
    exec_id_raw = state.get("execution_id")
    exec_uuid = uuid.UUID(str(exec_id_raw)) if exec_id_raw else None
    owner_id_raw = state.get("owner_id")
    owner_uuid = uuid.UUID(str(owner_id_raw)) if owner_id_raw else None

    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    session_factory = cast(
        Any,
        state.get("_session_factory") or configurable.get("session_factory"),
    )
    db_session = state.get("_db_session")
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode") or configurable.get("offline_fixture_mode")
    )

    if not is_offline_fixture and db_session is None and session_factory is None:
        try:
            session_factory = get_session_factory()
        except Exception:
            session_factory = None
        if session_factory is None:
            raise RuntimeError(
                "Database session factory is required for candidate registration in production. "
                "Silent in-memory registration fallback is prohibited."
            )

    if not proposals:
        report = CandidateEligibilityReport(
            eligible_stops=[],
            editorial_recommendations=[],
            excluded_candidates=[],
            summary={"total_candidates": 0, "eligible_stops": 0},
        )
        return {
            "current_step": "candidates_evaluated",
            "validated_candidates": [],
            "candidate_eligibility": report.model_dump(mode="json"),
            "eligible_stops": [],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

    fixture_obs_list: list[Any] = []
    fixture_claims_list: list[Any] = []
    if is_offline_fixture:
        task_out = state.get("task_outputs", {})
        if isinstance(task_out, dict):
            if "observations" in task_out and isinstance(task_out["observations"], list):
                fixture_obs_list.extend(task_out["observations"])
            if "claims" in task_out and isinstance(task_out["claims"], list):
                fixture_claims_list.extend(task_out["claims"])
            for sub in task_out.values():
                if isinstance(sub, dict):
                    if "observations" in sub and isinstance(sub["observations"], list):
                        fixture_obs_list.extend(sub["observations"])
                    if "claims" in sub and isinstance(sub["claims"], list):
                        fixture_claims_list.extend(sub["claims"])

    async def _verify_execution_barriers(session: Any | None) -> None:
        """Verifies authorization, cancellation, and deadline barriers before persistent writes."""
        if session is None or exec_uuid is None:
            return

        from functions.orchestrator.execution.models import ExecutionModel

        exec_stmt = select(ExecutionModel).where(ExecutionModel.id == exec_uuid)
        exec_res = await session.execute(exec_stmt)
        execution_record = exec_res.scalar_one_or_none()
        if execution_record is not None:
            if owner_uuid is not None and execution_record.owner_id != owner_uuid:
                raise PermissionError(
                    f"Execution authorization failed: execution {exec_uuid} belongs to owner {execution_record.owner_id}, caller is {owner_uuid}"
                )
            if execution_record.status == "CANCELLED" or execution_record.cancelled_at is not None:
                raise RuntimeError(
                    f"Execution {exec_uuid} is cancelled, aborting persistent candidate writes"
                )
            if (
                execution_record.deadline_at is not None
                and execution_record.deadline_at <= datetime.now(UTC)
            ):
                raise RuntimeError(
                    f"Execution {exec_uuid} deadline has expired, aborting persistent candidate writes"
                )

    async def _run_with_session(
        session: Any | None,
        is_caller_owned: bool,
    ) -> tuple[list[RegisteredCandidate], CandidateEligibilityReport, list[RegisteredCandidate]]:
        await _verify_execution_barriers(session)

        reg_service = CandidateRegistrationService(session=session)
        valid_cands, excluded_corr = await reg_service.register_candidate_proposals(
            proposals,
            corridor_points,
            execution_id=exec_uuid,
            owner_id=owner_uuid,
        )
        if session is not None:
            if is_caller_owned:
                await session.flush()
            else:
                await session.commit()
        gate = DeterministicEvidenceEligibilityGate(session=session)
        elig_report = await gate.evaluate_candidate_eligibility(
            valid_cands,
            execution_id=exec_uuid,
            owner_id=owner_uuid,
            fixture_observations=fixture_obs_list if is_offline_fixture else None,
            fixture_claims=fixture_claims_list if is_offline_fixture else None,
        )
        return valid_cands, elig_report, excluded_corr

    valid_cands: list[RegisteredCandidate] = []
    elig_report: CandidateEligibilityReport
    excluded_corr: list[RegisteredCandidate] = []

    if db_session is not None:
        valid_cands, elig_report, excluded_corr = await _run_with_session(
            db_session, is_caller_owned=True
        )
    elif session_factory is not None and not is_offline_fixture:
        async with session_factory() as session:
            valid_cands, elig_report, excluded_corr = await _run_with_session(
                session, is_caller_owned=False
            )
    else:
        valid_cands, elig_report, excluded_corr = await _run_with_session(
            None, is_caller_owned=False
        )

    all_excluded = list(elig_report.excluded_candidates) + list(excluded_corr)

    return {
        "current_step": "candidates_evaluated",
        "validated_candidates": [c.model_dump(mode="json") for c in valid_cands],
        "candidate_eligibility": elig_report.model_dump(mode="json"),
        "eligible_stops": [s.model_dump(mode="json") for s in elig_report.eligible_stops],
        "editorial_recommendations": [
            r.model_dump(mode="json") for r in elig_report.editorial_recommendations
        ],
        "excluded_candidates": [x.model_dump(mode="json") for x in all_excluded],
    }


def resolve_trip_timezone(
    brief: TripBrief | dict[str, Any] | None,
    state: TripPlanningState | None = None,
) -> zoneinfo.ZoneInfo:
    """Resolves trip timezone from state or brief contract without inventing a timezone.

    Validates timezone overrides according to the contract, raising ValueError on invalid values.
    """
    tz_val: Any = None
    if state and state.get("timezone") is not None:
        tz_val = state["timezone"]
        if not isinstance(tz_val, str) or not tz_val.strip():
            tz_val = None
        else:
            tz_str = tz_val.strip()
            try:
                return zoneinfo.ZoneInfo(tz_str)
            except Exception as exc:
                raise ValueError(f"Invalid IANA timezone string: '{tz_str}'") from exc

    if isinstance(brief, TripBrief):
        tz_val = brief.timezone
    elif isinstance(brief, dict):
        tz_val = brief.get("timezone")

    if tz_val is None or not str(tz_val).strip():
        return zoneinfo.ZoneInfo("Asia/Kolkata")

    tz_str = str(tz_val).strip()
    try:
        return zoneinfo.ZoneInfo(tz_str)
    except Exception as exc:
        raise ValueError(f"Invalid IANA timezone string: '{tz_str}'") from exc


def _parse_window_time(w_val: Any) -> time | None:
    if isinstance(w_val, time):
        return w_val
    if isinstance(w_val, datetime):
        return w_val.time()
    if isinstance(w_val, str) and w_val.strip():
        parts = w_val.strip().split(":")
        if len(parts) >= 2:
            try:
                return time(int(parts[0]), int(parts[1]))
            except Exception:
                pass
    return None


def _resolve_stop_window(
    stop_data: dict[str, Any],
    brief: TripBrief,
    brief_dest: DestinationPoint | None,
    trip_tz: zoneinfo.ZoneInfo,
) -> tuple[datetime | None, datetime | None]:
    """Resolves local opening window and converts consistently to UTC.

    Supports:
    - Multi-day trip offsets: respects brief_dest.visit_date or day_offset, or stop_data visit_date / day_offset.
    - Date boundaries / midnight crossing: if window_end <= window_start, closing extends to the next calendar day.
    - India-local and target IANA timezones: combines in local timezone first, then converts via .astimezone(UTC).
    """
    visit_date: date | None = None
    if brief_dest is not None:
        if brief_dest.visit_date is not None:
            visit_date = brief_dest.visit_date
        elif brief_dest.day_offset is not None:
            visit_date = brief.start_date + timedelta(days=brief_dest.day_offset)

    if visit_date is None:
        raw_vd = stop_data.get("visit_date")
        if isinstance(raw_vd, date):
            visit_date = raw_vd
        elif isinstance(raw_vd, str) and raw_vd.strip():
            try:
                visit_date = date.fromisoformat(raw_vd.strip())
            except Exception:
                pass

    if visit_date is None:
        raw_offset = stop_data.get("day_offset")
        if raw_offset is not None:
            try:
                visit_date = brief.start_date + timedelta(days=int(raw_offset))
            except Exception:
                pass

    if visit_date is None:
        visit_date = brief.start_date

    if visit_date < brief.start_date:
        visit_date = brief.start_date

    is_assumed = bool(
        stop_data.get("is_hours_assumed", False) or stop_data.get("hours_status") == "HOURS_ASSUMED"
    )
    t_start = _parse_window_time(stop_data.get("window_start")) or (
        time(9, 0) if is_assumed else None
    )
    t_end = _parse_window_time(stop_data.get("window_end")) or (time(18, 0) if is_assumed else None)

    if t_start is None or t_end is None:
        dt_start = (
            datetime.combine(visit_date, t_start, tzinfo=trip_tz).astimezone(UTC)
            if t_start is not None
            else None
        )
        dt_end = (
            datetime.combine(visit_date, t_end, tzinfo=trip_tz).astimezone(UTC)
            if t_end is not None
            else None
        )
        return dt_start, dt_end

    # Midnight date boundary crossing: if end time is earlier than or equal to start time,
    # venue closes on the next morning.
    end_date = visit_date + timedelta(days=1) if t_end <= t_start else visit_date

    dt_start = datetime.combine(visit_date, t_start, tzinfo=trip_tz).astimezone(UTC)
    dt_end = datetime.combine(end_date, t_end, tzinfo=trip_tz).astimezone(UTC)
    return dt_start, dt_end


def dispatch_route_optimizer_node(state: TripPlanningState) -> dict[str, Any]:
    """Formulates RouteRequest and emits pending task specification for route optimizer worker.

    Conforms to spec 0007 AC-3, AC-4, AC-9:
    1. Only verified places enter RouteRequest.stops (unverified candidate proposals and unverified
       brief destinations are strictly excluded from the solver stops and preserved in editorial recommendations).
    2. Contradicted places are excluded entirely.
    3. Verified places with unknown opening hours enter with assumed daytime window (09:00 to 18:00)
       explicitly labeled with hours_status=HOURS_ASSUMED.
    4. Verified places with verified opening hours enter with exact window timestamps on departure date.
    5. Emits task specification for worker OPTIMIZER with action solve, idempotency_key, and deadline.
    """
    brief = TripBrief.model_validate(state["brief"])
    trip_tz = resolve_trip_timezone(brief, state)
    brief_names = {d.name.strip().lower() for d in brief.destinations}
    dest_by_norm_name: dict[str, DestinationPoint] = {
        d.name.strip().lower(): d for d in brief.destinations
    }

    excluded_candidates = state.get("excluded_candidates", [])
    excluded_names = {
        str(c.get("primary_name") if isinstance(c, dict) else getattr(c, "primary_name", "") or "")
        .strip()
        .lower()
        for c in excluded_candidates
        if c is not None
    }

    eligible_stops_data = state.get("eligible_stops")
    eligible_by_name: dict[str, dict[str, Any]] = {}
    if eligible_stops_data is not None:
        for s in eligible_stops_data:
            s_name = (
                str(
                    s.get("primary_name")
                    if isinstance(s, dict)
                    else getattr(s, "primary_name", "") or ""
                )
                .strip()
                .lower()
            )
            if s_name:
                eligible_by_name[s_name] = s if isinstance(s, dict) else s.model_dump(mode="json")

    route_stops: list[RouteStopInput] = []
    editorial_recs = list(state.get("editorial_recommendations", []))

    dep_date = brief.start_date
    dep_time = time(9, 0)
    departure_local = datetime.combine(dep_date, dep_time, tzinfo=trip_tz)
    departure_dt = departure_local.astimezone(UTC)

    # 1. Origin stop
    route_stops.append(
        RouteStopInput(
            name=brief.origin_name,
            location=Point(
                latitude=float(brief.origin_lat),
                longitude=float(brief.origin_lng),
            ),
            stop_type=StopType.ORIGIN,
            is_must_visit=True,
            dwell_seconds=0,
        )
    )

    # 2. Brief destinations
    for d in brief.destinations:
        d_name_norm = d.name.strip().lower()
        if d_name_norm in excluded_names:
            logger.warning(
                "Brief destination %s contradicted by evidence; excluded from solver schedule",
                d.name,
            )
            continue

        if d_name_norm in eligible_by_name:
            verified_stop = eligible_by_name[d_name_norm]
            brief_d = dest_by_norm_name.get(d_name_norm)
            dt_w_start, dt_w_end = _resolve_stop_window(verified_stop, brief, brief_d, trip_tz)

            p_id = verified_stop.get("place_id")
            route_stops.append(
                RouteStopInput(
                    place_id=uuid.UUID(str(p_id)) if p_id else None,
                    name=d.name,
                    location=Point(latitude=float(d.latitude), longitude=float(d.longitude)),
                    stop_type=StopType.DESTINATION,
                    is_must_visit=True,
                    dwell_seconds=d.stay_days * 180 * 60,
                    window_start=dt_w_start,
                    window_end=dt_w_end,
                )
            )
        else:
            logger.info(
                "Brief destination %s lacks verified existence evidence; retained in editorial recommendations",
                d.name,
            )
            editorial_recs.append(
                {
                    "primary_name": d.name,
                    "category": "destination",
                    "point": {"lat": float(d.latitude), "lng": float(d.longitude)},
                    "uncertainty": "EDITORIAL_DISCOVERY",
                    "is_candidate": True,
                    "existence_status": CandidateExistenceStatus.UNVERIFIED.value,
                    "hours_status": CandidateHoursStatus.USER_SUPPLIED_UNVERIFIED.value,
                    "is_hours_assumed": True,
                    "diagnostics": {
                        "rejection_reason": "User supplied brief destination lacks verified existence evidence; excluded from solver schedule"
                    },
                }
            )

    # 3. Discovered candidates with verified existence
    if eligible_stops_data is not None:
        for s in eligible_stops_data:
            s_name = (
                str(
                    s.get("primary_name")
                    if isinstance(s, dict)
                    else getattr(s, "primary_name", "") or ""
                )
                .strip()
                .lower()
            )
            if s_name in brief_names or s_name in excluded_names:
                continue
            exist_st = s.get("existence_status")
            if exist_st and exist_st not in (
                CandidateExistenceStatus.VERIFIED,
                CandidateExistenceStatus.VERIFIED.value,
            ):
                continue
            pt = s.get("point", {})
            raw_lat = pt.get("lat") if isinstance(pt, dict) else getattr(pt, "lat", 0.0)
            raw_lng = pt.get("lng") if isinstance(pt, dict) else getattr(pt, "lng", 0.0)
            lat_val = float(raw_lat) if raw_lat is not None else 0.0
            lng_val = float(raw_lng) if raw_lng is not None else 0.0

            dt_w_start, dt_w_end = _resolve_stop_window(s, brief, None, trip_tz)

            p_id = s.get("place_id")
            route_stops.append(
                RouteStopInput(
                    place_id=uuid.UUID(str(p_id)) if p_id else None,
                    name=s["primary_name"],
                    location=Point(latitude=lat_val, longitude=lng_val),
                    stop_type=StopType.DESTINATION,
                    is_must_visit=True,
                    dwell_seconds=s.get("suggested_stay_minutes", 60) * 60,
                    window_start=dt_w_start,
                    window_end=dt_w_end,
                )
            )
    else:
        discovered = state.get("discovered_places", [])
        for p in discovered:
            p_name = p.get("name", "")
            if p_name.strip().lower() not in brief_names:
                editorial_recs.append(
                    {
                        "primary_name": p_name,
                        "uncertainty": "EDITORIAL_DISCOVERY",
                        "is_candidate": True,
                        "diagnostics": {
                            "rejection_reason": "Eligibility gate output missing; unverified proposal excluded from solver schedule"
                        },
                    }
                )

    owner_id_raw = state.get("owner_id")
    trip_id_raw = state.get("trip_id")
    execution_id_raw = state.get("execution_id")

    preferred_mode = brief.preferred_modes[0] if brief.preferred_modes else TransportMode.CAR_PETROL

    route_req = RouteRequest(
        owner_id=uuid.UUID(str(owner_id_raw)) if owner_id_raw else None,
        trip_id=uuid.UUID(str(trip_id_raw)) if trip_id_raw else None,
        execution_id=uuid.UUID(str(execution_id_raw)) if execution_id_raw else None,
        departure_at=departure_dt,
        stops=route_stops,
        objective=RouteObjective.FASTEST,
        is_round_trip=False,
        time_limit_seconds=10,
        transport_mode=preferred_mode,
    )

    exec_uuid_str = str(execution_id_raw or uuid.uuid4())
    opt_task_id = (
        uuid.UUID(str(state["optimizer_task_id"]))
        if state.get("optimizer_task_id")
        else uuid.uuid4()
    )
    idemp_key = f"{exec_uuid_str}:optimizer:solve"
    task_spec = {
        "task_id": str(opt_task_id),
        "worker": "OPTIMIZER",
        "action": "solve",
        "payload": route_req.model_dump(mode="json"),
        "idempotency_key": idemp_key,
        "deadline_seconds": 3600,
        "max_attempts": 3,
    }

    return {
        "current_step": "route_optimizer_dispatched",
        "status": "RUNNING",
        "pending_tasks": [task_spec],
        "editorial_recommendations": editorial_recs,
        "optimizer_task_id": str(opt_task_id),
        "optimizer_idempotency_key": idemp_key,
        "timezone": str(trip_tz),
    }


def await_route_results_node(state: TripPlanningState) -> dict[str, Any]:
    """Interrupt boundary node.

    Halts execution before this node on start. When resumed after optimizer worker task completion,
    this node clears pending_tasks to ensure no duplicate tasks are dispatched on replay.
    """
    return {
        "pending_tasks": [],
        "current_step": "awaiting_route_results",
    }


async def consume_route_result_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Consumes RouteOptimizerWorkerOutput and constructs the scheduled itinerary.

    Conforms to spec 0007 AC-4, AC-5, AC-9:
    1. Extracts RouteOptimizerWorkerOutput bound specifically to the optimizer_task_id dispatched
       for this execution. Earlier browser/evidence task outputs, including errors, are never mistaken
       for the route optimizer result.
    2. If routing provider was unavailable or failed (Valhalla outage), adheres to 0005 fail closed policy:
       sets route_id=None, routing_status="ROUTING_UNAVAILABLE", and records diagnostic notice
       without generating guessed straight-line matrices or road polylines.
    3. In production mode, fails closed if the persisted route cannot be loaded and validated for
       the expected owner, execution, and trip as supported by RouteModel. Applies across FEASIBLE,
       PARTIAL, and INFEASIBLE outcomes. Never invents a feasible schedule or timestamps when lookup fails.
    4. Offline fixture mode remains explicitly isolated.
    """
    task_outputs = state.get("task_outputs", {})
    worker_output_dict: dict[str, Any] | None = None
    target_task_id = state.get("optimizer_task_id")

    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    session_factory = configurable.get("session_factory") or state.get("_session_factory")
    is_offline_fixture = bool(state.get("_offline_fixture_mode", False))

    if target_task_id and str(target_task_id) in task_outputs:
        worker_output_dict = task_outputs[str(target_task_id)]
    elif target_task_id and session_factory is not None and not is_offline_fixture:
        try:
            target_uuid = uuid.UUID(str(target_task_id))
            expected_exec_uuid = (
                uuid.UUID(str(state["execution_id"]))
                if state.get("execution_id")
                else None
            )
            expected_owner_uuid = (
                uuid.UUID(str(state["owner_id"]))
                if state.get("owner_id")
                else None
            )
            async with session_factory() as session:
                from functions.orchestrator.execution.models import (
                    ExecutionModel,
                    TaskModel,
                    WorkerResultModel,
                )

                t_stmt = select(TaskModel).where(TaskModel.id == target_uuid)
                task_row = (await session.execute(t_stmt)).scalar_one_or_none()

                if task_row is not None:
                    # Validate execution boundary
                    if (
                        expected_exec_uuid is not None
                        and task_row.execution_id != expected_exec_uuid
                    ):
                        logger.warning(
                            "Task %s execution %s does not match expected %s",
                            target_uuid,
                            task_row.execution_id,
                            expected_exec_uuid,
                        )
                        task_row = None

                if task_row is not None and expected_owner_uuid is not None:
                    # Validate tenant boundary via parent execution
                    exec_stmt = select(ExecutionModel.owner_id).where(
                        ExecutionModel.id == task_row.execution_id
                    )
                    exec_owner = (await session.execute(exec_stmt)).scalar_one_or_none()
                    if exec_owner is not None and exec_owner != expected_owner_uuid:
                        logger.warning(
                            "Task %s execution owner %s does not match expected owner %s",
                            target_uuid,
                            exec_owner,
                            expected_owner_uuid,
                        )
                        task_row = None

                if task_row is not None and task_row.status == "COMPLETED":
                    res_stmt = None
                    if task_row.result_id is not None:
                        res_stmt = select(WorkerResultModel).where(
                            WorkerResultModel.id == task_row.result_id
                        )
                    else:
                        res_stmt = (
                            select(WorkerResultModel)
                            .where(
                                WorkerResultModel.task_id == target_uuid,
                                WorkerResultModel.execution_id == task_row.execution_id,
                                WorkerResultModel.attempt == task_row.attempt,
                            )
                            .order_by(WorkerResultModel.finished_at.desc())
                        )
                    worker_res = (await session.execute(res_stmt)).scalar_one_or_none()
                    if (
                        worker_res is not None
                        and worker_res.execution_id == task_row.execution_id
                        and worker_res.task_id == target_uuid
                        and worker_res.attempt == task_row.attempt
                    ):
                        if worker_res.status in ("SUCCESS", "COMPLETED"):
                            worker_output_dict = worker_res.output_ref
                        elif worker_res.status in ("FAILED", "ERROR") or worker_res.error:
                            worker_output_dict = {
                                "status": "FAILED",
                                "error": worker_res.error or "optimizer_task_failed",
                            }
                elif task_row is not None and task_row.status in ("FAILED", "CANCELLED"):
                    worker_output_dict = {
                        "status": task_row.status,
                        "error": f"optimizer_task_{task_row.status.lower()}",
                    }
        except Exception as e:
            logger.warning(
                "Error fetching task output from database for task %s: %s", target_task_id, e
            )

    if worker_output_dict is None and not target_task_id:
        # Fallback inspection for offline fixture or legacy executions without bound optimizer_task_id:
        # Strictly match entries with optimizer markers. Never match generic browser or evidence errors.
        for _task_id, out_val in task_outputs.items():
            if isinstance(out_val, dict):
                if (
                    "route_id" in out_val
                    or out_val.get("worker") in ("OPTIMIZER", "ROUTE_OPTIMIZER")
                    or "stops_count" in out_val
                    or "routing_status" in out_val
                ):
                    worker_output_dict = out_val
                    break

    if worker_output_dict is None:
        if is_offline_fixture:
            brief_val = TripBrief.model_validate(state["brief"])
            trip_tz = resolve_trip_timezone(brief_val, state)
            eligible_stops_val = state.get("eligible_stops", [])
            places_list: list[dict[str, Any]] = []
            brief_dest_names = {d.name.strip().lower() for d in brief_val.destinations}
            for d in brief_val.destinations:
                d_norm = d.name.strip().lower()
                for es in eligible_stops_val:
                    if (es.get("primary_name") or "").strip().lower() == d_norm:
                        is_ass = bool(
                            es.get("is_hours_assumed", False)
                            or es.get("hours_status") == "HOURS_ASSUMED"
                        )
                        places_list.append(
                            {
                                "name": d.name,
                                "lat": float(d.latitude),
                                "lng": float(d.longitude),
                                "confidence": float(d.confidence),
                                "stay_minutes": d.stay_days * 180,
                                "is_hours_assumed": is_ass,
                                "hours_status": es.get(
                                    "hours_status", "HOURS_ASSUMED" if is_ass else "VERIFIED_HOURS"
                                ),
                                "window_start": es.get("window_start")
                                or (time(9, 0) if is_ass else None),
                                "window_end": es.get("window_end")
                                or (time(18, 0) if is_ass else None),
                            }
                        )
                        break
            for es in eligible_stops_val:
                es_name = es.get("primary_name", "")
                if es_name.strip().lower() not in brief_dest_names:
                    exist_st = es.get("existence_status")
                    if exist_st in (
                        CandidateExistenceStatus.VERIFIED,
                        CandidateExistenceStatus.VERIFIED.value,
                    ):
                        pt = es.get("point", {})
                        lat_val = float(pt.get("lat") or 0.0)
                        lng_val = float(pt.get("lng") or 0.0)
                        is_ass = bool(
                            es.get("is_hours_assumed", False)
                            or es.get("hours_status") == "HOURS_ASSUMED"
                        )
                        places_list.append(
                            {
                                "name": es_name,
                                "lat": lat_val,
                                "lng": lng_val,
                                "confidence": 1.0,
                                "stay_minutes": es.get("suggested_stay_minutes", 60),
                                "is_hours_assumed": is_ass,
                                "hours_status": es.get(
                                    "hours_status", "HOURS_ASSUMED" if is_ass else "VERIFIED_HOURS"
                                ),
                                "window_start": es.get("window_start")
                                or (time(9, 0) if is_ass else None),
                                "window_end": es.get("window_end")
                                or (time(18, 0) if is_ass else None),
                            }
                        )
            start_dt = datetime.combine(
                brief_val.start_date, time(9, 0), tzinfo=trip_tz
            ).astimezone(UTC)
            schedule = ItineraryScheduler.solve_schedule(
                origin_name=brief_val.origin_name,
                origin_lat=brief_val.origin_lat,
                origin_lng=brief_val.origin_lng,
                stops=places_list,
                start_datetime=start_dt,
            )
            return {
                "current_step": "route_result_consumed",
                "route_id": str(uuid.uuid4()),
                "route_status": "FEASIBLE",
                "routing_status": "FEASIBLE",
                "route_diagnostics": [],
                "optimized_schedule": schedule.model_dump(mode="json"),
            }

        logger.warning(
            "No optimizer output found for execution %s; failing closed to routing unavailable",
            state.get("execution_id"),
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [
                "Road routing engine unavailable; stops preserved without driving schedule or route polyline"
            ],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    is_failed_worker = (
        worker_output_dict.get("status") in ("FAILED", "CANCELLED")
        or worker_output_dict.get("routing_status") == "ROUTING_UNAVAILABLE"
        or "error" in worker_output_dict
    )
    if is_failed_worker and "route_id" not in worker_output_dict:
        logger.warning(
            "Optimizer worker reported failure or outage: %s",
            worker_output_dict.get("error") or worker_output_dict.get("status"),
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [
                "Road routing engine unavailable; stops preserved without driving schedule or route polyline"
            ],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    try:
        worker_output = RouteOptimizerWorkerOutput.model_validate(worker_output_dict)
    except Exception as exc:
        logger.warning(
            "Malformed RouteOptimizerWorkerOutput (%s); falling back to unavailable", exc
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [f"Invalid optimizer worker output: {exc}"],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    route_id = worker_output.route_id
    route_status_str = str(
        worker_output.status.value
        if hasattr(worker_output.status, "value")
        else worker_output.status
    )
    route_diags = list(worker_output.diagnostics)

    if is_offline_fixture:
        if worker_output.status == RouteStatus.INFEASIBLE:
            empty_schedule = OptimizedSchedule(
                stops=[],
                legs=[],
                total_travel_minutes=0,
                is_optimal=False,
            )
            return {
                "current_step": "route_result_consumed",
                "route_id": str(route_id),
                "route_status": "INFEASIBLE",
                "routing_status": "INFEASIBLE",
                "route_diagnostics": route_diags
                or ["Route overconstrained; impossible stop windows"],
                "optimized_schedule": empty_schedule.model_dump(mode="json"),
            }
        # Offline fixture schedule formulation
        brief_data = state.get("brief", {})
        origin_name = brief_data.get("origin_name", "Origin")
        origin_lat = float(brief_data.get("origin_lat", 0.0))
        origin_lng = float(brief_data.get("origin_lng", 0.0))
        sched_stops: list[ScheduledStop] = [
            ScheduledStop(
                position=0,
                name=origin_name,
                latitude=origin_lat,
                longitude=origin_lng,
                arrival_time=datetime.now(UTC),
                departure_time=datetime.now(UTC),
                stay_duration_minutes=0,
                is_hours_assumed=False,
                hours_status="ORIGIN",
            )
        ]
        for idx, es in enumerate(state.get("eligible_stops", [])):
            pt = es.get("point", {})
            e_lat = float(pt.get("lat") or 0.0)
            e_lng = float(pt.get("lng") or 0.0)
            is_assumed = bool(
                es.get("is_hours_assumed", False) or es.get("hours_status") == "HOURS_ASSUMED"
            )
            sched_stops.append(
                ScheduledStop(
                    position=idx + 1,
                    name=es.get("primary_name", f"Stop {idx + 1}"),
                    latitude=e_lat,
                    longitude=e_lng,
                    arrival_time=datetime.now(UTC),
                    departure_time=datetime.now(UTC),
                    stay_duration_minutes=es.get("suggested_stay_minutes", 60),
                    is_hours_assumed=is_assumed,
                    hours_status=es.get(
                        "hours_status", "HOURS_ASSUMED" if is_assumed else "VERIFIED_HOURS"
                    ),
                )
            )
        schedule = OptimizedSchedule(
            stops=sched_stops,
            legs=[],
            total_travel_minutes=worker_output.total_travel_seconds // 60,
            is_optimal=route_status_str in ("FEASIBLE", "PARTIAL"),
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": str(route_id),
            "route_status": route_status_str,
            "routing_status": route_status_str,
            "route_diagnostics": route_diags,
            "optimized_schedule": schedule.model_dump(mode="json"),
        }

    # Production Mode: Fail closed if persisted route cannot be loaded and validated
    owner_id_raw = state.get("owner_id")
    if session_factory is None or not owner_id_raw:
        logger.error(
            "Production mode requires session_factory and owner_id to load and validate persisted route"
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [
                "Database session factory and owner ID required in production mode to load and validate persisted route"
            ],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    owner_id = uuid.UUID(str(owner_id_raw))
    expected_exec_id = uuid.UUID(str(state["execution_id"])) if state.get("execution_id") else None
    expected_trip_id = uuid.UUID(str(state["trip_id"])) if state.get("trip_id") else None

    route_model: RouteModel | None = None
    async with session_factory() as session:
        route_repo = RouteRepository(session)
        route_model = await route_repo.get_route_by_id(route_id, owner_id=owner_id)

    if route_model is None:
        logger.error(
            "Persisted route %s not found or tenant isolation barrier rejected access for owner %s",
            route_id,
            owner_id,
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [
                f"Persisted route {route_id} not found or tenant isolation barrier rejected access for owner {owner_id}"
            ],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    if route_model.owner_id != owner_id:
        logger.error(
            "Tenant isolation breach: route owner %s does not match expected owner %s",
            route_model.owner_id,
            owner_id,
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [f"Tenant isolation barrier rejected access to route {route_id}"],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    if not expected_exec_id and not expected_trip_id:
        logger.error(
            "Production mode requires execution_id or trip_id in state to validate route association"
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [
                "Execution ID and Trip ID missing from state; cannot establish route association"
            ],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    if expected_exec_id is not None and (
        route_model.execution_id is None or route_model.execution_id != expected_exec_id
    ):
        logger.error(
            "Execution mismatch or missing: route execution %s does not match expected %s",
            route_model.execution_id,
            expected_exec_id,
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        diag = (
            f"Persisted route execution ID is missing; expected {expected_exec_id}"
            if route_model.execution_id is None
            else f"Persisted route execution {route_model.execution_id} does not match current execution {expected_exec_id}"
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [diag],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    if expected_trip_id is not None and (
        route_model.trip_id is None or route_model.trip_id != expected_trip_id
    ):
        logger.error(
            "Trip mismatch or missing: route trip %s does not match expected %s",
            route_model.trip_id,
            expected_trip_id,
        )
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        diag = (
            f"Persisted route trip ID is missing; expected {expected_trip_id}"
            if route_model.trip_id is None
            else f"Persisted route trip {route_model.trip_id} does not match current trip {expected_trip_id}"
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": None,
            "route_status": "ROUTING_UNAVAILABLE",
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_diagnostics": [diag],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    if worker_output.status == RouteStatus.INFEASIBLE or route_model.status == "INFEASIBLE":
        empty_schedule = OptimizedSchedule(
            stops=[],
            legs=[],
            total_travel_minutes=0,
            is_optimal=False,
        )
        return {
            "current_step": "route_result_consumed",
            "route_id": str(route_id),
            "route_status": "INFEASIBLE",
            "routing_status": "INFEASIBLE",
            "route_diagnostics": route_diags or ["Route overconstrained; impossible stop windows"],
            "optimized_schedule": empty_schedule.model_dump(mode="json"),
        }

    # Verify that planned schedule timestamps exist on persisted stops in production
    for s in route_model.stops:
        if s.planned_arrival_at is None or s.planned_departure_at is None:
            logger.error(
                "Persisted route stop %s lacks planned schedule timestamps in production",
                s.name,
            )
            empty_schedule = OptimizedSchedule(
                stops=[],
                legs=[],
                total_travel_minutes=0,
                is_optimal=False,
            )
            return {
                "current_step": "route_result_consumed",
                "route_id": None,
                "route_status": "ROUTING_UNAVAILABLE",
                "routing_status": "ROUTING_UNAVAILABLE",
                "route_diagnostics": [
                    f"Persisted route stop '{s.name}' lacks planned arrival or departure schedule timestamps"
                ],
                "optimized_schedule": empty_schedule.model_dump(mode="json"),
            }

    # Construct scheduled stops and legs from validated route model
    eligible_stops = state.get("eligible_stops", [])
    stop_hours_map: dict[str, tuple[bool, str]] = {}
    for es in eligible_stops:
        es_name = (
            str(
                es.get("primary_name")
                if isinstance(es, dict)
                else getattr(es, "primary_name", "") or ""
            )
            .strip()
            .lower()
        )
        is_assumed = bool(
            es.get("is_hours_assumed", False) or es.get("hours_status") == "HOURS_ASSUMED"
        )
        h_status = str(es.get("hours_status", "HOURS_ASSUMED" if is_assumed else "VERIFIED_HOURS"))
        stop_hours_map[es_name] = (is_assumed, h_status)

    sched_stops = []
    for s in route_model.stops:
        s_name_norm = s.name.strip().lower()
        is_origin = s.stop_order == 0 or s.stop_type == StopType.ORIGIN
        if is_origin:
            is_assumed, h_status = False, "ORIGIN"
        elif s_name_norm in stop_hours_map:
            is_assumed, h_status = stop_hours_map[s_name_norm]
        else:
            is_assumed, h_status = True, "HOURS_ASSUMED"

        from geoalchemy2.elements import WKBElement, WKTElement

        if isinstance(s.location, (WKBElement, WKTElement)):
            pt = to_shape(s.location)
            lat_val = float(pt.y)
            lng_val = float(pt.x)
        elif hasattr(s.location, "y") and hasattr(s.location, "x"):
            lat_val = float(s.location.y)
            lng_val = float(s.location.x)
        elif hasattr(s.location, "latitude"):
            lat_val = float(s.location.latitude)
            lng_val = float(s.location.longitude)
        else:
            lat_val = 0.0
            lng_val = 0.0

        assert s.planned_arrival_at is not None
        assert s.planned_departure_at is not None
        sched_stops.append(
            ScheduledStop(
                position=s.stop_order,
                name=s.name,
                latitude=lat_val,
                longitude=lng_val,
                arrival_time=s.planned_arrival_at,
                departure_time=s.planned_departure_at,
                stay_duration_minutes=s.dwell_seconds // 60,
                window_start=s.window_start,
                window_end=s.window_end,
                is_hours_assumed=is_assumed,
                hours_status=h_status,
            )
        )

    sched_legs = []
    stops_by_id = {s.id: s.name for s in route_model.stops}
    for leg in route_model.legs:
        from_name = stops_by_id.get(leg.from_stop_id, "Origin")
        to_name = stops_by_id.get(leg.to_stop_id, "Destination")
        sched_legs.append(
            ScheduledLeg(
                from_stop=from_name,
                to_stop=to_name,
                duration_minutes=leg.travel_seconds // 60,
                distance_km=round(leg.distance_m / 1000.0, 3),
            )
        )

    total_travel_mins: int = worker_output.total_travel_seconds // 60
    is_optimal = route_status_str in ("FEASIBLE", "PARTIAL")

    schedule = OptimizedSchedule(
        stops=sched_stops,
        legs=sched_legs,
        total_travel_minutes=total_travel_mins,
        is_optimal=is_optimal,
    )

    return {
        "current_step": "route_result_consumed",
        "route_id": str(route_id),
        "route_status": route_status_str,
        "routing_status": route_status_str,
        "route_diagnostics": route_diags,
        "optimized_schedule": schedule.model_dump(mode="json"),
    }


def solve_schedule_node(state: TripPlanningState) -> dict[str, Any]:
    """Applies OR Tools scheduling solver to generate optimal sequence and timestamps.

    Conforms to spec 0007 AC-3:
    1. Consumes candidate eligibility gate output.
    2. Applies source backed existence policy to brief destinations:
       * If contradicted by evidence, excludes them from solver schedule.
       * If verified by supporting evidence, schedules with verified status.
       * If unverified, retains in editorial recommendations without falsely claiming verification.
    3. Discovered candidate proposals enter solver schedule ONLY when existence is VERIFIED.
       Unverified discovered proposals remain strictly in editorial recommendations.
    4. Safe path for missing gate output: if gate output is missing, unverified proposals
       are safely excluded from the solver schedule and preserved in editorial recommendations.
    5. Verified or assumed opening windows are passed to the solver and preserved with accurate labeling.
    """
    brief = TripBrief.model_validate(state["brief"])
    brief_names = {d.name.strip().lower() for d in brief.destinations}

    excluded_candidates = state.get("excluded_candidates", [])
    excluded_names = {
        str(c.get("primary_name") if isinstance(c, dict) else getattr(c, "primary_name", "") or "")
        .strip()
        .lower()
        for c in excluded_candidates
        if c is not None
    }

    eligible_stops_data = state.get("eligible_stops")
    eligible_by_name: dict[str, dict[str, Any]] = {}
    if eligible_stops_data is not None:
        for s in eligible_stops_data:
            s_name = (
                str(
                    s.get("primary_name")
                    if isinstance(s, dict)
                    else getattr(s, "primary_name", "") or ""
                )
                .strip()
                .lower()
            )
            if s_name:
                eligible_by_name[s_name] = s if isinstance(s, dict) else s.model_dump(mode="json")

    places: list[dict[str, Any]] = []
    editorial_recs = list(state.get("editorial_recommendations", []))

    # 1. Evaluate brief destinations against evidence
    for d in brief.destinations:
        d_name_norm = d.name.strip().lower()
        if d_name_norm in excluded_names:
            logger.warning(
                "Brief destination %s contradicted by evidence; excluded from solver schedule",
                d.name,
            )
            continue

        if d_name_norm in eligible_by_name:
            verified_stop = eligible_by_name[d_name_norm]
            is_assumed = bool(
                verified_stop.get("is_hours_assumed", False)
                or verified_stop.get("hours_status") == "HOURS_ASSUMED"
            )
            w_start = verified_stop.get("window_start") or (time(9, 0) if is_assumed else None)
            w_end = verified_stop.get("window_end") or (time(18, 0) if is_assumed else None)
            places.append(
                {
                    "name": d.name,
                    "lat": float(d.latitude),
                    "lng": float(d.longitude),
                    "confidence": float(d.confidence),
                    "stay_minutes": d.stay_days * 180,
                    "is_hours_assumed": is_assumed,
                    "hours_status": verified_stop.get(
                        "hours_status", "HOURS_ASSUMED" if is_assumed else "VERIFIED_HOURS"
                    ),
                    "window_start": w_start,
                    "window_end": w_end,
                }
            )
        else:
            # Unverified brief destination:
            # Under spec 0007 AC-3, unverified candidate places never enter the route optimizer solver as stops.
            # They are preserved in editorial recommendations with honest user supplied labeling.
            logger.info(
                "Brief destination %s lacks verified existence evidence; retained in editorial recommendations",
                d.name,
            )
            editorial_recs.append(
                {
                    "primary_name": d.name,
                    "category": "destination",
                    "point": {"lat": float(d.latitude), "lng": float(d.longitude)},
                    "uncertainty": "EDITORIAL_DISCOVERY",
                    "is_candidate": True,
                    "existence_status": CandidateExistenceStatus.UNVERIFIED.value,
                    "hours_status": CandidateHoursStatus.USER_SUPPLIED_UNVERIFIED.value,
                    "is_hours_assumed": True,
                    "diagnostics": {
                        "rejection_reason": "User supplied brief destination lacks verified existence evidence; excluded from solver schedule"
                    },
                }
            )

    # 2. Add verified candidate stops (discovered candidates with verified existence)
    if eligible_stops_data is not None:
        for s in eligible_stops_data:
            s_name = (
                str(
                    s.get("primary_name")
                    if isinstance(s, dict)
                    else getattr(s, "primary_name", "") or ""
                )
                .strip()
                .lower()
            )
            if s_name in brief_names:
                continue
            exist_st = s.get("existence_status")
            if exist_st and exist_st not in (
                CandidateExistenceStatus.VERIFIED,
                CandidateExistenceStatus.VERIFIED.value,
            ):
                continue
            pt = s.get("point", {})
            raw_lat = pt.get("lat") if isinstance(pt, dict) else getattr(pt, "lat", 0.0)
            raw_lng = pt.get("lng") if isinstance(pt, dict) else getattr(pt, "lng", 0.0)
            lat_val = float(raw_lat) if raw_lat is not None else 0.0
            lng_val = float(raw_lng) if raw_lng is not None else 0.0
            is_assumed = bool(
                s.get("is_hours_assumed", False) or s.get("hours_status") == "HOURS_ASSUMED"
            )
            w_start = s.get("window_start") or (time(9, 0) if is_assumed else None)
            w_end = s.get("window_end") or (time(18, 0) if is_assumed else None)
            places.append(
                {
                    "name": s["primary_name"],
                    "lat": lat_val,
                    "lng": lng_val,
                    "confidence": 1.0,
                    "stay_minutes": s.get("suggested_stay_minutes", 60),
                    "is_hours_assumed": is_assumed,
                    "hours_status": s.get(
                        "hours_status", "HOURS_ASSUMED" if is_assumed else "VERIFIED_HOURS"
                    ),
                    "window_start": w_start,
                    "window_end": w_end,
                }
            )
    else:
        # Safe path for missing gate output:
        # Discovered places are unverified proposals. Do NOT schedule them!
        # Relegate any unverified proposals to editorial recommendations with diagnostic.
        discovered = state.get("discovered_places", [])
        for p in discovered:
            p_name = p.get("name", "")
            if p_name.strip().lower() not in brief_names:
                editorial_recs.append(
                    {
                        "primary_name": p_name,
                        "uncertainty": "EDITORIAL_DISCOVERY",
                        "is_candidate": True,
                        "diagnostics": {
                            "rejection_reason": "Eligibility gate output missing; unverified proposal excluded from solver schedule"
                        },
                    }
                )

    trip_tz = resolve_trip_timezone(brief, state)
    start_dt = datetime.combine(
        brief.start_date, time(hour=9, minute=0), tzinfo=trip_tz
    ).astimezone(UTC)

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
        "editorial_recommendations": editorial_recs,
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
        "editorial_recommendations": state.get("editorial_recommendations", []),
        "excluded_candidates": state.get("excluded_candidates", []),
        "route_id": state.get("route_id"),
        "route_status": state.get("route_status"),
        "routing_status": state.get("routing_status"),
        "route_diagnostics": state.get("route_diagnostics", []),
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
    re.compile(
        r"\b(?:content|details?|data|service|source)\s+(?:is\s+)?unavailable\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:temporarily|currently)\s+unavailable\b", re.IGNORECASE),
    re.compile(
        r"\bno\s+(?:results?|information|details?|data|records?)\s+(?:found|available)\b",
        re.IGNORECASE,
    ),
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
    url = getattr(obs, "canonical_url", None) or (
        obs.get("canonical_url") if isinstance(obs, dict) else ""
    )
    method = getattr(obs, "extraction_method", None) or (
        obs.get("extraction_method") if isinstance(obs, dict) else ""
    )
    content_hash = getattr(obs, "content_hash", None) or (
        obs.get("content_hash") if isinstance(obs, dict) else ""
    )
    excerpt = getattr(obs, "excerpt_text", None) or (
        obs.get("excerpt_text") if isinstance(obs, dict) else ""
    )
    artifact_ref = getattr(obs, "artifact_ref", None) or (
        obs.get("artifact_ref") if isinstance(obs, dict) else {}
    )

    # 1. Reject NEEDS_BROWSER dynamic rendering placeholders and error/unavailable metadata
    if method == "NEEDS_BROWSER":
        return False
    if isinstance(artifact_ref, dict):
        if artifact_ref.get("browser_reason"):
            return False
        status_val = str(artifact_ref.get("status") or "").upper().strip()
        if status_val in ("UNAVAILABLE", "BLOCKED", "TIMED_OUT", "FAILED", "ERROR", "NOT_FOUND"):
            return False
        if (
            artifact_ref.get("unavailable_reason")
            or artifact_ref.get("unavailable_sources")
            or artifact_ref.get("error")
        ):
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
            or (
                destinations[0].get("name")
                if destinations and isinstance(destinations[0], dict)
                else None
            )
            or (destinations[0].name if destinations and hasattr(destinations[0], "name") else None)
            or ""
        )
    else:
        destinations = getattr(brief, "destinations", [])
        primary_destination = getattr(brief, "destination_name", "") or ""
    dest_name = primary_destination or (
        destinations[0].get("name")
        if destinations and isinstance(destinations[0], dict)
        else "Unknown"
    )

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
        [
            r
            for r in extracted_requests
            if r.get("canonical_url", r.get("url")) not in already_dispatched
        ]
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
                destinations_info.append(
                    {
                        "name": str(name).strip(),
                        "lat": d.get("latitude"),
                        "lng": d.get("longitude"),
                    }
                )
        elif hasattr(d, "name") and d.name:
            destinations_info.append(
                {
                    "name": str(d.name).strip(),
                    "lat": getattr(d, "latitude", None),
                    "lng": getattr(d, "longitude", None),
                }
            )

    if not destinations_info and primary_destination:
        destinations_info.append({"name": primary_destination.strip(), "lat": None, "lng": None})

    if not destinations_info:
        for p in discovered_places:
            if isinstance(p, dict) and p.get("name"):
                destinations_info.append(
                    {
                        "name": str(p["name"]).strip(),
                        "lat": p.get("lat") or p.get("latitude"),
                        "lng": p.get("lng") or p.get("longitude"),
                    }
                )
            elif hasattr(p, "name") and p.name:
                destinations_info.append(
                    {
                        "name": str(p.name).strip(),
                        "lat": getattr(p, "lat", None) or getattr(p, "latitude", None),
                        "lng": getattr(p, "lng", None) or getattr(p, "longitude", None),
                    }
                )

    # 3. Check evidence prerequisites via fail-closed authoritative validation
    db_session = state.get("_db_session")
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode") or configurable.get("offline_fixture_mode")
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
        outputs_to_inspect = list(task_outputs.values())
        if isinstance(task_outputs, dict):
            outputs_to_inspect.append(task_outputs)
        for output in outputs_to_inspect:
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
            validated_evidence_satisfied = (
                len(offline_matched) == len(destinations_info) and valid_persisted_count > 0
            )
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

    remaining_search_budget = int(
        state.get("remaining_search_budget", 1 if not static_search_completed else 0)
    )

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
    dest_name = brief.get("destination_name") or (
        destinations[0].get("name") if destinations else "Unknown"
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
        eligible_actions=eligible_choices
        if eligible_choices
        else [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
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
        policy_derived_eligible_choices=eligible_choices
        if eligible_choices
        else [DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
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
                    policy_version=recorded.get(
                        "policy_version", LayaPolicyValidator.POLICY_VERSION
                    ),
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
                    factory = (
                        session_factory if session_factory is not None else get_session_factory()
                    )
                    async with factory() as session:
                        repo = LayaDecisionRepository(session=session)
                        reused = await repo.find_reusable_decision(decision_req, active_phase=phase)
                except Exception as repo_exc:
                    logger.debug("Database reusable decision lookup missed: %s", repo_exc)

        if reused is not None:
            envelope = reused
        else:
            adapter = state.get("_laya_adapter") or configurable.get("laya_adapter")
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
        "static_search_completed": True,
        "remaining_search_budget": 0,
    }


def route_evidence_decision_edge(state: TripPlanningState) -> str:
    """Routes based on evidence decision action."""
    action = state.get("evidence_routing_action")
    if action == DecisionAction.DISPATCH_BROWSER_ESCALATION.value:
        return TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE
    elif action == DecisionAction.DISPATCH_EVIDENCE_SEARCH.value:
        return TripPlanningNode.AWAIT_EVIDENCE_RESULTS
    return TripPlanningNode.EVALUATE_CANDIDATES


def escalate_dynamic_evidence_edge(state: TripPlanningState) -> str:
    """Routes to await_evidence_results if browser tasks are pending, else to evaluate_candidates."""
    if state.get("pending_tasks"):
        return TripPlanningNode.AWAIT_EVIDENCE_RESULTS
    return TripPlanningNode.EVALUATE_CANDIDATES


# --- Graph Construction ---


def build_trip_planning_graph() -> StateGraph[TripPlanningState, Any, Any, Any]:
    graph: StateGraph[TripPlanningState, Any, Any, Any] = StateGraph(TripPlanningState)

    graph.add_node(TripPlanningNode.PARSE_BRIEF, parse_and_validate_brief_node)
    graph.add_node(TripPlanningNode.DISCOVER_CANDIDATES, discover_candidates_node)
    graph.add_node(TripPlanningNode.ROUTE_EVIDENCE_DECISION, route_evidence_decision_node)
    graph.add_node(TripPlanningNode.AWAIT_EVIDENCE_RESULTS, await_evidence_results_node)
    graph.add_node(TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE, escalate_dynamic_evidence_node)
    graph.add_node(TripPlanningNode.EVALUATE_CANDIDATES, evaluate_candidates_node)
    graph.add_node(TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER, dispatch_route_optimizer_node)
    graph.add_node(TripPlanningNode.AWAIT_ROUTE_RESULTS, await_route_results_node)
    graph.add_node(TripPlanningNode.CONSUME_ROUTE_RESULT, consume_route_result_node)
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
            TripPlanningNode.EVALUATE_CANDIDATES: TripPlanningNode.EVALUATE_CANDIDATES,
        },
    )
    graph.add_conditional_edges(
        TripPlanningNode.ESCALATE_DYNAMIC_EVIDENCE,
        escalate_dynamic_evidence_edge,
        {
            TripPlanningNode.AWAIT_EVIDENCE_RESULTS: TripPlanningNode.AWAIT_EVIDENCE_RESULTS,
            TripPlanningNode.EVALUATE_CANDIDATES: TripPlanningNode.EVALUATE_CANDIDATES,
        },
    )
    graph.add_edge(
        TripPlanningNode.AWAIT_EVIDENCE_RESULTS, TripPlanningNode.ROUTE_EVIDENCE_DECISION
    )
    graph.add_edge(TripPlanningNode.EVALUATE_CANDIDATES, TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER)
    graph.add_edge(TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER, TripPlanningNode.AWAIT_ROUTE_RESULTS)
    graph.add_edge(TripPlanningNode.AWAIT_ROUTE_RESULTS, TripPlanningNode.CONSUME_ROUTE_RESULT)
    graph.add_edge(TripPlanningNode.CONSUME_ROUTE_RESULT, TripPlanningNode.COMPUTE_BUDGET)
    graph.add_edge(TripPlanningNode.COMPUTE_BUDGET, TripPlanningNode.SYNTHESIZE_PROPOSAL)
    graph.add_edge(TripPlanningNode.SYNTHESIZE_PROPOSAL, END)

    return graph


def compile_trip_planning_workflow(
    checkpointer: Any | None = None,
    interrupt_before: list[str] | None = None,
) -> Any:
    """Compiles the trip planning StateGraph with the required yield/interrupt boundaries."""
    if interrupt_before is None:
        if checkpointer is not None:
            interrupt_before = [
                TripPlanningNode.AWAIT_EVIDENCE_RESULTS,
                TripPlanningNode.AWAIT_ROUTE_RESULTS,
            ]
        else:
            interrupt_before = [TripPlanningNode.AWAIT_EVIDENCE_RESULTS]
    return build_trip_planning_graph().compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )


trip_planning_workflow = compile_trip_planning_workflow()
