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
from contracts.evidence import EvidenceClass
from contracts.llm import (
    PlaceProposal,
    RankingRequest,
    ResearchRequest,
    ResearchResponse,
)
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
from contracts.weather import (
    CorridorWeatherRequest,
    WeatherAdvisoryStatus,
    WeatherStopInput,
)
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
from functions.orchestrator.evidence.query_planner import (
    DEFAULT_MAX_QUERIES,
    QueryPlanningService,
)
from functions.orchestrator.llm.factory import create_llm_client
from functions.orchestrator.llm.port import HostedLlmClientPort
from functions.orchestrator.places.candidate_service import (
    CandidateRegistrationService,
)
from functions.orchestrator.places.schemas import CoordinatePoint
from functions.orchestrator.routing.models import RouteModel
from functions.orchestrator.routing.repository import RouteRepository
from functions.orchestrator.weather.factory import create_weather_client
from functions.orchestrator.weather.port import WeatherClientPort

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
    ranked_places: list[dict[str, Any]]
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
    route_geometry: dict[str, Any] | list[Any] | None
    optimizer_task_id: str | None
    optimizer_idempotency_key: str | None
    timezone: str | None
    weather_advisories: list[dict[str, Any]]
    weather_status: str | None
    proposal_status: str | None
    provider_diagnostics: dict[str, Any]
    food_research: dict[str, Any]
    accommodation_research: dict[str, Any]
    advisory_research: dict[str, Any]
    advisory_status: str | None
    advisories: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    planned_search_queries: list[Any]
    covered_destinations: list[str]
    uncovered_destinations: list[str]
    coverage_reason: str | None
    _llm_client: HostedLlmClientPort
    _weather_client: WeatherClientPort
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
    RANK_CANDIDATES = "rank_candidates"
    DISPATCH_ROUTE_OPTIMIZER = "dispatch_route_optimizer"
    AWAIT_ROUTE_RESULTS = "await_route_results"
    CONSUME_ROUTE_RESULT = "consume_route_result"
    FETCH_WEATHER = "fetch_weather"
    SOLVE_SCHEDULE = "solve_schedule"
    COMPUTE_BUDGET = "compute_budget"
    SYNTHESIZE_PROPOSAL = "synthesize_proposal"
    PUBLISH_PROPOSAL = "publish_proposal"


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


