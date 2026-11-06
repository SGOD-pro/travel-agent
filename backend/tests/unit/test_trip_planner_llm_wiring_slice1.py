"""Unit tests for trip planner LLM research and ranking graph wiring.

Verifies:
- discover_candidates_node queries HostedLlmClientPort and preserves brief destinations
- Fallback on LLM research timeout/failure to brief destinations with honest diagnostics
- rank_candidates_node evaluates and orders candidates by fit
- Fallback on ranking failure preserving candidate order with honest diagnostics
- Graph compilation includes RANK_CANDIDATES node between EVALUATE_CANDIDATES and DISPATCH_ROUTE_OPTIMIZER
- Proposals remain unverified and cannot bypass deterministic eligibility gates
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.candidate import CandidateExistenceStatus
from contracts.llm import (
    PlaceProposal,
    RankedPlace,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningNode,
    TripPlanningState,
    build_trip_planning_graph,
    compile_trip_planning_workflow,
    discover_candidates_node,
    evaluate_candidates_node,
    rank_candidates_node,
)
from functions.orchestrator.llm.adapters.bedrock import BedrockLlmClient
from functions.orchestrator.llm.adapters.fixture_client import FixtureLlmClient
from functions.orchestrator.llm.factory import create_llm_client
from functions.orchestrator.llm.port import HostedLlmClientPort


def _make_brief(
    origin_name: str = "Kolkata",
    origin_lat: float = 22.5726,
    origin_lng: float = 88.3639,
    dest_name: str = "Digha",
    dest_lat: float = 21.6266,
    dest_lng: float = 87.5074,
) -> dict[str, Any]:
    return {
        "origin_name": origin_name,
        "origin_lat": origin_lat,
        "origin_lng": origin_lng,
        "destinations": [
            {
                "name": dest_name,
                "latitude": dest_lat,
                "longitude": dest_lng,
                "confidence": 1.0,
                "stay_days": 1,
            }
        ],
        "start_date": "2026-11-10",
        "end_date": "2026-11-12",
        "timezone": "Asia/Kolkata",
        "adults": 2,
        "rooms": 1,
        "preferred_modes": ["car_petrol"],
        "dietary_preferences": ["bengali", "street_food"],
        "accessibility_requirements": [],
        "hard_constraints": [],
    }


class TestTripPlannerLlmWiring:
    """Verifies graph wiring, typed LLM contracts, and evidence boundaries."""

    def test_graph_compilation_includes_rank_candidates(self) -> None:
        graph = build_trip_planning_graph()
        assert TripPlanningNode.DISCOVER_CANDIDATES in graph.nodes
        assert TripPlanningNode.EVALUATE_CANDIDATES in graph.nodes
        assert TripPlanningNode.RANK_CANDIDATES in graph.nodes
        assert TripPlanningNode.DISPATCH_ROUTE_OPTIMIZER in graph.nodes

        workflow = compile_trip_planning_workflow()
        assert workflow is not None

    @pytest.mark.asyncio
    async def test_discover_candidates_invokes_llm_and_preserves_brief_destinations(self) -> None:
        brief = _make_brief()
        mock_llm = MagicMock(spec=HostedLlmClientPort)
        mock_llm.research_places = AsyncMock(
            return_value=ResearchResponse(
                candidate_places=[
                    PlaceProposal(
                        name="Kolaghat Express Dhaba",
                        category="restaurant",
                        estimated_latitude=22.4285,
                        estimated_longitude=87.8712,
                        description="Midway snacks and tea",
                        fit_score=0.92,
                        suggested_stay_minutes=40,
                        price_estimate_inr=200.0,
                        uncertainty="EDITORIAL_DISCOVERY",
                        is_candidate=True,
                    )
                ],
                regional_travel_notes=["Monsoon caution on NH16"],
                recommended_pace="MODERATE",
                provider="mock_llm",
            )
        )

        state: TripPlanningState = {
            "brief": brief,
            "status": "RUNNING",
            "_llm_client": mock_llm,
        }

        res = await discover_candidates_node(state)

        assert res["current_step"] == "candidates_discovered"
        discovered = res["discovered_places"]
        assert len(discovered) == 2  # Brief destination Digha + Kolaghat Express Dhaba

        # Brief destination is preserved
        digha = next((d for d in discovered if d["name"] == "Digha"), None)
        assert digha is not None
        assert digha["category"] == "destination"

        # LLM proposal is added as unverified editorial discovery candidate
        kolaghat = next((d for d in discovered if d["name"] == "Kolaghat Express Dhaba"), None)
        assert kolaghat is not None
        assert kolaghat["category"] == "restaurant"
        assert kolaghat["uncertainty"] == "EDITORIAL_DISCOVERY"
        assert kolaghat["is_candidate"] is True

        assert mock_llm.research_places.call_count == 1
        req: ResearchRequest = mock_llm.research_places.call_args[0][0]
        assert req.origin_name == "Kolkata"
        assert req.destination_names == ["Digha"]
        assert "bengali" in req.traveler_preferences

    @pytest.mark.asyncio
    async def test_discover_candidates_provider_failure_falls_back_to_brief_destinations(self) -> None:
        brief = _make_brief()
        mock_llm = MagicMock(spec=HostedLlmClientPort)
        mock_llm.research_places = AsyncMock(side_effect=TimeoutError("Bedrock socket timeout"))

        state: TripPlanningState = {
            "brief": brief,
            "status": "RUNNING",
            "_llm_client": mock_llm,
        }

        res = await discover_candidates_node(state)

        assert res["current_step"] == "candidates_discovered"
        discovered = res["discovered_places"]
        # Must strictly preserve brief destination despite external LLM failure
        assert len(discovered) == 1
        assert discovered[0]["name"] == "Digha"

        # Honest partial diagnostic recorded without crash
        diags = res.get("provider_diagnostics", {})
        assert "llm_research" in diags
        assert "Preserved brief destination stops" in diags["llm_research"]
        assert "TimeoutError" in diags["llm_research"]

    @pytest.mark.asyncio
    async def test_rank_candidates_node_orders_eligible_stops(self) -> None:
        brief = _make_brief()
        c1 = {
            "primary_name": "Low Fit Spot",
            "category": "attraction",
            "point": {"lat": 22.0, "lng": 88.0},
            "fit_score": 0.5,
            "suggested_stay_minutes": 60,
            "is_candidate": False,
            "uncertainty": "VERIFIED_EVIDENCE",
        }
        c2 = {
            "primary_name": "High Fit Spot",
            "category": "heritage",
            "point": {"lat": 22.1, "lng": 88.1},
            "fit_score": 0.95,
            "suggested_stay_minutes": 90,
            "is_candidate": False,
            "uncertainty": "VERIFIED_EVIDENCE",
        }

        mock_llm = MagicMock(spec=HostedLlmClientPort)
        mock_llm.rank_places = AsyncMock(
            return_value=RankingResponse(
                ranked_places=[
                    RankedPlace(
                        place=PlaceProposal(
                            name="High Fit Spot",
                            category="heritage",
                            estimated_latitude=22.1,
                            estimated_longitude=88.1,
                            fit_score=0.99,
                        ),
                        rank=1,
                        fit_score=0.99,
                        selection_rationale="Best match for cultural interests",
                    ),
                    RankedPlace(
                        place=PlaceProposal(
                            name="Low Fit Spot",
                            category="attraction",
                            estimated_latitude=22.0,
                            estimated_longitude=88.0,
                            fit_score=0.60,
                        ),
                        rank=2,
                        fit_score=0.60,
                        selection_rationale="Secondary stop",
                    ),
                ],
                eliminated_candidates=[],
                overall_pacing_evaluation="Feasible pace",
                provider="mock_llm",
            )
        )

        state: TripPlanningState = {
            "brief": brief,
            "eligible_stops": [c1, c2],  # Initially c1 is first
            "_llm_client": mock_llm,
        }

        res = await rank_candidates_node(state)

        assert res["current_step"] == "candidates_ranked"
        assert len(res["ranked_places"]) == 2
        # Assert eligible_stops was reordered based on model rank: High Fit Spot is now first
        ordered_stops = res["eligible_stops"]
        assert ordered_stops[0]["primary_name"] == "High Fit Spot"
        assert ordered_stops[1]["primary_name"] == "Low Fit Spot"

    @pytest.mark.asyncio
    async def test_rank_candidates_provider_failure_preserves_order(self) -> None:
        brief = _make_brief()
        c1 = {"primary_name": "First Spot", "point": {"lat": 22.0, "lng": 88.0}}
        c2 = {"primary_name": "Second Spot", "point": {"lat": 22.1, "lng": 88.1}}

        mock_llm = MagicMock(spec=HostedLlmClientPort)
        mock_llm.rank_places = AsyncMock(side_effect=RuntimeError("Bedrock service throttled"))

        state: TripPlanningState = {
            "brief": brief,
            "eligible_stops": [c1, c2],
            "_llm_client": mock_llm,
        }

        res = await rank_candidates_node(state)

        assert res["current_step"] == "candidates_ranked"
        # Order is preserved
        assert res["eligible_stops"][0]["primary_name"] == "First Spot"
        assert res["eligible_stops"][1]["primary_name"] == "Second Spot"
        diags = res.get("provider_diagnostics", {})
        assert "llm_ranking" in diags
        assert "RuntimeError" in diags["llm_ranking"]

    @pytest.mark.asyncio
    async def test_llm_proposals_cannot_bypass_evidence_eligibility_gate(self) -> None:
        """Confirms invariant: LLM place proposals cannot mark themselves verified or enter solver stops directly."""
        brief = _make_brief()
        unverified_llm_proposal = {
            "name": "Hallucinated Palace",
            "category": "heritage",
            "lat": 22.2,
            "lng": 88.2,
            "confidence": 0.99,
            "stay_minutes": 60,
            "uncertainty": "EDITORIAL_DISCOVERY",
            "is_candidate": True,
        }

        state: TripPlanningState = {
            "execution_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "brief": brief,
            "discovered_places": [unverified_llm_proposal],
            "_offline_fixture_mode": True,  # Isolated in-memory evaluation
            "task_outputs": {},  # No evidence observations or verified claims provided
        }

        res = await evaluate_candidates_node(state)

        assert res["current_step"] == "candidates_evaluated"
        # Invariant: Unverified proposal must NOT enter eligible_stops
        assert not any(s["primary_name"] == "Hallucinated Palace" for s in res["eligible_stops"])
        # Instead, it must be relegated strictly to editorial recommendations
        recs = res["editorial_recommendations"]
        relegated = next((r for r in recs if r["primary_name"] == "Hallucinated Palace"), None)
        assert relegated is not None
        assert relegated["existence_status"] == CandidateExistenceStatus.UNVERIFIED.value
        assert relegated["uncertainty"] == "EDITORIAL_DISCOVERY"

    def test_fixture_mode_only_when_explicitly_configured(self) -> None:
        client_bedrock = create_llm_client(provider="bedrock")
        assert isinstance(client_bedrock, BedrockLlmClient)
        assert not isinstance(client_bedrock, FixtureLlmClient)

        client_fixture = create_llm_client(provider="fixture")
        assert isinstance(client_fixture, FixtureLlmClient)