async def discover_candidates_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Discovers transit corridors and candidate destination points.

    Conforms to spec 0007 AC-1, AC-2, AC-3, AC-9:
    1. Preserves brief destinations as anchor stops.
    2. Queries configured language model client (Bedrock or fixture) for points of interest along the corridor.
    3. Keeps all generative proposals unverified with uncertainty=EDITORIAL_DISCOVERY and is_candidate=True.
    4. On provider failure or timeout, preserves brief destinations and returns honest partial diagnostics.
    5. Keeps fixture mode available only when explicitly configured.
    """
    brief = TripBrief.model_validate(state["brief"])
    discovered: list[dict[str, Any]] = list(state.get("discovered_places", []))
    existing_names = {
        str(d.get("name", "")).strip().lower()
        for d in discovered
        if isinstance(d, dict) and d.get("name")
    }

    # 1. Base seed: ensure brief destinations are present
    for d in brief.destinations:
        d_name_norm = d.name.strip().lower()
        if d_name_norm not in existing_names:
            discovered.append(
                {
                    "name": d.name,
                    "category": "destination",
                    "lat": float(d.latitude),
                    "lng": float(d.longitude),
                    "latitude": float(d.latitude),
                    "longitude": float(d.longitude),
                    "confidence": float(d.confidence),
                    "stay_minutes": d.stay_days * 180,  # 3 hours per stay day
                    "uncertainty": "EDITORIAL_DISCOVERY",
                    "is_candidate": True,
                }
            )
            existing_names.add(d_name_norm)

    # 2. Resolve configured LLM client
    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode") or configurable.get("offline_fixture_mode")
    )
    llm_client = state.get("_llm_client") or configurable.get("llm_client")

    if llm_client is None:
        settings = get_settings()
        # Keep fixture mode available only when explicitly configured
        if is_offline_fixture or settings.LLM_PROVIDER == "fixture":
            llm_client = create_llm_client(settings=settings, provider="fixture")
        else:
            llm_client = create_llm_client(settings=settings)

    provider_diagnostics = dict(state.get("provider_diagnostics", {}))
    errors = list(state.get("errors", []))

    # 3. Query LLM research along the travel corridor
    pref_mode = (
        brief.preferred_modes[0].value
        if brief.preferred_modes and hasattr(brief.preferred_modes[0], "value")
        else (str(brief.preferred_modes[0]) if brief.preferred_modes else "car_petrol")
    )
    brief_dict = state.get("brief", {})
    budget_val = str(brief_dict.get("budget_tier", "MODERATE"))
    traveler_prefs = list(brief.dietary_preferences) + list(brief.accessibility_requirements)
    if "traveler_preferences" in brief_dict and isinstance(
        brief_dict["traveler_preferences"], list
    ):
        traveler_prefs.extend(str(p) for p in brief_dict["traveler_preferences"])

    trip_title = str(brief_dict.get("trip_title") or brief_dict.get("title") or "").strip()
    brief_summary = (
        trip_title
        if trip_title
        else f"Trip from {brief.origin_name} to {', '.join(d.name for d in brief.destinations)}"
    )

    research_req = ResearchRequest(
        brief_summary=brief_summary,
        origin_name=brief.origin_name,
        destination_names=[d.name for d in brief.destinations],
        traveler_preferences=traveler_prefs,
        budget_tier=budget_val,
        transport_mode=pref_mode,
    )

    research_resp: ResearchResponse | None = None
    try:
        research_resp = await llm_client.research_places(research_req)
        for prop in research_resp.candidate_places:
            prop_name_norm = prop.name.strip().lower()
            if prop_name_norm in existing_names:
                continue

            # Invariant: model proposals must remain strictly unverified candidate proposals
            discovered.append(
                {
                    "name": prop.name,
                    "category": prop.category,
                    "lat": prop.estimated_latitude,
                    "lng": prop.estimated_longitude,
                    "latitude": prop.estimated_latitude,
                    "longitude": prop.estimated_longitude,
                    "confidence": prop.fit_score,
                    "stay_minutes": prop.suggested_stay_minutes,
                    "price_estimate_inr": prop.price_estimate_inr,
                    "address": prop.address,
                    "opening_hours": prop.opening_hours,
                    "uncertainty": "EDITORIAL_DISCOVERY",
                    "is_candidate": True,
                }
            )
            existing_names.add(prop_name_norm)
    except Exception as exc:
        diag_msg = f"LLM research provider failed ({type(exc).__name__}). Preserved brief destination stops."
        logger.warning("LLM research failed in discover_candidates_node: %s", type(exc).__name__)
        provider_diagnostics["llm_research"] = diag_msg
        errors.append(diag_msg)

    result: dict[str, Any] = {
        "current_step": "candidates_discovered",
        "discovered_places": discovered,
        "provider_diagnostics": provider_diagnostics,
    }
    planned_from_research = getattr(research_resp, "planned_queries", None) if research_resp is not None else None
    if planned_from_research:
        result["planned_search_queries"] = planned_from_research
        covered = getattr(research_resp, "covered_destinations", None) or getattr(planned_from_research, "covered_destinations", None)
        if covered:
            result["covered_destinations"] = list(covered)
        uncovered = getattr(research_resp, "uncovered_destinations", None) or getattr(planned_from_research, "uncovered_destinations", None)
        if uncovered is not None:
            result["uncovered_destinations"] = list(uncovered)
        cov_reason = getattr(research_resp, "coverage_reason", None) or getattr(planned_from_research, "coverage_reason", None)
        if cov_reason:
            result["coverage_reason"] = cov_reason
    if errors:
        result["errors"] = errors
    return result


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

    # Extract evidence-backed place candidates from collected search/scraping observations
    obs_for_extraction: list[Any] = list(fixture_obs_list)
    if not is_offline_fixture and exec_uuid is not None:
        try:
            from functions.evidence_collector.models import SourceObservationModel

            obs_stmt = select(SourceObservationModel).where(
                SourceObservationModel.execution_id == exec_uuid
            )
            if db_session is not None:
                obs_res = await db_session.execute(obs_stmt)
                obs_for_extraction.extend(list(obs_res.scalars().all()))
            elif session_factory is not None:
                async with session_factory() as session:
                    obs_res = await session.execute(obs_stmt)
                    obs_for_extraction.extend(list(obs_res.scalars().all()))
        except Exception as obs_err:
            logger.debug("Failed querying observations for candidate extraction: %s", obs_err)

    if obs_for_extraction:
        from functions.orchestrator.evidence.place_extraction import PlaceExtractionService

        default_lat = brief.destinations[0].latitude if brief.destinations else None
        default_lng = brief.destinations[0].longitude if brief.destinations else None
        evidence_proposals = PlaceExtractionService.extract_candidates_from_observations(
            obs_for_extraction,
            default_destination_lat=default_lat,
            default_destination_lng=default_lng,
        )
        existing_names = {p.name.strip().lower() for p in candidate_proposals}
        for ep in evidence_proposals:
            if ep.name.strip().lower() not in existing_names:
                candidate_proposals.append(ep)
                existing_names.add(ep.name.strip().lower())

    proposals = brief_proposals + candidate_proposals

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
            from functions.orchestrator.evidence.claim_ingestion import (
                ingest_claims_from_observations,
            )

            if exec_uuid is not None:
                await ingest_claims_from_observations(
                    session=session,
                    execution_id=exec_uuid,
                    candidates=valid_cands,
                    owner_id=owner_uuid,
                )
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

    serialized_obs: list[dict[str, Any]] = []
    for obs in obs_for_extraction:
        if isinstance(obs, dict):
            serialized_obs.append(obs)
        else:
            r_at = getattr(obs, "retrieved_at", None)
            p_at = getattr(obs, "published_at", None)
            c_at = getattr(obs, "created_at", None)
            obs_id = getattr(obs, "id", None)
            exec_id = getattr(obs, "execution_id", None)
            serialized_obs.append(
                {
                    "id": str(obs_id) if obs_id else None,
                    "execution_id": str(exec_id) if exec_id else None,
                    "source_url": getattr(obs, "source_url", None),
                    "canonical_url": getattr(obs, "canonical_url", None),
                    "publisher": getattr(obs, "publisher", None),
                    "content_hash": getattr(obs, "content_hash", None),
                    "retrieved_at": (
                        r_at.isoformat()
                        if r_at is not None and hasattr(r_at, "isoformat")
                        else (str(r_at) if r_at is not None else None)
                    ),
                    "published_at": (
                        p_at.isoformat()
                        if p_at is not None and hasattr(p_at, "isoformat")
                        else (str(p_at) if p_at is not None else None)
                    ),
                    "artifact_ref": getattr(obs, "artifact_ref", None),
                    "excerpt_text": getattr(obs, "excerpt_text", None),
                    "excerpt_locator": getattr(obs, "excerpt_locator", None),
                    "extraction_method": getattr(obs, "extraction_method", None),
                    "created_at": (
                        c_at.isoformat()
                        if c_at is not None and hasattr(c_at, "isoformat")
                        else (str(c_at) if c_at is not None else None)
                    ),
                }
            )

    return {
        "current_step": "candidates_evaluated",
        "validated_candidates": [c.model_dump(mode="json") for c in valid_cands],
        "candidate_eligibility": elig_report.model_dump(mode="json"),
        "eligible_stops": [s.model_dump(mode="json") for s in elig_report.eligible_stops],
        "editorial_recommendations": [
            r.model_dump(mode="json") for r in elig_report.editorial_recommendations
        ],
        "excluded_candidates": [x.model_dump(mode="json") for x in all_excluded],
        "observations": serialized_obs,
    }


async def rank_candidates_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Scores candidates by brief interest alignment and stay duration feasibility.

    Conforms to spec 0007 AC-2, AC-3, AC-9:
    1. Evaluates eligible stops and editorial recommendations against traveler constraints.
    2. Preserves candidate uncertainty and existence status without bypassing eligibility.
    3. On provider failure or timeout, preserves candidate order and records honest diagnostics.
    4. Keeps fixture mode available only when explicitly configured.
    """
    eligible_stops = list(state.get("eligible_stops", []))
    editorial_recs = list(state.get("editorial_recommendations", []))
    brief_data = state.get("brief", {})
    brief = TripBrief.model_validate(brief_data)

    if not eligible_stops and not editorial_recs:
        return {
            "current_step": "candidates_ranked",
            "ranked_places": [],
        }

    # Convert candidates to PlaceProposal objects for ranking
    proposals_to_rank: list[PlaceProposal] = []
    seen_names: set[str] = set()

    for s in eligible_stops:
        name = str(s.get("primary_name") or s.get("name", "")).strip()
        if not name or name.lower() in seen_names:
            continue
        pt = s.get("point", {})
        lat_val = pt.get("lat") if isinstance(pt, dict) else s.get("lat", 0.0)
        lng_val = pt.get("lng") if isinstance(pt, dict) else s.get("lng", 0.0)
        proposals_to_rank.append(
            PlaceProposal(
                name=name,
                category=str(s.get("category", "attraction")).strip().lower(),
                estimated_latitude=float(lat_val or 0.0),
                estimated_longitude=float(lng_val or 0.0),
                description=str(s.get("description", "")).strip(),
                fit_score=float(s.get("fit_score", 1.0)),
                suggested_stay_minutes=int(s.get("suggested_stay_minutes", 60)),
                price_estimate_inr=float(s["price_estimate_inr"])
                if s.get("price_estimate_inr") is not None
                else None,
                uncertainty=str(s.get("uncertainty", "EDITORIAL_DISCOVERY")),
                is_candidate=bool(s.get("is_candidate", True)),
                address=s.get("address"),
            )
        )
        seen_names.add(name.lower())

    for r in editorial_recs:
        name = str(r.get("primary_name") or r.get("name", "")).strip()
        if not name or name.lower() in seen_names:
            continue
        pt = r.get("point", {})
        lat_val = pt.get("lat") if isinstance(pt, dict) else r.get("lat", 0.0)
        lng_val = pt.get("lng") if isinstance(pt, dict) else r.get("lng", 0.0)
        proposals_to_rank.append(
            PlaceProposal(
                name=name,
                category=str(r.get("category", "attraction")).strip().lower(),
                estimated_latitude=float(lat_val or 0.0),
                estimated_longitude=float(lng_val or 0.0),
                description=str(r.get("description", "")).strip(),
                fit_score=float(r.get("fit_score", 1.0)),
                suggested_stay_minutes=int(r.get("suggested_stay_minutes", 60)),
                price_estimate_inr=float(r["price_estimate_inr"])
                if r.get("price_estimate_inr") is not None
                else None,
                uncertainty=str(r.get("uncertainty", "EDITORIAL_DISCOVERY")),
                is_candidate=bool(r.get("is_candidate", True)),
                address=r.get("address"),
            )
        )
        seen_names.add(name.lower())

    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode") or configurable.get("offline_fixture_mode")
    )
    llm_client = state.get("_llm_client") or configurable.get("llm_client")

    if llm_client is None:
        settings = get_settings()
        if is_offline_fixture or settings.LLM_PROVIDER == "fixture":
            llm_client = create_llm_client(settings=settings, provider="fixture")
        else:
            llm_client = create_llm_client(settings=settings)

    ranking_req = RankingRequest(
        candidate_places=proposals_to_rank,
        trip_brief=brief.model_dump(mode="json"),
        traveler_constraints=[],
        max_places_to_select=max(10, len(proposals_to_rank)),
    )

    provider_diagnostics = dict(state.get("provider_diagnostics", {}))
    errors = list(state.get("errors", []))
    ranked_places_data: list[dict[str, Any]] = []

    try:
        ranking_resp = await llm_client.rank_places(ranking_req)
        ranked_places_data = [rp.model_dump(mode="json") for rp in ranking_resp.ranked_places]

        # Reorder eligible_stops according to the ranking if candidates were ranked
        rank_order = {rp.place.name.strip().lower(): rp.rank for rp in ranking_resp.ranked_places}
        if rank_order:
            eligible_stops.sort(
                key=lambda s: rank_order.get(
                    (s.get("primary_name") or s.get("name", "")).strip().lower(), 999
                )
            )
    except Exception as exc:
        diag_msg = f"LLM ranking provider failed ({type(exc).__name__}). Preserved existing candidate order."
        logger.warning("LLM ranking failed in rank_candidates_node: %s", type(exc).__name__)
        provider_diagnostics["llm_ranking"] = diag_msg
        errors.append(diag_msg)

    result: dict[str, Any] = {
        "current_step": "candidates_ranked",
        "ranked_places": ranked_places_data,
        "eligible_stops": eligible_stops,
        "provider_diagnostics": provider_diagnostics,
    }
    if errors:
        result["errors"] = errors
    return result


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
                uuid.UUID(str(state["execution_id"])) if state.get("execution_id") else None
            )
            expected_owner_uuid = (
                uuid.UUID(str(state["owner_id"])) if state.get("owner_id") else None
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
            "route_geometry": (
                worker_output_dict.get("route_geometry")
                or worker_output_dict.get("geometry")
                or state.get("route_geometry")
            ),
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

    road_coords: list[list[float]] = []
    for leg in route_model.legs:
        if leg.geometry and isinstance(leg.geometry, dict):
            coords = leg.geometry.get("coordinates")
            if isinstance(coords, list):
                if road_coords and coords and coords[0] == road_coords[-1]:
                    road_coords.extend(coords[1:])
                else:
                    road_coords.extend(coords)

    resolved_geom = (
        {"type": "LineString", "coordinates": road_coords}
        if road_coords
        else state.get("route_geometry")
    )

    return {
        "current_step": "route_result_consumed",
        "route_id": str(route_id),
        "route_status": route_status_str,
        "routing_status": route_status_str,
        "route_diagnostics": route_diags,
        "optimized_schedule": schedule.model_dump(mode="json"),
        "route_geometry": resolved_geom,
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


async def fetch_weather_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Fetches corridor weather advisories for actual stops and times.

    Conforms to spec 0007 Slice 2 and AC-6:
    1. Extracts scheduled stops and arrival and departure timestamps from optimized_schedule.
    2. If routing was unavailable or has no schedule, samples stops from eligible_stops
       or brief destinations without inventing timestamps or road schedules.
    3. Resolves configured weather client (Open Meteo or deterministic fixture).
    4. Gating against 16 day meteorological horizon, preserves severe cautions,
       monsoon risks, ghat warnings, unknown values, and Open Meteo attribution.
    5. On weather service failure or timeout, preserves the partial proposal
       with WEATHER_UNAVAILABLE status and honest diagnostics without crashing.
    """
    configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode") or configurable.get("offline_fixture_mode")
    )
    weather_client = state.get("_weather_client") or configurable.get("weather_client")

    if weather_client is None:
        settings = get_settings()
        if is_offline_fixture or settings.WEATHER_PROVIDER == "fixture":
            weather_client = create_weather_client(settings=settings, provider="fixture")
        else:
            weather_client = create_weather_client(settings=settings)

    provider_diagnostics = dict(state.get("provider_diagnostics", {}))

    # 1. Resolve trip departure date and timezone
    brief_data = state.get("brief", {})
    tz_str = (
        state.get("timezone")
        or (
            brief_data.get("timezone")
            if isinstance(brief_data, dict)
            else getattr(brief_data, "timezone", "Asia/Kolkata")
        )
        or "Asia/Kolkata"
    )

    dep_date_raw = (
        brief_data.get("start_date")
        if isinstance(brief_data, dict)
        else getattr(brief_data, "start_date", None)
    )
    departure_date: date
    if isinstance(dep_date_raw, datetime):
        departure_date = dep_date_raw.date()
    elif isinstance(dep_date_raw, date):
        departure_date = dep_date_raw
    elif isinstance(dep_date_raw, str):
        try:
            departure_date = date.fromisoformat(dep_date_raw[:10])
        except Exception:
            departure_date = date.today()
    else:
        departure_date = date.today()

    # 2. Extract stops
    weather_stops: list[WeatherStopInput] = []
    opt_schedule = state.get("optimized_schedule", {})
    scheduled_stops = (
        opt_schedule.get("stops", [])
        if isinstance(opt_schedule, dict)
        else getattr(opt_schedule, "stops", [])
    )

    if scheduled_stops:
        for s in scheduled_stops:
            s_name = str(s.get("name") if isinstance(s, dict) else getattr(s, "name", "Stop"))
            raw_lat = s.get("latitude") if isinstance(s, dict) else getattr(s, "latitude", None)
            s_lat = float(raw_lat) if raw_lat is not None else 0.0

            raw_lng = s.get("longitude") if isinstance(s, dict) else getattr(s, "longitude", None)
            s_lng = float(raw_lng) if raw_lng is not None else 0.0
            arr_raw = (
                s.get("arrival_time") if isinstance(s, dict) else getattr(s, "arrival_time", None)
            )
            dep_raw = (
                s.get("departure_time")
                if isinstance(s, dict)
                else getattr(s, "departure_time", None)
            )

            arr_dt: datetime | None = None
            if isinstance(arr_raw, datetime):
                arr_dt = arr_raw
            elif isinstance(arr_raw, str):
                try:
                    arr_dt = datetime.fromisoformat(arr_raw)
                except Exception:
                    arr_dt = None

            dep_dt: datetime | None = None
            if isinstance(dep_raw, datetime):
                dep_dt = dep_raw
            elif isinstance(dep_raw, str):
                try:
                    dep_dt = datetime.fromisoformat(dep_raw)
                except Exception:
                    dep_dt = None

            is_ghat = bool(
                s.get("is_ghat_section", False)
                if isinstance(s, dict)
                else getattr(s, "is_ghat_section", False)
            ) or ("ghat" in s_name.lower())

            weather_stops.append(
                WeatherStopInput(
                    name=s_name,
                    latitude=s_lat,
                    longitude=s_lng,
                    arrival_time=arr_dt,
                    departure_time=dep_dt,
                    is_ghat_section=is_ghat,
                )
            )
    else:
        # Routing unavailable or no schedule: sample from eligible stops or brief destinations
        # Invariant: do not invent arrival/departure times or road schedule
        eligible = state.get("eligible_stops", [])
        for es in eligible:
            es_name = str(es.get("primary_name") or es.get("name") or "Stop")
            pt = es.get("point", {}) if isinstance(es, dict) else getattr(es, "point", {})
            pt_lat = pt.get("lat") if isinstance(pt, dict) else getattr(pt, "lat", None)
            es_lat = es.get("latitude") if isinstance(es, dict) else getattr(es, "latitude", None)
            lat = float(pt_lat if pt_lat is not None else (es_lat if es_lat is not None else 0.0))

            pt_lng = pt.get("lng") if isinstance(pt, dict) else getattr(pt, "lng", None)
            es_lng = es.get("longitude") if isinstance(es, dict) else getattr(es, "longitude", None)
            lng = float(pt_lng if pt_lng is not None else (es_lng if es_lng is not None else 0.0))
            if lat != 0.0 and lng != 0.0:
                is_ghat = bool(es.get("is_ghat_section", False)) or ("ghat" in es_name.lower())
                weather_stops.append(
                    WeatherStopInput(
                        name=es_name,
                        latitude=lat,
                        longitude=lng,
                        arrival_time=None,
                        departure_time=None,
                        is_ghat_section=is_ghat,
                    )
                )

        if not weather_stops:
            # Fallback to brief origin and destinations if valid
            origin_name = str(
                brief_data.get("origin_name", "Origin")
                if isinstance(brief_data, dict)
                else getattr(brief_data, "origin_name", "Origin")
            )
            origin_lat = float(
                brief_data.get("origin_lat", 0.0)
                if isinstance(brief_data, dict)
                else getattr(brief_data, "origin_lat", 0.0)
            )
            origin_lng = float(
                brief_data.get("origin_lng", 0.0)
                if isinstance(brief_data, dict)
                else getattr(brief_data, "origin_lng", 0.0)
            )
            if origin_lat != 0.0 and origin_lng != 0.0:
                weather_stops.append(
                    WeatherStopInput(
                        name=origin_name,
                        latitude=origin_lat,
                        longitude=origin_lng,
                        arrival_time=None,
                        departure_time=None,
                        is_ghat_section="ghat" in origin_name.lower(),
                    )
                )
            dest_list = (
                brief_data.get("destinations", [])
                if isinstance(brief_data, dict)
                else getattr(brief_data, "destinations", [])
            )
            for d in dest_list:
                d_name = str(
                    d.get("name", "Destination")
                    if isinstance(d, dict)
                    else getattr(d, "name", "Destination")
                )
                d_lat = float(
                    d.get("latitude", 0.0) if isinstance(d, dict) else getattr(d, "latitude", 0.0)
                )
                d_lng = float(
                    d.get("longitude", 0.0) if isinstance(d, dict) else getattr(d, "longitude", 0.0)
                )
                if d_lat != 0.0 and d_lng != 0.0:
                    weather_stops.append(
                        WeatherStopInput(
                            name=d_name,
                            latitude=d_lat,
                            longitude=d_lng,
                            arrival_time=None,
                            departure_time=None,
                            is_ghat_section="ghat" in d_name.lower(),
                        )
                    )

    if not weather_stops:
        logger.warning("No stops with coordinates available for weather evaluation")
        diag_entry = {
            "status": "WEATHER_UNAVAILABLE",
            "error": "No stops with coordinates available for weather evaluation; proposal preserved without weather",
            "attribution": "Weather data by Open Meteo.com",
        }
        provider_diagnostics["weather"] = diag_entry
        return {
            "current_step": "weather_fetched",
            "weather_status": WeatherAdvisoryStatus.WEATHER_UNAVAILABLE.value,
            "weather_advisories": [],
            "provider_diagnostics": provider_diagnostics,
        }

    weather_req = CorridorWeatherRequest(
        trip_id=str(state.get("trip_id")) if state.get("trip_id") else None,
        departure_date=departure_date,
        stops=weather_stops,
        timezone=tz_str,
    )

    try:
        report = await weather_client.get_corridor_weather(weather_req)
        status_val = report.status.value if hasattr(report.status, "value") else str(report.status)

        serialized_stops: list[dict[str, Any]] = []
        for s in report.stops:
            s_dict = s.model_dump(mode="json")
            if report.overall_caution and "overall_caution" not in s_dict:
                s_dict["overall_caution"] = report.overall_caution
            if report.ghat_caution and "ghat_caution" not in s_dict:
                s_dict["ghat_caution"] = report.ghat_caution
            s_dict["attribution"] = report.attribution
            serialized_stops.append(s_dict)

        lic_val = (
            report.licensing_status.value
            if hasattr(report.licensing_status, "value")
            else str(report.licensing_status)
        )
        weather_diag = {
            "provider": report.provider,
            "attribution": report.attribution,
            "licensing_status": lic_val,
            "is_commercial_licensed": report.is_commercial_licensed,
            "forecast_horizon_days": report.forecast_horizon_days,
            "overall_caution": report.overall_caution,
            "ghat_caution": report.ghat_caution,
        }
        weather_diag.update(report.diagnostics)
        provider_diagnostics["weather"] = weather_diag

        return {
            "current_step": "weather_fetched",
            "weather_status": status_val,
            "weather_advisories": serialized_stops,
            "provider_diagnostics": provider_diagnostics,
        }
    except Exception as exc:
        diag_msg = f"Weather provider failed ({type(exc).__name__}): {exc}"
        logger.warning("Weather fetch failed in fetch_weather_node: %s", type(exc).__name__)
        provider_diagnostics["weather"] = {
            "status": "WEATHER_UNAVAILABLE",
            "error": diag_msg,
            "attribution": "Weather data by Open Meteo.com",
        }
        return {
            "current_step": "weather_fetched",
            "weather_status": WeatherAdvisoryStatus.WEATHER_UNAVAILABLE.value,
            "weather_advisories": [],
            "provider_diagnostics": provider_diagnostics,
        }


async def compute_budget_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Calculates fuel, accommodation, and entrance fee budget using exact domain math.

    Conforms to AC-7:
    - Fuel costs labeled as ESTIMATED based on vehicle profiles.
    - Accommodation labeled as ESTIMATED based on stay duration benchmarks.
    - Verified entrance fees labeled as QUOTED (estimated=False) backed by evidence.
    - Unverified fees, missing fees, or unquoted tolls labeled as explicit UNKNOWN entries
      with documented reasons and provenance where available, never defaulting to zero.
    """
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

    # 3. Activities and entrance fees for stops (AC-7, AC-12)
    stops_data = schedule_data.get("stops", [])
    seen_entry_stops: set[str] = set()

    # Track candidate metadata, provenance, and categories
    place_categories: dict[str, str] = {}
    place_provenance: dict[str, str] = {}
    verified_fees: dict[str, tuple[float, str]] = {}
    cand_coords: dict[str, tuple[float, float]] = {}

    for cand in state.get("validated_candidates", []) + state.get("discovered_places", []):
        cand_name = (cand.get("primary_name") or cand.get("name") or "").strip().lower()
        place_id = str(cand["place_id"]) if cand.get("place_id") else None
        if cand_name:
            if cand.get("category"):
                place_categories[cand_name] = str(cand["category"]).lower()
            prov = cand.get("provenance")
            if isinstance(prov, dict) and prov.get("source"):
                place_provenance[cand_name] = str(prov["source"])
            elif place_id:
                place_provenance[cand_name] = f"place:{place_id}"
            if place_id:
                place_provenance[place_id] = place_provenance.get(cand_name, f"place:{place_id}")
                if cand.get("category"):
                    place_categories[place_id] = str(cand["category"]).lower()

        coords: tuple[float, float] | None = None
        if "lat" in cand and "lng" in cand and cand["lat"] is not None and cand["lng"] is not None:
            try:
                coords = (float(cand["lat"]), float(cand["lng"]))
            except (ValueError, TypeError):
                coords = None
        elif "point" in cand and isinstance(cand["point"], dict):
            pt = cand["point"]
            if pt.get("lat") is not None and pt.get("lng") is not None:
                try:
                    coords = (float(pt["lat"]), float(pt["lng"]))
                except (ValueError, TypeError):
                    coords = None
        if cand_name and coords:
            cand_coords[cand_name] = coords
        if place_id and coords:
            cand_coords[place_id] = coords

    # Helper function to match claim to stop using place ID and spatial distance
    def _match_fee_claim_to_stop(
        claim_pid: str | None,
        claim_subj: str,
        claim_coordinates: tuple[float, float] | None,
        target_pid: str | None,
        target_name: str,
        target_coordinates: tuple[float, float] | None,
    ) -> bool:
        # Place ID match
        if claim_pid is not None:
            if target_pid is not None and str(claim_pid) == str(target_pid):
                return True
            return False
        # Name-only match
        if claim_subj.strip().lower() != target_name.strip().lower():
            return False
        # Name-only claim requires established location match within 1.0 km
        if not claim_coordinates or not target_coordinates:
            return False
        from functions.orchestrator.places.corridor import haversine_distance_km

        dist = haversine_distance_km(
            target_coordinates[0],
            target_coordinates[1],
            claim_coordinates[0],
            claim_coordinates[1],
        )
        return dist <= 1.0

    configurable = config.get("configurable", {}) if config else {}
    session_factory = configurable.get("session_factory") or state.get("_session_factory")
    is_offline_fixture = bool(
        state.get("_offline_fixture_mode", False) or configurable.get("offline_fixture_mode", False)
    )

    execution_id_raw = state.get("execution_id")
    execution_uuid = uuid.UUID(str(execution_id_raw)) if execution_id_raw else None

    if is_offline_fixture:
        # Offline fixture mode: inspect claims in state['task_outputs']
        raw_claims: list[dict[str, Any]] = []
        task_outputs = state.get("task_outputs", {})
        if isinstance(task_outputs, dict):
            if "claims" in task_outputs and isinstance(task_outputs["claims"], list):
                raw_claims.extend(task_outputs["claims"])
            for val in task_outputs.values():
                if isinstance(val, dict) and "claims" in val and isinstance(val["claims"], list):
                    raw_claims.extend(val["claims"])

        for cl in raw_claims:
            cl_exec = cl.get("execution_id")
            if cl_exec and execution_id_raw and str(cl_exec) != str(execution_id_raw):
                continue

            cat = str(cl.get("category", "")).upper()
            if cat in ("FEES", "PRICE", "TICKET", "ENTRANCE"):
                ver = str(cl.get("verification", "")).upper()
                avail = str(cl.get("availability", "AVAILABLE")).upper()
                if ver == "VERIFIED" and avail != "UNAVAILABLE":
                    support = str(cl.get("support_type", "SUPPORTS")).upper()
                    if support == "REFUTES":
                        continue
                    freshness = str(cl.get("freshness", "FRESH")).upper()
                    if freshness == "STALE":
                        continue
                    valid_until = cl.get("valid_until")
                    if (
                        valid_until
                        and isinstance(valid_until, datetime)
                        and valid_until < datetime.now(UTC)
                    ):
                        continue

                    ev_refs = (
                        cl.get("evidence_ids")
                        or cl.get("evidence_refs")
                        or cl.get("observations")
                        or []
                    )
                    if ev_refs or cl.get("evidence_count", 0) > 0 or cl.get("evidence_links"):
                        tv = cl.get("typed_value") or {}
                        price_val = tv.get("price") or tv.get("amount") or tv.get("price_inr")
                        if price_val is not None:
                            fee_amount = float(price_val)
                            fee_source = (
                                str(ev_refs[0])
                                if isinstance(ev_refs, list) and ev_refs
                                else f"claim:{cl.get('id', 'verified')}"
                            )
                            cl_subj = str(cl.get("subject_id", "")).strip().lower()
                            cl_pid = str(cl.get("place_id")) if cl.get("place_id") else None

                            from functions.orchestrator.evidence.eligibility import (
                                DeterministicEvidenceEligibilityGate,
                            )

                            cl_coords = (
                                DeterministicEvidenceEligibilityGate._extract_coordinates_from_data(
                                    cl.get("geographic_scope")
                                )
                                or DeterministicEvidenceEligibilityGate._extract_coordinates_from_data(
                                    cl.get("typed_value")
                                )
                            )

                            for stop in stops_data:
                                s_name = (
                                    (stop.get("name") or stop.get("stop_id", "")).strip().lower()
                                )
                                s_pid = str(stop.get("place_id")) if stop.get("place_id") else None
                                if not s_pid:
                                    for cand in state.get("validated_candidates", []):
                                        if (
                                            cand.get("primary_name") or cand.get("name") or ""
                                        ).strip().lower() == s_name:
                                            if cand.get("place_id"):
                                                s_pid = str(cand["place_id"])
                                                break
                                s_coords = (
                                    (float(stop["lat"]), float(stop["lng"]))
                                    if stop.get("lat") and stop.get("lng")
                                    else cand_coords.get(s_name)
                                    or (cand_coords.get(s_pid) if s_pid else None)
                                )

                                if _match_fee_claim_to_stop(
                                    cl_pid, cl_subj, cl_coords, s_pid, s_name, s_coords
                                ):
                                    verified_fees[s_name] = (fee_amount, fee_source)
                                    if s_pid:
                                        verified_fees[s_pid] = (fee_amount, fee_source)

    else:
        # Production mode: query database for verified fee claims under authorized evidence policy
        # Raw claims in task_outputs or candidate fields are untrusted and must NOT be accepted
        if session_factory is not None and execution_uuid is not None:
            try:
                from functions.evidence_collector.models import SourceObservationModel
                from functions.orchestrator.evidence.eligibility import (
                    DeterministicEvidenceEligibilityGate,
                )
                from functions.orchestrator.evidence.models import ClaimEvidenceModel, ClaimModel
                from functions.orchestrator.evidence.policy import ClaimVerificationPolicy
                from functions.orchestrator.evidence.schemas import FreshnessStatus

                policy = ClaimVerificationPolicy()
                now = datetime.now(UTC)

                async with session_factory() as session:
                    claims_stmt = (
                        select(ClaimModel, ClaimEvidenceModel, SourceObservationModel)
                        .join(ClaimEvidenceModel, ClaimEvidenceModel.claim_id == ClaimModel.id)
                        .join(
                            SourceObservationModel,
                            SourceObservationModel.id == ClaimEvidenceModel.observation_id,
                        )
                        .where(
                            ClaimModel.execution_id == execution_uuid,
                            SourceObservationModel.execution_id == execution_uuid,
                            ClaimModel.category.in_(["FEES", "PRICE", "TICKET", "ENTRANCE"]),
                            ClaimModel.verification == "VERIFIED",
                            ClaimModel.availability == "AVAILABLE",
                            ClaimEvidenceModel.support_type == "SUPPORTS",
                        )
                    )
                    claims_rows = (await session.execute(claims_stmt)).all()
                    for claim_row, _ev_row, obs_row in claims_rows:
                        # Validate freshness per policy
                        freshness = policy.evaluate_freshness(
                            claim_row, published_at=obs_row.published_at, as_of=now
                        )
                        if freshness == FreshnessStatus.STALE:
                            continue

                        # Extract coordinates for name-only matching
                        claim_coords = (
                            DeterministicEvidenceEligibilityGate._extract_coordinates_from_data(
                                claim_row.geographic_scope
                            )
                            or DeterministicEvidenceEligibilityGate._extract_coordinates_from_data(
                                obs_row.artifact_ref
                            )
                        )

                        tv = claim_row.typed_value or {}
                        price_val = tv.get("price") or tv.get("amount") or tv.get("price_inr")
                        if price_val is None:
                            continue

                        fee_amount = float(price_val)
                        fee_source = f"obs:{obs_row.id}"
                        cl_subj = claim_row.subject_id.strip().lower()
                        cl_pid = str(claim_row.place_id) if claim_row.place_id else None

                        for stop in stops_data:
                            s_name = (stop.get("name") or stop.get("stop_id", "")).strip().lower()
                            s_pid = str(stop.get("place_id")) if stop.get("place_id") else None
                            if not s_pid:
                                for cand in state.get("validated_candidates", []):
                                    if (
                                        cand.get("primary_name") or cand.get("name") or ""
                                    ).strip().lower() == s_name:
                                        if cand.get("place_id"):
                                            s_pid = str(cand["place_id"])
                                            break
                            s_coords = (
                                (float(stop["lat"]), float(stop["lng"]))
                                if stop.get("lat") and stop.get("lng")
                                else cand_coords.get(s_name)
                                or (cand_coords.get(s_pid) if s_pid else None)
                            )

                            if _match_fee_claim_to_stop(
                                cl_pid, cl_subj, claim_coords, s_pid, s_name, s_coords
                            ):
                                verified_fees[s_name] = (fee_amount, fee_source)
                                if s_pid:
                                    verified_fees[s_pid] = (fee_amount, fee_source)
            except Exception as exc:
                logger.warning("Could not query database for verified fee claims: %s", exc)
        else:
            logger.info(
                "Production fee calculation: session_factory or execution_id missing, fees remain UNKNOWN"
            )

    for stop in stops_data:
        stop_name = stop.get("name") or stop.get("stop_id", "Stop")
        stop_type = stop.get("stop_type", "INTERMEDIATE")
        if stop_type == "DEPARTURE" or stop_name.lower() in seen_entry_stops:
            continue

        seen_entry_stops.add(stop_name.lower())
        stop_key = stop_name.lower()
        stop_place_id = str(stop.get("place_id")) if stop.get("place_id") else None

        verified_entry = verified_fees.get(stop_key) or (
            verified_fees.get(stop_place_id) if stop_place_id else None
        )

        if verified_entry is not None:
            price_val, source_ref = verified_entry
            all_budget_lines.append(
                BudgetLine(
                    category=BudgetCategory.ACTIVITIES,
                    amount=Decimal(str(price_val)),
                    currency=Currency.INR,
                    basis=f"entry_fee_{stop_name}",
                    source_ref=source_ref,
                    estimated=False,  # AC-7: verified entrance fees are QUOTED!
                )
            )
        else:
            cat_val = place_categories.get(stop_key)
            if not cat_val and stop_place_id:
                cat_val = place_categories.get(stop_place_id)
            cat = cat_val or "attraction"
            if (
                cat
                in (
                    "attraction",
                    "museum",
                    "park",
                    "heritage",
                    "monument",
                    "sightseeing",
                    "activity",
                )
                or stop_key in place_categories
            ):
                prov_ref = place_provenance.get(stop_key) or (
                    place_provenance.get(stop_place_id) if stop_place_id else f"stop:{stop_name}"
                )
                all_budget_lines.append(
                    BudgetLine(
                        category=BudgetCategory.ACTIVITIES,
                        amount=None,  # AC-7: unquoted fees are explicit UNKNOWN entries
                        currency=Currency.INR,
                        basis=f"entry_fee_{stop_name}",
                        unknown_reason=f"Entrance fee unverified for {stop_name}",
                        source_ref=prov_ref,
                    )
                )

    summary = BudgetSummary.from_lines(all_budget_lines, currency=Currency.INR)

    return {
        "current_step": "budget_computed",
        "budget_lines": [line.model_dump(mode="json") for line in all_budget_lines],
        "budget_summary": summary.model_dump(mode="json"),
    }


def _build_food_research(state: TripPlanningState) -> dict[str, Any]:
    """Assembles structured food research distinguishing regional dishes from dining venues."""
    from functions.orchestrator.evidence.place_extraction import (
        PlaceExtractionService,
    )

    eligible_stops = list(state.get("eligible_stops", []))
    editorial_recs = list(state.get("editorial_recommendations", []))
    all_candidates = eligible_stops + editorial_recs

    food_categories = {"restaurant", "street_food", "cafe", "dining", "cuisine", "food"}
    dining_items: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for c in all_candidates:
        if not isinstance(c, dict):
            continue
        cat = str(c.get("category", "")).lower()
        if cat not in food_categories:
            continue
        name = str(c.get("primary_name", "")).strip()
        if not name or name.lower() in seen_names:
            continue
        if PlaceExtractionService.is_culinary_dish(name):
            continue
        seen_names.add(name.lower())

        prov = c.get("provenance") or {}
        source_urls = list(prov.get("source_urls") or [])
        retrieval_time = prov.get("retrieval_time")
        is_verified = (
            str(c.get("existence_status", "")).upper()
            == CandidateExistenceStatus.VERIFIED.value
        )
        point_data = c.get("point")
        coords = (
            {"lat": point_data.get("lat"), "lng": point_data.get("lng")}
            if isinstance(point_data, dict)
            else None
        )

        dining_items.append(
            {
                "name": name,
                "category": cat,
                "address": c.get("diagnostics", {}).get("address") or c.get("address"),
                "coordinates": coords,
                "source_urls": source_urls,
                "source_url": source_urls[0] if source_urls else None,
                "retrieval_time": retrieval_time,
                "identity": prov.get("identity")
                or {"name": name, "category": cat},
                "uncertainty": "VERIFIED" if is_verified else "EDITORIAL_DISCOVERY",
                "is_verified": is_verified,
                "unavailable_details": list(prov.get("unavailable_details") or []),
                "conflicting_details": list(prov.get("conflicting_details") or []),
            }
        )

    # Extract regional culinary dishes from source observations
    raw_obs_list: list[Any] = list(state.get("observations") or [])
    task_outputs = state.get("task_outputs", {})
    if isinstance(task_outputs, dict):
        if "observations" in task_outputs and isinstance(
            task_outputs["observations"], list
        ):
            raw_obs_list.extend(task_outputs["observations"])
        for sub in task_outputs.values():
            if (
                isinstance(sub, dict)
                and "observations" in sub
                and isinstance(sub["observations"], list)
            ):
                raw_obs_list.extend(sub["observations"])

    local_dishes = PlaceExtractionService.extract_local_dishes_from_observations(
        raw_obs_list
    )

    if not local_dishes and not dining_items:
        food_status = "UNAVAILABLE"
        reason: str | None = "No usable source-backed food or dining evidence retrieved"
    elif not local_dishes:
        food_status = "PARTIAL"
        reason = "Dining recommendations retrieved without source-backed regional culinary dishes"
    elif not dining_items:
        food_status = "PARTIAL"
        reason = "Regional culinary dishes retrieved without verified dining venues"
    else:
        food_status = "COMPLETED"
        reason = None

    return {
        "status": food_status,
        "reason": reason,
        "local_dishes": local_dishes,
        "dining_recommendations": dining_items,
        "verified_dining_count": len([d for d in dining_items if d["is_verified"]]),
        "unverified_dining_count": len(
            [d for d in dining_items if not d["is_verified"]]
        ),
    }


def _build_accommodation_research(state: TripPlanningState) -> dict[str, Any]:
    """Assembles structured accommodation research strictly preserving indicative search status."""
    from functions.orchestrator.evidence.claim_ingestion import _extract_coordinates_from_dict
    from functions.orchestrator.evidence.place_extraction import _normalize_place_name
    from functions.orchestrator.places.corridor import haversine_distance_km

    task_outputs = state.get("task_outputs", {})
    raw_obs_list: list[Any] = list(state.get("observations") or [])
    if isinstance(task_outputs, dict):
        if "observations" in task_outputs and isinstance(
            task_outputs["observations"], list
        ):
            raw_obs_list.extend(task_outputs["observations"])
        for sub in task_outputs.values():
            if (
                isinstance(sub, dict)
                and "observations" in sub
                and isinstance(sub["observations"], list)
            ):
                raw_obs_list.extend(sub["observations"])

    distinct_property_modifiers = frozenset(
        {
            "annex",
            "wing",
            "tower",
            "towers",
            "block",
            "extension",
            "residency",
            "suites",
            "suite",
            "villa",
            "villas",
            "cottage",
            "cottages",
            "resort",
            "resorts",
            "heritage",
            "boutique",
            "express",
            "inn",
            "club",
            "spa",
            "apartment",
            "apartments",
            "homestay",
            "lodge",
            "motel",
            "hostel",
        }
    )

    def _find_matching_priced_obs(
        prop_name: str,
        cand_lat: float | None = None,
        cand_lng: float | None = None,
        cand_source_urls: list[str] | None = None,
        cand_obs_ids: list[str] | None = None,
    ) -> tuple[int | None, str | None, str | None, str | None, str | None]:
        norm_name = _normalize_place_name(prop_name)
        cand_tokens = norm_name.split()
        if not cand_tokens:
            return None, None, None, None, None

        source_urls_set = set(cand_source_urls or [])
        obs_ids_set = set(cand_obs_ids or [])

        for obs in raw_obs_list:
            art = (
                getattr(obs, "artifact_ref", None)
                or (obs.get("artifact_ref") if isinstance(obs, dict) else {})
                or {}
            )
            if not isinstance(art, dict):
                continue

            # 1. Observation must have a usable source URL
            raw_url = (
                getattr(obs, "canonical_url", None)
                or getattr(obs, "source_url", None)
                or (
                    obs.get("canonical_url") or obs.get("source_url")
                    if isinstance(obs, dict)
                    else None
                )
            )
            src_url = str(raw_url).strip() if raw_url else ""
            if not (src_url.startswith("http://") or src_url.startswith("https://")):
                continue

            # 2. Observation must have valid numeric price and currency
            p_val = art.get("price_per_night") or art.get("indicative_price_inr")
            c_val = art.get("currency")
            if p_val is None or not c_val:
                continue
            try:
                int_price = int(p_val)
            except (ValueError, TypeError):
                continue

            # 3. Property name matching and substring protection
            title = str(
                art.get("property_name")
                or art.get("title")
                or art.get("name")
                or ""
            ).strip()
            if not title:
                continue

            norm_title = _normalize_place_name(title)
            obs_tokens = norm_title.split()
            if not obs_tokens:
                continue

            obs_id = (
                str(getattr(obs, "id", None) or (obs.get("id") if isinstance(obs, dict) else ""))
                or None
            )

            is_same_name = False
            if norm_name == norm_title:
                is_same_name = True
            elif len(cand_tokens) == len(obs_tokens) and all(
                a == b or a.rstrip("s") == b.rstrip("s")
                for a, b in zip(cand_tokens, obs_tokens, strict=True)
            ):
                is_same_name = True
            else:
                extra_in_obs = set(obs_tokens) - set(cand_tokens)
                extra_in_cand = set(cand_tokens) - set(obs_tokens)

                # Distinct modifiers like annex or residency forbid substring sharing
                if (extra_in_obs & distinct_property_modifiers) or (
                    extra_in_cand & distinct_property_modifiers
                ):
                    continue

                # Difference must be strictly subset with verified spatial proximity
                obs_coords_check = _extract_coordinates_from_dict(art)
                if obs_coords_check is None:
                    geo_check = getattr(obs, "geographic_scope", None) or (
                        obs.get("geographic_scope") if isinstance(obs, dict) else None
                    )
                    if isinstance(geo_check, dict):
                        obs_coords_check = _extract_coordinates_from_dict(geo_check)

                if (
                    (set(cand_tokens).issubset(set(obs_tokens)) or set(obs_tokens).issubset(set(cand_tokens)))
                    and cand_lat is not None
                    and cand_lng is not None
                    and obs_coords_check is not None
                    and haversine_distance_km(cand_lat, cand_lng, obs_coords_check[0], obs_coords_check[1]) <= 1.0
                ):
                    is_same_name = True
                else:
                    continue

            if not is_same_name:
                continue

            # 4. Spatial / Location Identity verification
            obs_coords = _extract_coordinates_from_dict(art)
            if obs_coords is None:
                geo = getattr(obs, "geographic_scope", None) or (
                    obs.get("geographic_scope") if isinstance(obs, dict) else None
                )
                if isinstance(geo, dict):
                    obs_coords = _extract_coordinates_from_dict(geo)

            if cand_lat is not None and cand_lng is not None and obs_coords is not None:
                dist = haversine_distance_km(cand_lat, cand_lng, obs_coords[0], obs_coords[1])
                if dist > 1.0:
                    # Same named property located far away: reject
                    continue
            elif cand_lat is not None and cand_lng is not None and obs_coords is None:
                # Candidate has coordinates but observation has no location evidence: require explicit provenance
                has_provenance = (
                    src_url in source_urls_set
                    or (obs_id is not None and obs_id in obs_ids_set)
                )
                if not has_provenance:
                    continue

            r_time = getattr(obs, "retrieved_at", None) or (
                obs.get("retrieved_at") if isinstance(obs, dict) else None
            )
            r_str: str | None = None
            if isinstance(r_time, str):
                r_str = r_time
            elif r_time is not None and hasattr(r_time, "isoformat"):
                val = r_time.isoformat()
                if isinstance(val, str):
                    r_str = val

            return int_price, str(c_val), obs_id, src_url, r_str

        return None, None, None, None, None

    editorial_recs = list(state.get("editorial_recommendations", []))
    eligible_stops = list(state.get("eligible_stops", []))
    all_candidates = eligible_stops + editorial_recs

    indicative_options: list[dict[str, Any]] = []
    seen_hotels: set[str] = set()

    for c in all_candidates:
        if not isinstance(c, dict):
            continue
        cat = str(c.get("category", "")).lower()
        if cat not in ("lodging", "hotel"):
            continue
        name = str(c.get("primary_name", "")).strip()
        if not name or name.lower() in seen_hotels:
            continue
        seen_hotels.add(name.lower())

        prov = c.get("provenance") or {}
        source_urls = list(prov.get("source_urls") or [])
        retrieval_time = prov.get("retrieval_time")
        cand_obs_ids = list(prov.get("observation_ids") or [])

        # Extract candidate coordinates
        c_lat: float | None = None
        c_lng: float | None = None
        c_point = c.get("point")
        if isinstance(c_point, dict):
            try:
                if c_point.get("lat") is not None and c_point.get("lng") is not None:
                    c_lat = float(c_point["lat"])
                    c_lng = float(c_point["lng"])
            except (ValueError, TypeError):
                pass
        elif c_point is not None and hasattr(c_point, "lat") and hasattr(c_point, "lng"):
            try:
                p_lat = getattr(c_point, "lat", None)
                p_lng = getattr(c_point, "lng", None)
                if p_lat is not None and p_lng is not None:
                    c_lat = float(p_lat)
                    c_lng = float(p_lng)
            except (ValueError, TypeError):
                pass
        if c_lat is None:
            for k_lat, k_lng in (
                ("latitude", "longitude"),
                ("lat", "lng"),
                ("estimated_latitude", "estimated_longitude"),
            ):
                if c.get(k_lat) is not None and c.get(k_lng) is not None:
                    try:
                        c_lat = float(c[k_lat])
                        c_lng = float(c[k_lng])
                        break
                    except (ValueError, TypeError):
                        pass

        obs_price, obs_curr, obs_id, obs_url, obs_time = _find_matching_priced_obs(
            name, c_lat, c_lng, source_urls, cand_obs_ids
        )
        if obs_price is not None and obs_curr and obs_url:
            indicative_options.append(
                {
                    "property_name": name,
                    "price_per_night": obs_price,
                    "currency": obs_curr,
                    "observation_id": obs_id,
                    "source_url": obs_url,
                    "source_urls": [obs_url] if obs_url else source_urls,
                    "retrieval_time": obs_time or retrieval_time,
                    "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
                    "is_indicative_search": True,
                    "is_verified_offer": False,
                    "booking_confirmed": False,
                    "availability_status": "INDICATIVE_SEARCH_ONLY",
                    "uncertainty": "INDICATIVE_SEARCH",
                    "unavailable_details": [
                        "live_room_availability_unverified",
                        "booking_unconfirmed",
                    ],
                    "conflicting_details": [],
                }
            )
        else:
            indicative_options.append(
                {
                    "property_name": name,
                    "price_per_night": None,
                    "currency": None,
                    "observation_id": None,
                    "source_url": source_urls[0] if source_urls else None,
                    "source_urls": source_urls,
                    "retrieval_time": retrieval_time,
                    "evidence_class": None,
                    "is_indicative_search": False,
                    "is_verified_offer": False,
                    "booking_confirmed": False,
                    "availability_status": "PRICE_UNAVAILABLE",
                    "uncertainty": "UNVERIFIED",
                    "unavailable_details": [
                        "price_unsupported_by_source",
                        "live_room_availability_unverified",
                        "booking_unconfirmed",
                    ],
                    "conflicting_details": [],
                }
            )

    # Check for lodging in observations or task outputs
    for obs in raw_obs_list:
        art_ref = (
            getattr(obs, "artifact_ref", None)
            or (obs.get("artifact_ref") if isinstance(obs, dict) else {})
            or {}
        )
        cat = str(art_ref.get("category", "")).lower()
        if "lodging" in cat or "hotel" in cat:
            prop_name = str(
                art_ref.get("property_name") or art_ref.get("title") or ""
            ).strip()
            if not prop_name or prop_name.lower() in seen_hotels:
                continue
            seen_hotels.add(prop_name.lower())

            raw_url = (
                getattr(obs, "canonical_url", None)
                or getattr(obs, "source_url", None)
                or (
                    obs.get("canonical_url") or obs.get("source_url")
                    if isinstance(obs, dict)
                    else None
                )
            )
            src_url = str(raw_url).strip() if raw_url else ""
            has_usable_url = bool(
                src_url and (src_url.startswith("http://") or src_url.startswith("https://"))
            )

            r_time = getattr(obs, "retrieved_at", None) or (
                obs.get("retrieved_at") if isinstance(obs, dict) else None
            )
            r_str: str | None = None
            if isinstance(r_time, str):
                r_str = r_time
            elif r_time is not None and hasattr(r_time, "isoformat"):
                val = r_time.isoformat()
                if isinstance(val, str):
                    r_str = val

            obs_id = (
                str(getattr(obs, "id", None) or (obs.get("id") if isinstance(obs, dict) else ""))
                or None
            )

            p_val = art_ref.get("price_per_night") or art_ref.get("indicative_price_inr")
            c_val = art_ref.get("currency")
            int_price: int | None = None
            if p_val is not None:
                try:
                    int_price = int(p_val)
                except (ValueError, TypeError):
                    int_price = None

            if has_usable_url and int_price is not None and c_val:
                indicative_options.append(
                    {
                        "property_name": prop_name,
                        "price_per_night": int_price,
                        "currency": str(c_val),
                        "observation_id": obs_id,
                        "source_url": src_url,
                        "source_urls": [src_url],
                        "retrieval_time": r_str,
                        "evidence_class": EvidenceClass.INDICATIVE_SEARCH.value,
                        "is_indicative_search": True,
                        "is_verified_offer": False,
                        "booking_confirmed": False,
                        "availability_status": "INDICATIVE_SEARCH_ONLY",
                        "uncertainty": "INDICATIVE_SEARCH",
                        "unavailable_details": [
                            "live_room_availability_unverified",
                            "booking_unconfirmed",
                        ],
                        "conflicting_details": [],
                    }
                )
            else:
                indicative_options.append(
                    {
                        "property_name": prop_name,
                        "price_per_night": None,
                        "currency": None,
                        "observation_id": None,
                        "source_url": src_url if has_usable_url else None,
                        "source_urls": [src_url] if has_usable_url else [],
                        "retrieval_time": r_str,
                        "evidence_class": None,
                        "is_indicative_search": False,
                        "is_verified_offer": False,
                        "booking_confirmed": False,
                        "availability_status": "PRICE_UNAVAILABLE",
                        "uncertainty": "UNVERIFIED",
                        "unavailable_details": [
                            "price_unsupported_by_source",
                            "live_room_availability_unverified",
                            "booking_unconfirmed",
                        ],
                        "conflicting_details": [],
                    }
                )

    if not indicative_options:
        status = "UNAVAILABLE"
        reason = "No usable source-backed accommodation evidence retrieved"
    elif all(opt.get("price_per_night") is None for opt in indicative_options):
        status = "PARTIAL"
        reason = "Accommodation options identified without source-supported indicative rates"
    else:
        status = "COMPLETED"
        reason = None

    return {
        "status": status,
        "reason": reason,
        "indicative_options": indicative_options,
        "verified_offers": [],
        "availability_status": (
            "INDICATIVE_SEARCH_ONLY"
            if any(opt.get("is_indicative_search") for opt in indicative_options)
            else "PRICE_UNAVAILABLE"
        ),
        "pricing_notice": (
            "All rates are indicative search estimates from web aggregators. "
            "Verified date specific offers, live room availability, and booking confirmations "
            "are not included."
        ),
        "cheapest_claim_guarantee": (
            "No cheapest hotel claims are asserted; search rates represent indicative pricing "
            "without live supplier confirmation."
        ),
    }


def _extract_road_corridor_coordinates(state: TripPlanningState) -> list[Any]:
    """Extracts ordered physical road corridor coordinates from the selected route geometry.

    Conforms to spec 0007 AC-13, AC-14:
    Uses the real road polyline geometry from route optimization. Never falls back to
    straight lines between stops. If road geometry is unavailable, returns an empty list.
    """
    from functions.orchestrator.places.corridor import validate_coordinates
    from functions.orchestrator.places.schemas import CoordinatePoint

    raw_geom = state.get("route_geometry")
    if raw_geom is None:
        opt_sched = state.get("optimized_schedule")
        if isinstance(opt_sched, dict):
            raw_geom = opt_sched.get("route_geometry")

    points: list[CoordinatePoint] = []

    def _parse_pair(lon: Any, lat: Any) -> CoordinatePoint | None:
        try:
            f_lat = float(lat)
            f_lon = float(lon)
            is_valid, _ = validate_coordinates(f_lat, f_lon)
            if is_valid:
                return CoordinatePoint(lat=f_lat, lng=f_lon)
        except Exception:
            pass
        return None

    def _parse_coord_list(coords: Any) -> None:
        if not isinstance(coords, list):
            return
        for item in coords:
            if isinstance(item, CoordinatePoint):
                points.append(item)
            elif isinstance(item, dict):
                lat = item.get("lat") or item.get("latitude")
                lon = item.get("lng") or item.get("lon") or item.get("longitude")
                pt = _parse_pair(lon, lat)
                if pt:
                    points.append(pt)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                # GeoJSON coordinates are [lon, lat]
                pt = _parse_pair(item[0], item[1])
                if pt:
                    points.append(pt)

    if isinstance(raw_geom, dict):
        g_type = str(raw_geom.get("type") or "").lower()
        if g_type == "linestring":
            _parse_coord_list(raw_geom.get("coordinates"))
        elif g_type == "multilinestring":
            for line in raw_geom.get("coordinates") or []:
                _parse_coord_list(line)
        elif g_type == "feature":
            inner_geom = raw_geom.get("geometry")
            if isinstance(inner_geom, dict):
                g_type_inner = str(inner_geom.get("type") or "").lower()
                if g_type_inner == "linestring":
                    _parse_coord_list(inner_geom.get("coordinates"))
                elif g_type_inner == "multilinestring":
                    for line in inner_geom.get("coordinates") or []:
                        _parse_coord_list(line)
        elif g_type == "featurecollection":
            for feat in raw_geom.get("features") or []:
                if isinstance(feat, dict):
                    inner_geom = feat.get("geometry")
                    if isinstance(inner_geom, dict):
                        _parse_coord_list(inner_geom.get("coordinates"))
        elif "coordinates" in raw_geom:
            _parse_coord_list(raw_geom.get("coordinates"))
    elif isinstance(raw_geom, list):
        _parse_coord_list(raw_geom)

    if not points:
        opt_sched = state.get("optimized_schedule")
        if isinstance(opt_sched, dict):
            legs = opt_sched.get("legs", [])
            if isinstance(legs, list):
                for leg in legs:
                    if isinstance(leg, dict) and "geometry" in leg:
                        leg_geom = leg["geometry"]
                        if isinstance(leg_geom, dict):
                            _parse_coord_list(leg_geom.get("coordinates"))
                        elif isinstance(leg_geom, list):
                            _parse_coord_list(leg_geom)

    return points


def _build_advisory_research(state: TripPlanningState) -> dict[str, Any]:
    """Assembles structured road hazard and travel advisory research.

    Conforms to spec 0007 AC-13, AC-14, AC-15:
    1. Extracts travel advisories, road closures, and weather warnings from observations and claims.
    2. Preserves source URL, observation ID, retrieval time, affected area, applicable time, and uncertainty.
    3. Distinguishes confirmed authoritative warnings from unverified search snippets.
    4. Evaluates geographic relevance against selected road route corridor geometry.
    5. Evaluates temporal relevance against trip travel dates; classifies ended alerts as expired.
    6. Preserves conflicting reports side by side without guessing.
    7. Fail closed safety: absence of warnings NEVER evaluates to SAFE; reports ADVISORY_UNKNOWN
       with an explicit reason whenever evidence is missing, stale, outside area, or unverified.
    """
    from functions.orchestrator.evidence.policy import (
        is_verified_authority_domain,
        safe_parse_datetime,
    )
    from functions.orchestrator.places.corridor import (
        compute_corridor_distance_km,
        validate_coordinates,
    )
    from functions.orchestrator.places.schemas import CoordinatePoint

    brief_data = state.get("brief", {})
    raw_origin = (
        brief_data.get("origin_name")
        if isinstance(brief_data, dict)
        else getattr(brief_data, "origin_name", None)
    )
    origin_name = str(raw_origin or "").strip().lower()
    raw_dests = (
        brief_data.get("destinations", [])
        if isinstance(brief_data, dict)
        else getattr(brief_data, "destinations", [])
    )
    dest_names = [
        str(
            (d.get("name") if isinstance(d, dict) else getattr(d, "name", None)) or ""
        ).strip().lower()
        for d in (raw_dests if isinstance(raw_dests, list) else [])
    ]

    opt_sched = state.get("optimized_schedule", {})
    sched_stops = (
        opt_sched.get("stops", [])
        if isinstance(opt_sched, dict)
        else getattr(opt_sched, "stops", [])
    )
    stop_names = [
        str(
            (s.get("name") if isinstance(s, dict) else getattr(s, "name", None)) or ""
        ).strip().lower()
        for s in (sched_stops if isinstance(sched_stops, list) else [])
    ]

    # Extract physical road corridor coordinates from the selected route geometry
    road_corridor_coords = _extract_road_corridor_coordinates(state)

    # Trip travel dates
    start_date_val = (
        brief_data.get("start_date")
        if isinstance(brief_data, dict)
        else getattr(brief_data, "start_date", None)
    )
    end_date_val = (
        brief_data.get("end_date")
        if isinstance(brief_data, dict)
        else getattr(brief_data, "end_date", None)
    )
    parsed_trip_start = safe_parse_datetime(start_date_val)
    parsed_trip_end = safe_parse_datetime(end_date_val)
    trip_start_date: date | None = parsed_trip_start.date() if parsed_trip_start else None
    trip_end_date: date | None = parsed_trip_end.date() if parsed_trip_end else None

    if trip_start_date and not trip_end_date:
        stay_days = sum(
            int(d.get("stay_days", 1) if isinstance(d, dict) else getattr(d, "stay_days", 1))
            for d in (raw_dests if isinstance(raw_dests, list) else [])
        )
        trip_end_date = trip_start_date + timedelta(days=max(stay_days, 1))

    now_utc = datetime.now(UTC)
    now_date = now_utc.date()

    # Relevant trip location keywords
    verified_trip_places: set[str] = set()
    if origin_name and origin_name != "none":
        verified_trip_places.add(origin_name)
    for dn in dest_names:
        if dn and dn != "none":
            verified_trip_places.add(dn)
    for sn in stop_names:
        if sn and sn != "none":
            verified_trip_places.add(sn)

    # Collect all observations
    raw_obs_list: list[Any] = list(state.get("observations") or [])
    task_outputs = state.get("task_outputs", {})
    if isinstance(task_outputs, dict):
        if "observations" in task_outputs and isinstance(
            task_outputs["observations"], list
        ):
            raw_obs_list.extend(task_outputs["observations"])
        for sub in task_outputs.values():
            if (
                isinstance(sub, dict)
                and "observations" in sub
                and isinstance(sub["observations"], list)
            ):
                raw_obs_list.extend(sub["observations"])

    relevant_advisories: list[dict[str, Any]] = []
    unrelated_advisories: list[dict[str, Any]] = []
    expired_advisories: list[dict[str, Any]] = []
    unverified_advisories: list[dict[str, Any]] = []
    conflicting_advisories: list[dict[str, Any]] = []
    checked_sources: set[str] = set()
    auth_sources: set[str] = set()

    for obs in raw_obs_list:
        art = (
            getattr(obs, "artifact_ref", None)
            or (obs.get("artifact_ref") if isinstance(obs, dict) else {})
            or {}
        )
        if not isinstance(art, dict):
            art = {}
        cat = str(art.get("category") or "").lower()
        excerpt = str(
            getattr(obs, "excerpt_text", None)
            or (obs.get("excerpt_text") if isinstance(obs, dict) else "")
            or ""
        )
        title = str(
            art.get("headline")
            or art.get("title")
            or (obs.get("title") if isinstance(obs, dict) else "")
            or ""
        ).strip()

        is_adv = any(
            k in cat
            for k in (
                "advisory",
                "hazard",
                "road_closure",
                "closure",
                "restriction",
                "weather_warning",
                "disaster",
                "weather_alert",
            )
        )
        combined_text = (cat + " " + title + " " + excerpt).lower()
        if not is_adv and any(
            k in combined_text
            for k in (
                "road closure",
                "road closed",
                "highway closed",
                "traffic diversion",
                "cyclone",
                "landslide",
                "flood",
                "flooding",
                "waterlog",
                "heavy rain alert",
                "storm warning",
                "travel advisory",
                "travel restriction",
            )
        ):
            is_adv = True

        if not is_adv:
            continue

        raw_url = (
            getattr(obs, "canonical_url", None)
            or getattr(obs, "source_url", None)
            or (
                obs.get("canonical_url") or obs.get("source_url")
                if isinstance(obs, dict)
                else ""
            )
        )
        obs_url = str(raw_url).strip()
        obs_id = str(
            getattr(obs, "id", None)
            or (obs.get("id") if isinstance(obs, dict) else "")
            or ""
        )
        ret_time = (
            getattr(obs, "retrieved_at", None)
            or (obs.get("retrieved_at") if isinstance(obs, dict) else None)
        )
        ret_str: str | None = None
        if isinstance(ret_time, str):
            ret_str = ret_time
        elif ret_time is not None and hasattr(ret_time, "isoformat"):
            ret_str = ret_time.isoformat()

        pub_name = str(
            getattr(obs, "publisher", None)
            or (obs.get("publisher") if isinstance(obs, dict) else "")
            or ""
        )

        has_usable_url = bool(
            obs_url and (obs_url.startswith("http://") or obs_url.startswith("https://"))
        )
        if has_usable_url:
            checked_sources.add(obs_url)

        # 1. Authority validation against explicit policy (no substring matching, no untrusted flags)
        is_auth = is_verified_authority_domain(obs_url)
        ext_method = str(
            getattr(obs, "extraction_method", None)
            or (obs.get("extraction_method") if isinstance(obs, dict) else "")
            or ""
        ).upper()
        is_search_snippet = ext_method in (
            "SERPAPI_SEARCH",
            "SEARCH_SNIPPET",
            "SEARCH_RESULT",
        )
        if is_auth:
            auth_sources.add(obs_url or pub_name)

        headline = title
        if not headline and excerpt:
            first_sent = excerpt.split(".")[0].strip()
            headline = first_sent[:120] if first_sent else "Travel Advisory"

        affected_area = str(
            art.get("affected_area")
            or art.get("location")
            or art.get("route")
            or ""
        ).strip()

        adv_type = str(art.get("advisory_type") or "").upper()
        if not adv_type:
            if "cyclone" in combined_text:
                adv_type = "CYCLONE"
            elif "flood" in combined_text or "waterlog" in combined_text:
                adv_type = "FLOOD"
            elif "landslide" in combined_text:
                adv_type = "LANDSLIDE"
            elif any(w in combined_text for w in ("closure", "closed", "blocked", "diversion")):
                adv_type = "ROAD_CLOSURE"
            elif "restriction" in combined_text or "curfew" in combined_text:
                adv_type = "TRAVEL_RESTRICTION"
            elif "rain" in combined_text or "storm" in combined_text:
                adv_type = "WEATHER_WARNING"
            else:
                adv_type = "GENERAL_ADVISORY"

        severity = str(art.get("severity") or "WARNING").upper()

        # 2. Time and freshness validation
        start_time_raw = art.get("applicable_start")
        end_time_raw = art.get("applicable_end")
        parsed_start = safe_parse_datetime(start_time_raw)
        parsed_end = safe_parse_datetime(end_time_raw)
        is_malformed = (start_time_raw is not None and parsed_start is None) or (
            end_time_raw is not None and parsed_end is None
        )
        is_undated = parsed_start is None and parsed_end is None

        ret_dt = ret_time if isinstance(ret_time, datetime) else safe_parse_datetime(ret_time)
        pub_dt = (
            getattr(obs, "published_at", None)
            or (obs.get("published_at") if isinstance(obs, dict) else None)
        )
        if isinstance(pub_dt, str):
            pub_dt = safe_parse_datetime(pub_dt)
        obs_dt = pub_dt or ret_dt

        is_stale = False
        if obs_dt is not None:
            dt_with_tz = obs_dt if obs_dt.tzinfo else obs_dt.replace(tzinfo=UTC)
            age_hours = (datetime.now(UTC) - dt_with_tz).total_seconds() / 3600.0
            if age_hours > 24.0 and parsed_end is None:
                is_stale = True

        effective_start = trip_start_date if trip_start_date is not None else now_date
        is_expired = False
        if parsed_end is not None:
            if parsed_end.date() < effective_start:
                is_expired = True

        is_future = False
        if parsed_start is not None and trip_end_date is not None:
            if parsed_start.date() > trip_end_date:
                is_future = True

        interval_uncertain = False
        if trip_start_date is None and trip_end_date is None and not is_expired and not is_undated:
            interval_uncertain = True

        is_lifted = any(
            w in combined_text
            for w in ("reopened", "lifted", "cleared", "normal traffic", "road opened")
        )

        # 3. Route and spatial relevance validation
        adv_lat = (
            art.get("latitude")
            or art.get("lat")
            or (art.get("point", {}).get("lat") if isinstance(art.get("point"), dict) else None)
        )
        adv_lng = (
            art.get("longitude")
            or art.get("lng")
            or (art.get("point", {}).get("lng") if isinstance(art.get("point"), dict) else None)
        )

        area_text = (affected_area + " " + headline + " " + excerpt).lower()
        matched_trip_place = (
            any(place in area_text for place in verified_trip_places if len(place) >= 3)
            if verified_trip_places
            else False
        )

        spatial_relevance: str
        relevance_reason: str | None = None
        if road_corridor_coords:
            if adv_lat is not None and adv_lng is not None:
                try:
                    is_valid_pt, _ = validate_coordinates(float(adv_lat), float(adv_lng))
                    if is_valid_pt:
                        adv_pt = CoordinatePoint(lat=float(adv_lat), lng=float(adv_lng))
                        dist_km = compute_corridor_distance_km(adv_pt, road_corridor_coords)
                        if dist_km <= 100.0:
                            spatial_relevance = "WITHIN_CORRIDOR"
                            relevance_reason = (
                                f"Notice coordinates are {dist_km:.1f} km from selected road route (within 100 km corridor)"
                            )
                        else:
                            spatial_relevance = "OUTSIDE_CORRIDOR"
                            relevance_reason = (
                                f"Notice coordinates are {dist_km:.1f} km from selected road route (exceeds 100 km corridor)"
                            )
                    else:
                        spatial_relevance = "UNKNOWN"
                        relevance_reason = "Affected area coordinates are invalid or out of bounds"
                except Exception:
                    spatial_relevance = "UNKNOWN"
                    relevance_reason = "Error validating affected area coordinates"
            else:
                # Road route geometry exists but notice has only an affected area name without coordinates
                spatial_relevance = "UNKNOWN"
                if matched_trip_place:
                    relevance_reason = (
                        "Affected area mentions trip place but lacks coordinates to tie to selected road route geometry"
                    )
                elif affected_area:
                    relevance_reason = (
                        f"Affected area '{affected_area}' lacks coordinates; intermediate corridor location cannot be ruled out"
                    )
                else:
                    relevance_reason = (
                        "Affected area has no coordinates to evaluate against selected road route geometry"
                    )
        else:
            # Selected road route geometry is unavailable
            spatial_relevance = "UNKNOWN"
            if matched_trip_place:
                relevance_reason = (
                    "Selected road route geometry is unavailable; "
                    "notice mentions trip place but spatial corridor cannot be confirmed without road geometry"
                )
            elif affected_area:
                relevance_reason = (
                    f"Selected road route geometry is unavailable and affected area '{affected_area}' lacks coordinates; "
                    "spatial relevance cannot be verified"
                )
            else:
                relevance_reason = (
                    "Selected road route geometry is unavailable; spatial corridor relevance cannot be verified"
                )

        # Determine status and uncertainty
        if is_lifted:
            status_val = "LIFTED"
            uncertainty_val = "LIFTED"
        elif is_expired:
            status_val = "EXPIRED"
            uncertainty_val = "EXPIRED"
        elif is_future:
            status_val = "FUTURE"
            uncertainty_val = "FUTURE_ADVISORY"
        elif is_malformed:
            status_val = "MALFORMED_DATES"
            uncertainty_val = "MALFORMED_DATES"
        elif is_undated:
            status_val = "UNDATED"
            uncertainty_val = "UNVERIFIED_UNDATED"
        elif is_stale:
            status_val = "STALE"
            uncertainty_val = "STALE"
        elif interval_uncertain:
            status_val = "UNKNOWN_INTERVAL"
            uncertainty_val = "UNKNOWN_INTERVAL"
        elif is_auth and not is_search_snippet:
            status_val = "ACTIVE"
            uncertainty_val = "CONFIRMED_AUTHORITATIVE"
        else:
            status_val = "ACTIVE"
            uncertainty_val = "UNVERIFIED_SEARCH"

        adv_item = {
            "headline": headline,
            "advisory_type": adv_type,
            "severity": severity,
            "affected_area": affected_area,
            "applicable_start": str(start_time_raw) if start_time_raw else None,
            "applicable_end": str(end_time_raw) if end_time_raw else None,
            "is_authoritative": is_auth,
            "authority_name": pub_name if is_auth else None,
            "status": status_val,
            "source_url": obs_url if has_usable_url else None,
            "observation_id": obs_id or None,
            "retrieval_time": ret_str,
            "uncertainty": uncertainty_val,
            "relevance": spatial_relevance,
            "relevance_reason": relevance_reason,
            "reason": relevance_reason,
        }

        if is_expired or is_lifted:
            expired_advisories.append(adv_item)
        elif spatial_relevance == "OUTSIDE_CORRIDOR":
            adv_item["relevance"] = "OUTSIDE_CORRIDOR"
            unrelated_advisories.append(adv_item)
        elif spatial_relevance in ("UNKNOWN", "LOCATION_EVIDENCE_INSUFFICIENT"):
            adv_item["relevance"] = "UNKNOWN"
            if adv_item["uncertainty"] not in (
                "FUTURE_ADVISORY",
                "UNVERIFIED_UNDATED",
                "STALE",
                "UNVERIFIED_SEARCH",
            ):
                adv_item["uncertainty"] = "UNKNOWN_RELEVANCE"
            unverified_advisories.append(adv_item)
        elif status_val in ("FUTURE", "UNDATED", "STALE", "MALFORMED_DATES", "UNKNOWN_INTERVAL"):
            unverified_advisories.append(adv_item)
        elif uncertainty_val == "UNVERIFIED_SEARCH":
            unverified_advisories.append(adv_item)
        else:
            relevant_advisories.append(adv_item)

    # Detect conflicts between active closures and reopenings for the same route or area
    active_closures = [
        a
        for a in relevant_advisories
        if a["advisory_type"] == "ROAD_CLOSURE" and a["status"] == "ACTIVE"
    ]
    lifted_for_same_area = [
        e
        for e in expired_advisories
        if e["status"] == "LIFTED"
        and any(
            (c["affected_area"] and c["affected_area"].lower() in e["affected_area"].lower())
            or (e["affected_area"] and e["affected_area"].lower() in c["affected_area"].lower())
            or (c["headline"] and c["headline"].lower() in e["headline"].lower())
            for c in active_closures
        )
    ]
    if active_closures and lifted_for_same_area:
        for c in active_closures:
            c["uncertainty"] = "CONFLICTED"
            c["conflict_reason"] = (
                "Conflicting reports: road closure reported alongside reopening report"
            )
            if c not in conflicting_advisories:
                conflicting_advisories.append(c)
        for l_item in lifted_for_same_area:
            l_item["uncertainty"] = "CONFLICTED"
            l_item["conflict_reason"] = (
                "Conflicting reports: road reopening reported alongside closure report"
            )
            if l_item not in conflicting_advisories:
                conflicting_advisories.append(l_item)

    # Also detect contradictory reports within relevant advisories
    seen_opposing: dict[str, str] = {}
    for a in relevant_advisories:
        k = (a["affected_area"] or a["headline"]).lower()
        if k in seen_opposing and seen_opposing[k] != a["status"]:
            a["uncertainty"] = "CONFLICTED"
            if a not in conflicting_advisories:
                conflicting_advisories.append(a)
        else:
            seen_opposing[k] = a["status"]

    # Determine status and fail closed reason
    if conflicting_advisories:
        status = "ADVISORIES_PRESENT"
        reason = "Conflicting travel advisory reports detected for route corridor"
    elif relevant_advisories:
        status = "ADVISORIES_PRESENT"
        active_count = len([a for a in relevant_advisories if a["status"] == "ACTIVE"])
        reason = f"{active_count} active travel warning(s) detected for trip corridor"
    elif expired_advisories:
        status = "ADVISORY_UNKNOWN"
        reason = "Advisory evidence is expired or lifted; real time road conditions unmonitored"
    elif unverified_advisories:
        status = "ADVISORY_UNKNOWN"
        reason = (
            "Advisory reports are unverified, undated, stale, or have insufficient location evidence; "
            "route conditions remain unverified"
        )
    elif unrelated_advisories:
        status = "ADVISORY_UNKNOWN"
        reason = (
            "No verified advisories for route corridor during travel dates; "
            "unmonitored road segments remain unverified"
        )
    else:
        status = "ADVISORY_UNKNOWN"
        reason = "No road hazard or travel advisory evidence retrieved for route; route safety unverified"

    safety_notice = (
        "Absence of travel advisories or warnings is not proof of safety; "
        "unmonitored conditions remain unverified."
    )

    return {
        "status": status,
        "reason": reason,
        "safety_notice": safety_notice,
        "advisories": relevant_advisories,
        "unrelated_advisories": unrelated_advisories,
        "expired_advisories": expired_advisories,
        "unverified_advisories": unverified_advisories,
        "conflicting_advisories": conflicting_advisories,
        "checked_sources_count": len(checked_sources),
        "authoritative_sources_count": len(auth_sources),
    }


def synthesize_proposal_node(state: TripPlanningState) -> dict[str, Any]:
    """Assembles the final validated travel proposal."""
    food_research = _build_food_research(state)
    accommodation_research = _build_accommodation_research(state)
    advisory_research = _build_advisory_research(state)

    # Route diagnostics: append advisory notices if active warnings exist, without mutating route
    route_diagnostics = list(state.get("route_diagnostics", []))
    for adv in advisory_research.get("advisories", []):
        if adv.get("severity") in ("CRITICAL", "WARNING"):
            notice = (
                f"Advisory Notice: {adv.get('headline')} "
                f"({adv.get('affected_area') or 'Route Corridor'}) - {adv.get('uncertainty')}"
            )
            if notice not in route_diagnostics:
                route_diagnostics.append(notice)

    proposal = {
        "trip_id": state.get("trip_id"),
        "version": state.get("version", 1),
        "schedule": state.get("optimized_schedule"),
        "budget_summary": state.get("budget_summary"),
        "budget_lines": state.get("budget_lines"),
        "editorial_recommendations": state.get("editorial_recommendations", []),
        "excluded_candidates": state.get("excluded_candidates", []),
        "weather_advisories": state.get("weather_advisories", []),
        "weather_status": state.get("weather_status")
        or WeatherAdvisoryStatus.ADVISORY_UNKNOWN.value,
        "advisory_status": advisory_research.get(
            "status", WeatherAdvisoryStatus.ADVISORY_UNKNOWN.value
        ),
        "validated_candidates": state.get("validated_candidates", []),
        "discovered_places": state.get("discovered_places", []),
        "food_research": food_research,
        "accommodation_research": accommodation_research,
        "advisory_research": advisory_research,
        "advisories": advisory_research.get("advisories", []),
        "route_id": state.get("route_id"),
        "route_status": state.get("route_status"),
        "routing_status": state.get("routing_status"),
        "route_geometry": state.get("route_geometry"),
        "route_diagnostics": route_diagnostics,
        "provider_diagnostics": state.get("provider_diagnostics", {}),
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "ready_for_approval",
    }

    return {
        "current_step": "proposal_synthesized",
        "proposal": proposal,
        "food_research": food_research,
        "accommodation_research": accommodation_research,
        "advisory_research": advisory_research,
        "advisory_status": advisory_research.get(
            "status", WeatherAdvisoryStatus.ADVISORY_UNKNOWN.value
        ),
        "advisories": advisory_research.get("advisories", []),
        "route_geometry": state.get("route_geometry"),
    }


async def publish_proposal_node(
    state: TripPlanningState,
    config: RunnableConfig = _DEFAULT_RUNNABLE_CONFIG,
) -> dict[str, Any]:
    """Atomically publishes the synthesized travel proposal with pessimistic locking and version checking.

    Conforms to spec 0007 Slice 5 and AC-8:
    1. Checks cancellation barrier and execution deadline.
    2. Enforces tenant isolation between caller, execution owner, and trip owner.
    3. Verifies locked execution exists, belongs to caller and trip, and is eligible to publish.
    4. Fences publication using active runner lease. Stale or expired runner cannot write trip, delta, or events.
    5. Acquires pessimistic row lock on trips (SELECT ... FOR UPDATE).
    6. Validates base version match; detects concurrent brief updates, records
       PROPOSAL_SUPERSEDED event, and marks execution SUPERSEDED without overwriting trip data.
    7. On version match, records TripDeltaModel and atomically updates trips.current_route_id,
       trips.itinerary_json, and trips.state ('proposed').
    8. Replay safe and idempotent: duplicate invocations update existing proposal without
       creating duplicate deltas.
    """
    if state.get("status") == "CANCELLED":
        logger.info("Trip planner workflow cancelled; skipping proposal publication")
        return {
            "current_step": "proposal_publication_cancelled",
            "status": "CANCELLED",
            "proposal_status": "CANCELLED",
        }

    configurable = config.get("configurable", {}) if config else {}
    session_factory = configurable.get("session_factory") or state.get("_session_factory")
    is_offline_fixture = bool(state.get("_offline_fixture_mode", False))

    if not is_offline_fixture and session_factory is None and "session_factory" not in configurable:
        try:
            from config.db import get_session_factory

            session_factory = get_session_factory()
        except Exception:
            session_factory = None

    if is_offline_fixture:
        return {
            "current_step": "proposal_published",
            "status": "completed",
            "proposal_status": "PUBLISHED",
        }

    # Production mode: fail closed if session_factory, trip_id, owner_id, execution identity, or runner_lease is missing
    trip_id_raw = state.get("trip_id")
    owner_id_raw = state.get("owner_id")
    execution_id_raw = state.get("execution_id")
    runner_lease_raw = configurable.get("runner_lease") or state.get("runner_lease")

    if session_factory is None or not trip_id_raw or not owner_id_raw or not execution_id_raw:
        logger.error(
            "Production proposal publication failed closed: missing required persistence identity "
            "(session_factory=%s, trip_id=%s, owner_id=%s, execution_id=%s)",
            session_factory is not None,
            bool(trip_id_raw),
            bool(owner_id_raw),
            bool(execution_id_raw),
        )
        return {
            "current_step": "proposal_publication_failed",
            "status": "FAILED",
            "proposal_status": "FAILED",
            "errors": [
                "Production proposal publication requires session_factory, trip_id, owner_id, and execution_id"
            ],
        }

    if not runner_lease_raw:
        logger.error(
            "Production proposal publication failed closed: missing runner lease for execution %s",
            execution_id_raw,
        )
        return {
            "current_step": "proposal_publication_lost_lease",
            "status": "FAILED",
            "proposal_status": "FAILED",
            "errors": ["Production proposal publication requires a valid runner lease"],
        }

    try:
        trip_uuid = uuid.UUID(str(trip_id_raw))
        owner_uuid = uuid.UUID(str(owner_id_raw))
        execution_uuid = uuid.UUID(str(execution_id_raw))
        runner_lease_uuid = uuid.UUID(str(runner_lease_raw))
    except (ValueError, TypeError) as exc:
        logger.error("Invalid UUID identity in proposal publication: %s", exc)
        return {
            "current_step": "proposal_publication_failed",
            "status": "FAILED",
            "proposal_status": "FAILED",
            "errors": [f"Invalid UUID identity in proposal publication: {exc}"],
        }

    async with session_factory() as session:
        async with session.begin():
            # 1. Lock and verify execution under transaction
            from functions.api.models import TripDeltaModel, TripModel
            from functions.orchestrator.execution.models import ExecutionModel

            exec_stmt = (
                select(ExecutionModel).where(ExecutionModel.id == execution_uuid).with_for_update()
            )
            exec_row = (await session.execute(exec_stmt)).scalar_one_or_none()

            if exec_row is None:
                logger.error("Execution %s not found for proposal publication", execution_uuid)
                return {
                    "current_step": "proposal_publication_failed",
                    "status": "FAILED",
                    "proposal_status": "FAILED",
                    "errors": [f"Execution {execution_uuid} not found"],
                }

            # 2. Lock and verify trip under transaction
            trip_stmt = select(TripModel).where(TripModel.id == trip_uuid).with_for_update()
            trip = (await session.execute(trip_stmt)).scalar_one_or_none()

            if trip is None:
                logger.error("Trip %s not found for proposal publication", trip_uuid)
                return {
                    "current_step": "proposal_publication_failed",
                    "status": "FAILED",
                    "errors": [f"Trip {trip_uuid} not found"],
                    "proposal_status": "FAILED",
                }

            # 3. Post-lock evaluation: evaluate lease expiry and execution deadline AFTER acquiring all locks
            now = datetime.now(UTC)

            # Tenant isolation on execution and trip
            if exec_row.owner_id != owner_uuid:
                logger.error(
                    "Tenant isolation violation: execution %s belongs to owner %s, caller is %s",
                    execution_uuid,
                    exec_row.owner_id,
                    owner_uuid,
                )
                raise PermissionError(
                    f"Tenant isolation mismatch: execution owner {exec_row.owner_id} does not match {owner_uuid}"
                )

            if trip.owner_id != owner_uuid:
                logger.error(
                    "Tenant isolation violation: trip owner %s does not match execution owner %s",
                    trip.owner_id,
                    owner_uuid,
                )
                raise PermissionError(
                    f"Tenant isolation mismatch: trip owner {trip.owner_id} does not match {owner_uuid}"
                )

            # Execution trip binding verification
            if exec_row.trip_id is None or exec_row.trip_id != trip_uuid:
                logger.error(
                    "Cross-trip execution mismatch: execution %s belongs to trip %s, target trip is %s",
                    execution_uuid,
                    exec_row.trip_id,
                    trip_uuid,
                )
                raise ValueError(
                    f"Cross-trip execution mismatch: execution trip {exec_row.trip_id} does not match {trip_uuid}"
                )

            # Runner lease fencing (evaluated before any execution state mutation)
            if exec_row.lease_token is None:
                logger.warning(
                    "Publication lease check failed for execution %s: execution has no lease token in database",
                    execution_uuid,
                )
                return {
                    "current_step": "proposal_publication_lost_lease",
                    "status": "FAILED",
                    "proposal_status": "FAILED",
                    "errors": ["Execution has no active runner lease token"],
                }

            if exec_row.lease_token != runner_lease_uuid:
                logger.warning(
                    "Publication lease check failed for execution %s: current lease %s != runner lease %s",
                    execution_uuid,
                    exec_row.lease_token,
                    runner_lease_uuid,
                )
                return {
                    "current_step": "proposal_publication_lost_lease",
                    "status": "FAILED",
                    "proposal_status": "FAILED",
                    "errors": ["Runner lease expired or held by another runner"],
                }

            if exec_row.lease_expiry is None:
                logger.warning(
                    "Publication lease check failed for execution %s: execution has no lease expiry in database",
                    execution_uuid,
                )
                return {
                    "current_step": "proposal_publication_lost_lease",
                    "status": "FAILED",
                    "proposal_status": "FAILED",
                    "errors": ["Execution has no runner lease expiry timestamp"],
                }

            if now >= exec_row.lease_expiry:
                logger.warning(
                    "Publication lease expired for execution %s: expiry %s <= now %s (evaluated after lock wait)",
                    execution_uuid,
                    exec_row.lease_expiry,
                    now,
                )
                return {
                    "current_step": "proposal_publication_lost_lease",
                    "status": "FAILED",
                    "proposal_status": "FAILED",
                    "errors": ["Runner lease expired before or during lock acquisition"],
                }

            # Execution eligibility: cancellation barriers
            if exec_row.status == "CANCELLED" or exec_row.cancelled_at is not None:
                logger.info(
                    "Parent execution %s is cancelled (status=%s, cancelled_at=%s); skipping publication",
                    execution_uuid,
                    exec_row.status,
                    exec_row.cancelled_at,
                )
                return {
                    "current_step": "proposal_publication_cancelled",
                    "status": "CANCELLED",
                    "proposal_status": "CANCELLED",
                }

            # Execution deadline barrier (only valid runner holding unexpired lease may cancel execution on deadline)
            if exec_row.deadline_at and now >= exec_row.deadline_at:
                logger.warning(
                    "Parent execution %s deadline expired at %s (now=%s); marking CANCELLED",
                    execution_uuid,
                    exec_row.deadline_at,
                    now,
                )
                exec_row.status = "CANCELLED"
                exec_row.updated_at = now
                return {
                    "current_step": "proposal_publication_cancelled",
                    "status": "CANCELLED",
                    "proposal_status": "CANCELLED",
                }

            # Execution already superseded or terminal
            if exec_row.status == "SUPERSEDED":
                logger.info(
                    "Parent execution %s is SUPERSEDED; skipping publication", execution_uuid
                )
                return {
                    "current_step": "proposal_superseded",
                    "status": "SUPERSEDED",
                    "proposal_status": "SUPERSEDED",
                }

            if exec_row.status in ("FAILED", "SUCCEEDED", "PARTIAL"):
                logger.info(
                    "Parent execution %s is already terminal (status=%s); skipping publication",
                    execution_uuid,
                    exec_row.status,
                )
                return {
                    "current_step": "proposal_publication_ineligible",
                    "status": exec_row.status,
                    "proposal_status": exec_row.status,
                }

            # Optimistic concurrency / version validation
            expected_base_version = int(state.get("version", 1))
            if trip.current_version != expected_base_version:
                logger.warning(
                    "Proposal superseded for trip %s: current version %s != expected %s",
                    trip_uuid,
                    trip.current_version,
                    expected_base_version,
                )
                from functions.orchestrator.execution.job_events import record_job_event

                await record_job_event(
                    session,
                    execution_id=execution_uuid,
                    event_type="PROPOSAL_SUPERSEDED",
                    safe_payload={
                        "trip_id": str(trip_uuid),
                        "expected_base_version": expected_base_version,
                        "current_trip_version": trip.current_version,
                        "reason": "Trip version advanced concurrently during planning",
                    },
                )
                exec_row.status = "SUPERSEDED"
                exec_row.updated_at = now

                return {
                    "current_step": "proposal_superseded",
                    "status": "SUPERSEDED",
                    "proposal_status": "SUPERSEDED",
                }

            # 4. Version matches: publish proposal atomically
            proposal = state.get("proposal") or {}
            route_uuid = uuid.UUID(str(state["route_id"])) if state.get("route_id") else None

            # Replay safety: check if delta already exists for (trip_id, base_version)
            delta_stmt = select(TripDeltaModel).where(
                TripDeltaModel.trip_id == trip_uuid,
                TripDeltaModel.base_version == expected_base_version,
            )
            existing_delta = (await session.execute(delta_stmt)).scalar_one_or_none()

            evidence_refs: list[str] = []
            for c in state.get("validated_candidates", []):
                if c.get("place_id"):
                    evidence_refs.append(f"place:{c['place_id']}")

            actor_id = owner_uuid

            if existing_delta is None:
                delta = TripDeltaModel(
                    id=uuid.uuid4(),
                    trip_id=trip_uuid,
                    base_version=expected_base_version,
                    proposal_json=proposal,
                    evidence_refs=evidence_refs,
                    actor_id=actor_id,
                    created_at=now,
                )
                session.add(delta)
            else:
                existing_delta.proposal_json = proposal
                existing_delta.evidence_refs = evidence_refs

            trip.current_route_id = route_uuid
            trip.itinerary_json = proposal
            trip.state = "proposed"
            trip.updated_at = now

            # Publication event idempotency check: do not record duplicate publication events on retry or replay
            from functions.orchestrator.execution.models import JobEventModel

            pub_event_stmt = select(JobEventModel.id).where(
                JobEventModel.execution_id == execution_uuid,
                JobEventModel.event_type == "PROPOSAL_PUBLISHED",
            )
            has_pub_event = (await session.execute(pub_event_stmt)).first() is not None

            if not has_pub_event:
                from functions.orchestrator.execution.job_events import record_job_event

                await record_job_event(
                    session,
                    execution_id=execution_uuid,
                    event_type="PROPOSAL_PUBLISHED",
                    safe_payload={
                        "trip_id": str(trip_uuid),
                        "base_version": expected_base_version,
                        "route_id": str(route_uuid) if route_uuid else None,
                        "routing_status": state.get("routing_status"),
                    },
                )

            checkpointer = configurable.get("checkpointer")
            if checkpointer and hasattr(checkpointer, "flush"):
                await checkpointer.flush(session, str(execution_uuid))

    return {
        "current_step": "proposal_published",
        "status": "completed",
        "proposal_status": "PUBLISHED",
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
    elif "brief" in state or "trip_id" in state:
        # For 0007 trip planner workflow, default to maximum 2 browser URLs when not explicitly overridden
        max_slots = 2
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
    env_max = os.environ.get("BROWSER_MAX_ESCALATION_TASKS")
    if env_max and env_max.isdigit():
        max_slots = int(env_max)
    elif "brief" in state or "trip_id" in state:
        # For 0007 trip planner workflow, default to maximum 2 browser URLs when not explicitly overridden
        max_slots = 2
    else:
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
            raw_uncovered = state.get("uncovered_destinations")
            uncovered_in_state = (
                set(raw_uncovered)
                if isinstance(raw_uncovered, (list, tuple, set))
                else set()
            )
            expected_destinations = [
                d for d in destinations_info if d["name"] not in uncovered_in_state
            ]
            if expected_destinations:
                validated_evidence_satisfied = all(
                    d["name"] in matched_destinations for d in expected_destinations
                )
            else:
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
            raw_uncovered = state.get("uncovered_destinations")
            uncovered_in_state = (
                set(raw_uncovered)
                if isinstance(raw_uncovered, (list, tuple, set))
                else set()
            )
            expected_destinations = [
                d for d in destinations_info if d["name"] not in uncovered_in_state
            ]
            if expected_destinations:
                validated_evidence_satisfied = (
                    all(d["name"] in offline_matched for d in expected_destinations)
                    and valid_persisted_count > 0
                )
            else:
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
        existing_planned = state.get("planned_search_queries")
        planned_queries: Any
        if isinstance(existing_planned, list) and existing_planned:
            planned_queries = existing_planned
        else:
            llm_for_queries = state.get("_llm_client") or configurable.get("llm_client")
            planned_queries = await QueryPlanningService.plan_queries_with_llm(
                brief, llm_client=llm_for_queries, max_queries=DEFAULT_MAX_QUERIES
            )

        budget_dict = QueryPlanningService.calculate_collector_budget(planned_queries)
        planned_covered = getattr(planned_queries, "covered_destinations", None)
        raw_state_covered = state.get("covered_destinations")
        covered_dests = (
            list(planned_covered)
            if planned_covered is not None
            else (
                list(raw_state_covered)
                if isinstance(raw_state_covered, (list, tuple, set))
                else [d["name"] for d in destinations_info]
            )
        )
        planned_uncovered = getattr(planned_queries, "uncovered_destinations", None)
        raw_state_uncovered = state.get("uncovered_destinations")
        uncovered_dests = (
            list(planned_uncovered)
            if planned_uncovered is not None
            else (
                list(raw_state_uncovered)
                if isinstance(raw_state_uncovered, (list, tuple, set))
                else []
            )
        )
        cov_reason = (
            getattr(planned_queries, "coverage_reason", None)
            or state.get("coverage_reason")
        )

        search_task = {
            "worker": "EVIDENCE",
            "action": "collect",
            "payload": {
                "queries": [dict(q) if isinstance(q, dict) else q for q in planned_queries],
                "budget": budget_dict,
                "destination": dest_name,
                "destinations": [d["name"] for d in destinations_info]
                if destinations_info
                else [dest_name],
                "covered_destinations": covered_dests,
                "uncovered_destinations": uncovered_dests,
                "coverage_reason": cov_reason,
                "origin": brief.get("origin_name", "")
                if isinstance(brief, dict)
                else getattr(brief, "origin_name", ""),
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
            "covered_destinations": covered_dests,
            "uncovered_destinations": uncovered_dests,
            "coverage_reason": cov_reason,
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
    graph.add_node(TripPlanningNode.RANK_CANDIDATES, rank_candidates_node)
    graph.add_node(TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER, dispatch_route_optimizer_node)
    graph.add_node(TripPlanningNode.AWAIT_ROUTE_RESULTS, await_route_results_node)
    graph.add_node(TripPlanningNode.CONSUME_ROUTE_RESULT, consume_route_result_node)
    graph.add_node(TripPlanningNode.FETCH_WEATHER, fetch_weather_node)
    graph.add_node(TripPlanningNode.COMPUTE_BUDGET, compute_budget_node)
    graph.add_node(TripPlanningNode.SYNTHESIZE_PROPOSAL, synthesize_proposal_node)
    graph.add_node(TripPlanningNode.PUBLISH_PROPOSAL, publish_proposal_node)

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
    graph.add_edge(TripPlanningNode.EVALUATE_CANDIDATES, TripPlanningNode.RANK_CANDIDATES)
    graph.add_edge(TripPlanningNode.RANK_CANDIDATES, TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER)
    graph.add_edge(TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER, TripPlanningNode.AWAIT_ROUTE_RESULTS)
    graph.add_edge(TripPlanningNode.AWAIT_ROUTE_RESULTS, TripPlanningNode.CONSUME_ROUTE_RESULT)
    graph.add_edge(TripPlanningNode.CONSUME_ROUTE_RESULT, TripPlanningNode.FETCH_WEATHER)
    graph.add_edge(TripPlanningNode.FETCH_WEATHER, TripPlanningNode.COMPUTE_BUDGET)
    graph.add_edge(TripPlanningNode.COMPUTE_BUDGET, TripPlanningNode.SYNTHESIZE_PROPOSAL)
    graph.add_edge(TripPlanningNode.SYNTHESIZE_PROPOSAL, TripPlanningNode.PUBLISH_PROPOSAL)
    graph.add_edge(TripPlanningNode.PUBLISH_PROPOSAL, END)

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
