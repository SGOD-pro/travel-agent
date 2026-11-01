"""Unit tests for TripPlannerGraph proposal publication, budget computation, and versioning.

Conforms to specification 0007 Slice 5, AC-1, AC-7, AC-8, AC-9:
- Fuel math labeled ESTIMATED based on vehicle profile and distance.
- Accommodation labeled ESTIMATED based on stay duration benchmarks.
- Verified entrance fees labeled QUOTED; unpriced attractions labeled UNKNOWN with reasons.
- Budget summary completeness: known totals exclude unknown costs without zero coercion.
- Synthesized proposal includes daily schedule, candidate cards, weather advisories, budget lines, route ID.
- Pessimistic row locking and atomic proposal publication to trips and trip_deltas.
- Version validation: version conflict records PROPOSAL_SUPERSEDED and marks execution SUPERSEDED.
- Tenant isolation: foreign trip access raises PermissionError.
- Cancellation and deadline barriers halt publication.
- Replay safety: idempotent execution does not insert duplicate deltas.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.money import BudgetCategory
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningNode,
    TripPlanningState,
    build_trip_planning_graph,
    compile_trip_planning_workflow,
    compute_budget_node,
    publish_proposal_node,
    synthesize_proposal_node,
)


def _make_sample_state() -> TripPlanningState:
    trip_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    return {
        "trip_id": trip_id,
        "owner_id": owner_id,
        "execution_id": execution_id,
        "version": 1,
        "brief": {
            "origin_name": "Kolkata",
            "origin_lat": 22.5726,
            "origin_lng": 88.3639,
            "destinations": [
                {
                    "name": "Digha",
                    "latitude": 21.6266,
                    "longitude": 87.5074,
                    "stay_days": 2,
                }
            ],
            "start_date": "2026-11-03",
            "end_date": "2026-11-05",
            "rooms": 1,
            "adults": 2,
            "budget_tier": "standard",
            "preferred_modes": ["car_petrol"],
            "pace": "medium",
            "interests": ["beach", "nature"],
            "timezone": "Asia/Kolkata",
        },
        "optimized_schedule": {
            "stops": [
                {
                    "stop_id": "stop_0",
                    "name": "Kolkata",
                    "stop_type": "DEPARTURE",
                    "arrival_time": "2026-11-03T09:00:00+05:30",
                    "departure_time": "2026-11-03T09:00:00+05:30",
                    "dwell_minutes": 0,
                },
                {
                    "stop_id": "stop_1",
                    "name": "Marine Aquarium",
                    "stop_type": "INTERMEDIATE",
                    "arrival_time": "2026-11-03T13:00:00+05:30",
                    "departure_time": "2026-11-03T14:30:00+05:30",
                    "dwell_minutes": 90,
                },
                {
                    "stop_id": "stop_2",
                    "name": "Digha Beach",
                    "stop_type": "DESTINATION",
                    "arrival_time": "2026-11-03T15:00:00+05:30",
                    "departure_time": "2026-11-03T18:00:00+05:30",
                    "dwell_minutes": 180,
                },
            ],
            "legs": [
                {
                    "from_stop": "Kolkata",
                    "to_stop": "Marine Aquarium",
                    "distance_km": 180.5,
                    "duration_minutes": 240,
                },
                {
                    "from_stop": "Marine Aquarium",
                    "to_stop": "Digha Beach",
                    "distance_km": 5.0,
                    "duration_minutes": 15,
                },
            ],
            "total_travel_minutes": 255,
            "is_optimal": True,
        },
        "validated_candidates": [
            {
                "place_id": str(uuid.uuid4()),
                "primary_name": "Marine Aquarium",
                "category": "museum",
                "price_estimate_inr": None,
                "is_candidate": False,
            },
            {
                "place_id": str(uuid.uuid4()),
                "primary_name": "Digha Beach",
                "category": "attraction",
                "price_estimate_inr": 50.0,
                "verified_price_inr": 50.0,
                "fee_evidence_ref": "obs:ticket_digha_beach",
                "is_candidate": False,
            },
        ],
        "discovered_places": [],
        "editorial_recommendations": [],
        "weather_advisories": [
            {
                "stop_name": "Digha",
                "temperature_celsius": 28.5,
                "condition": "sunny",
                "monsoon_risk": "low",
            }
        ],
        "weather_status": "AVAILABLE",
        "route_id": str(uuid.uuid4()),
        "route_status": "FEASIBLE",
        "routing_status": "FEASIBLE",
        "route_diagnostics": [],
        "status": "RUNNING",
    }


def _make_mock_session() -> tuple[AsyncMock, MagicMock]:
    mock_session = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__.return_value = None
    mock_tx.__aexit__.return_value = None
    mock_session.begin = MagicMock(return_value=mock_tx)
    mock_session.add = MagicMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__.return_value = mock_session
    mock_factory.return_value.__aexit__.return_value = None
    return mock_session, mock_factory


def test_build_trip_planning_graph_includes_publish_proposal_node() -> None:
    """Verifies that TripPlannerGraph compiles with publish_proposal node connected to END."""
    graph = build_trip_planning_graph()
    assert TripPlanningNode.COMPUTE_BUDGET in graph.nodes
    assert TripPlanningNode.SYNTHESIZE_PROPOSAL in graph.nodes
    assert TripPlanningNode.PUBLISH_PROPOSAL in graph.nodes

    workflow = compile_trip_planning_workflow()
    assert workflow is not None


@pytest.mark.asyncio
async def test_compute_budget_calculates_estimated_fuel_and_accommodation() -> None:
    """Verifies fuel math is labeled ESTIMATED and accommodation is computed from stay days."""
    state = _make_sample_state()
    res = await compute_budget_node(state)

    assert res["current_step"] == "budget_computed"
    lines = res["budget_lines"]
    summary = res["budget_summary"]

    fuel_lines = [line for line in lines if line["category"] == BudgetCategory.TRANSPORT.value]
    assert len(fuel_lines) == 2
    for fl in fuel_lines:
        assert fl["estimated"] is True
        assert fl["amount"] is not None
        assert Decimal(str(fl["amount"])) > Decimal("0")

    hotel_lines = [line for line in lines if line["category"] == BudgetCategory.ACCOMMODATION.value]
    assert len(hotel_lines) == 1
    assert hotel_lines[0]["estimated"] is True
    assert hotel_lines[0]["quantity"] == "2"  # 1 room * 2 nights

    # Toll lines: unverified toll between segments produces explicit UNKNOWN lines
    toll_lines = [line for line in lines if line["category"] == BudgetCategory.TOLLS_AND_FEES.value]
    assert len(toll_lines) == 2
    for tl in toll_lines:
        assert tl["amount"] is None
        assert "unverified" in tl["unknown_reason"]

    assert summary["is_complete"] is False
    assert summary["unknown_count"] >= 2
    assert Decimal(str(summary["known_total"]["amount"])) > Decimal("0")


@pytest.mark.asyncio
async def test_compute_budget_handles_quoted_and_unpriced_attractions() -> None:
    """Verifies that verified attractions generate quoted lines and unpriced ones produce UNKNOWN lines."""
    state = _make_sample_state()
    res = await compute_budget_node(state)
    lines = res["budget_lines"]

    activity_lines = [line for line in lines if line["category"] == BudgetCategory.ACTIVITIES.value]
    assert len(activity_lines) == 2

    priced = [line for line in activity_lines if line["basis"] == "entry_fee_Digha Beach"][0]
    assert priced["amount"] == "50.0"
    assert priced["estimated"] is False  # QUOTED under AC-7
    assert priced["source_ref"] == "obs:ticket_digha_beach"

    unpriced = [line for line in activity_lines if line["basis"] == "entry_fee_Marine Aquarium"][0]
    assert unpriced["amount"] is None
    assert "Entrance fee unverified" in unpriced["unknown_reason"]


@pytest.mark.asyncio
async def test_compute_budget_unverified_proposal_prices_remain_unknown() -> None:
    """Verifies under AC-7 that unverified place proposal prices never become quoted or estimated."""
    state = _make_sample_state()
    state["validated_candidates"] = [
        {
            "place_id": str(uuid.uuid4()),
            "primary_name": "Digha Beach",
            "category": "attraction",
            "price_estimate_inr": 100.0,
            "verified_price_inr": None,
            "is_candidate": False,
        }
    ]
    state["optimized_schedule"]["stops"] = [
        {
            "stop_id": "stop_2",
            "name": "Digha Beach",
            "stop_type": "DESTINATION",
            "arrival_time": "2026-11-03T15:00:00+05:30",
            "departure_time": "2026-11-03T18:00:00+05:30",
            "dwell_minutes": 180,
        }
    ]
    res = await compute_budget_node(state)
    activity_lines = [line for line in res["budget_lines"] if line["category"] == BudgetCategory.ACTIVITIES.value]
    assert len(activity_lines) == 1
    line = activity_lines[0]
    assert line["amount"] is None
    assert "Entrance fee unverified" in line["unknown_reason"]


@pytest.mark.asyncio
async def test_compute_budget_when_routing_unavailable_preserves_accommodation_without_fuel() -> None:
    """Verifies fail-closed routing does not fabricate road segments or fuel expenses."""
    state = _make_sample_state()
    state["route_id"] = None
    state["routing_status"] = "ROUTING_UNAVAILABLE"
    state["optimized_schedule"] = {"stops": [], "legs": []}

    res = await compute_budget_node(state)
    lines = res["budget_lines"]

    fuel_lines = [line for line in lines if line["category"] == BudgetCategory.TRANSPORT.value]
    toll_lines = [line for line in lines if line["category"] == BudgetCategory.TOLLS_AND_FEES.value]
    assert len(fuel_lines) == 0
    assert len(toll_lines) == 0

    hotel_lines = [line for line in lines if line["category"] == BudgetCategory.ACCOMMODATION.value]
    assert len(hotel_lines) == 1
    assert hotel_lines[0]["amount"] == "3500.00"


@pytest.mark.asyncio
async def test_synthesize_proposal_assembles_complete_contract() -> None:
    """Verifies synthesize_proposal_node compiles schedule, budget, weather, and diagnostics."""
    state = _make_sample_state()
    budget_res = await compute_budget_node(state)
    state["budget_lines"] = budget_res["budget_lines"]
    state["budget_summary"] = budget_res["budget_summary"]

    res = synthesize_proposal_node(state)
    assert res["current_step"] == "proposal_synthesized"
    proposal = res["proposal"]

    assert proposal["trip_id"] == state["trip_id"]
    assert proposal["version"] == 1
    assert proposal["route_id"] == state["route_id"]
    assert proposal["weather_status"] == "AVAILABLE"
    assert len(proposal["weather_advisories"]) == 1
    assert len(proposal["budget_lines"]) > 0
    assert proposal["budget_summary"]["is_complete"] is False
    assert proposal["status"] == "ready_for_approval"


@pytest.mark.asyncio
async def test_publish_proposal_offline_fixture_mode_bypasses_database() -> None:
    """Verifies offline fixture mode safely publishes without database dependencies."""
    state = _make_sample_state()
    state["_offline_fixture_mode"] = True
    state["proposal"] = {"status": "ready_for_approval"}

    res = await publish_proposal_node(state)
    assert res["current_step"] == "proposal_published"
    assert res["proposal_status"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_publish_proposal_cancelled_state_halts() -> None:
    """Verifies cancelled workflow state halts without database publication."""
    state = _make_sample_state()
    state["status"] = "CANCELLED"

    res = await publish_proposal_node(state)
    assert res["current_step"] == "proposal_publication_cancelled"
    assert res["status"] == "CANCELLED"
    assert res["proposal_status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_publish_proposal_enforces_tenant_isolation_on_mismatch() -> None:
    """Verifies caller tenant mismatch raises PermissionError during publication."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_trip = MagicMock()
    mock_trip.id = uuid.UUID(state["trip_id"])
    mock_trip.owner_id = uuid.uuid4()  # Foreign owner
    mock_trip.current_version = 1

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row

    mock_result_trip = MagicMock()
    mock_result_trip.scalar_one_or_none.return_value = mock_trip

    mock_session.execute.side_effect = [mock_result_exec, mock_result_trip]

    with pytest.raises(PermissionError, match="Tenant isolation mismatch"):
        await publish_proposal_node(
            state,
            config={"configurable": {"session_factory": mock_factory}},
        )


@pytest.mark.asyncio
async def test_publish_proposal_version_conflict_records_superseded() -> None:
    """Verifies that an advanced trip version rejects publication and marks execution SUPERSEDED."""
    state = _make_sample_state()
    state["version"] = 1
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_trip = MagicMock()
    mock_trip.id = uuid.UUID(state["trip_id"])
    mock_trip.owner_id = uuid.UUID(state["owner_id"])
    mock_trip.current_version = 2  # Advanced concurrently!

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row

    mock_result_trip = MagicMock()
    mock_result_trip.scalar_one_or_none.return_value = mock_trip

    mock_result_seq = MagicMock()
    mock_result_seq.scalar.return_value = 1

    mock_session.execute.side_effect = [
        mock_result_exec,
        mock_result_trip,
        mock_result_seq,
    ]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )

    assert res["current_step"] == "proposal_superseded"
    assert res["status"] == "SUPERSEDED"
    assert res["proposal_status"] == "SUPERSEDED"
    assert mock_exec_row.status == "SUPERSEDED"


@pytest.mark.asyncio
async def test_publish_proposal_deadline_expired_halts() -> None:
    """Verifies that expired execution deadline halts publication with status CANCELLED."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = datetime.now(UTC) - timedelta(minutes=5)  # Expired
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row

    mock_session.execute.side_effect = [mock_result_exec]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )

    assert res["current_step"] == "proposal_publication_cancelled"
    assert res["status"] == "CANCELLED"
    assert res["proposal_status"] == "CANCELLED"
    assert mock_exec_row.status == "CANCELLED"


@pytest.mark.asyncio
async def test_publish_proposal_publishes_to_trips_and_deltas_when_version_matches() -> None:
    """Verifies atomic proposal publication to trips and trip_deltas when version matches."""
    state = _make_sample_state()
    state["version"] = 1
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_trip = MagicMock()
    mock_trip.id = uuid.UUID(state["trip_id"])
    mock_trip.owner_id = uuid.UUID(state["owner_id"])
    mock_trip.current_version = 1
    mock_trip.current_route_id = None
    mock_trip.itinerary_json = None
    mock_trip.state = "draft"

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row

    mock_result_trip = MagicMock()
    mock_result_trip.scalar_one_or_none.return_value = mock_trip

    mock_result_delta = MagicMock()
    mock_result_delta.scalar_one_or_none.return_value = None  # No existing delta

    mock_result_seq = MagicMock()
    mock_result_seq.scalar.return_value = 1

    mock_session.execute.side_effect = [
        mock_result_exec,
        mock_result_trip,
        mock_result_delta,
        mock_result_seq,
    ]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )

    assert res["current_step"] == "proposal_published"
    assert res["status"] == "completed"
    assert res["proposal_status"] == "PUBLISHED"

    # Trip model updated
    assert mock_trip.state == "proposed"
    assert mock_trip.current_route_id == uuid.UUID(state["route_id"])
    assert mock_trip.itinerary_json == state["proposal"]
    assert mock_session.add.called


@pytest.mark.asyncio
async def test_publish_proposal_replay_idempotency_updates_existing_delta() -> None:
    """Verifies that duplicate execution does not insert duplicate delta records."""
    state = _make_sample_state()
    state["version"] = 1
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_trip = MagicMock()
    mock_trip.id = uuid.UUID(state["trip_id"])
    mock_trip.owner_id = uuid.UUID(state["owner_id"])
    mock_trip.current_version = 1

    mock_existing_delta = MagicMock()
    mock_existing_delta.proposal_json = {}

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row

    mock_result_trip = MagicMock()
    mock_result_trip.scalar_one_or_none.return_value = mock_trip

    mock_result_delta = MagicMock()
    mock_result_delta.scalar_one_or_none.return_value = mock_existing_delta  # Existing delta!

    mock_result_seq = MagicMock()
    mock_result_seq.scalar.return_value = 2

    mock_session.execute.side_effect = [
        mock_result_exec,
        mock_result_trip,
        mock_result_delta,
        mock_result_seq,
    ]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )

    assert res["current_step"] == "proposal_published"
    assert res["proposal_status"] == "PUBLISHED"
    assert mock_existing_delta.proposal_json == state["proposal"]
    # session.add should NOT be called for TripDeltaModel since it already exists
    from functions.api.models import TripDeltaModel

    assert not any(
        isinstance(arg, TripDeltaModel)
        for call in mock_session.add.call_args_list
        for arg in call.args
    )


@pytest.mark.asyncio
async def test_publish_proposal_fails_closed_when_trip_not_found() -> None:
    """Verifies fail closed behavior when trip record is missing from database."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row

    mock_result_trip = MagicMock()
    mock_result_trip.scalar_one_or_none.return_value = None  # Trip missing!

    mock_session.execute.side_effect = [mock_result_exec, mock_result_trip]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )

    assert res["current_step"] == "proposal_publication_failed"
    assert res["status"] == "FAILED"
    assert res["proposal_status"] == "FAILED"
    assert "Trip" in res["errors"][0]


@pytest.mark.asyncio
async def test_publish_proposal_production_missing_identity_fails_closed() -> None:
    """Verifies Finding 1: production publication fails closed if session_factory, trip_id, owner_id, or execution_id is missing."""
    state = _make_sample_state()
    state["_offline_fixture_mode"] = False
    state["proposal"] = {"status": "ready_for_approval"}

    # Missing session_factory
    res = await publish_proposal_node(state, config={"configurable": {"session_factory": None}})
    assert res["current_step"] == "proposal_publication_failed"
    assert res["status"] == "FAILED"
    assert res["proposal_status"] == "FAILED"

    # Missing trip_id
    state_no_trip = dict(state)
    state_no_trip["trip_id"] = None
    res2 = await publish_proposal_node(
        cast(TripPlanningState, state_no_trip),
        config={"configurable": {"session_factory": MagicMock()}},
    )
    assert res2["status"] == "FAILED"

    # Missing owner_id
    state_no_owner = dict(state)
    state_no_owner["owner_id"] = None
    res3 = await publish_proposal_node(
        cast(TripPlanningState, state_no_owner),
        config={"configurable": {"session_factory": MagicMock()}},
    )
    assert res3["status"] == "FAILED"

    # Missing execution_id
    state_no_exec = dict(state)
    state_no_exec["execution_id"] = None
    res4 = await publish_proposal_node(
        cast(TripPlanningState, state_no_exec),
        config={"configurable": {"session_factory": MagicMock()}},
    )
    assert res4["status"] == "FAILED"


@pytest.mark.asyncio
async def test_publish_proposal_cross_owner_execution_mismatch_raises_permission_error() -> None:
    """Verifies Finding 2: execution belonging to a different owner raises PermissionError under lock."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.uuid4()  # Cross-owner mismatch!
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row
    mock_session.execute.side_effect = [mock_result_exec]

    with pytest.raises(PermissionError, match="Tenant isolation mismatch"):
        await publish_proposal_node(
            state,
            config={"configurable": {"session_factory": mock_factory}},
        )


@pytest.mark.asyncio
async def test_publish_proposal_cross_trip_execution_mismatch_raises_value_error() -> None:
    """Verifies Finding 2: execution bound to a different trip raises ValueError under lock."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.uuid4()  # Cross-trip mismatch!
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row
    mock_session.execute.side_effect = [mock_result_exec]

    with pytest.raises(ValueError, match="Cross-trip execution mismatch"):
        await publish_proposal_node(
            state,
            config={"configurable": {"session_factory": mock_factory}},
        )


@pytest.mark.asyncio
async def test_publish_proposal_cancelled_at_halts_publication() -> None:
    """Verifies Finding 2: cancelled_at timestamp halts publication under transaction lock."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = datetime.now(UTC)  # Cancelled via timestamp
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row
    mock_session.execute.side_effect = [mock_result_exec]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )
    assert res["current_step"] == "proposal_publication_cancelled"
    assert res["status"] == "CANCELLED"
    assert res["proposal_status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_publish_proposal_lost_or_expired_runner_lease_rejected() -> None:
    """Verifies Finding 3: lost or expired runner lease halts publication without writing trip or delta."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    held_lease = uuid.uuid4()
    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "RUNNING"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = held_lease
    mock_exec_row.lease_expiry = datetime.now(UTC) + timedelta(minutes=10)

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row
    mock_session.execute.side_effect = [mock_result_exec]

    # Another runner tries to publish with different lease token
    stale_runner_lease = uuid.uuid4()
    res = await publish_proposal_node(
        state,
        config={
            "configurable": {
                "session_factory": mock_factory,
                "runner_lease": stale_runner_lease,
            }
        },
    )
    assert res["current_step"] == "proposal_publication_lost_lease"
    assert res["status"] == "FAILED"
    assert res["proposal_status"] == "FAILED"

    # Expired runner lease rejected
    mock_exec_row.lease_expiry = datetime.now(UTC) - timedelta(seconds=1)
    mock_session.execute.side_effect = [mock_result_exec]
    res_expired = await publish_proposal_node(
        state,
        config={
            "configurable": {
                "session_factory": mock_factory,
                "runner_lease": held_lease,
            }
        },
    )
    assert res_expired["current_step"] == "proposal_publication_lost_lease"
    assert res_expired["status"] == "FAILED"
    assert res_expired["proposal_status"] == "FAILED"


@pytest.mark.asyncio
async def test_publish_proposal_terminal_execution_ineligible() -> None:
    """Verifies Finding 2: already terminal execution (e.g. SUCCEEDED) rejects re-publication."""
    state = _make_sample_state()
    state["proposal"] = {"status": "ready_for_approval"}

    mock_session, mock_factory = _make_mock_session()

    mock_exec_row = MagicMock()
    mock_exec_row.id = uuid.UUID(state["execution_id"])
    mock_exec_row.owner_id = uuid.UUID(state["owner_id"])
    mock_exec_row.trip_id = uuid.UUID(state["trip_id"])
    mock_exec_row.status = "SUCCEEDED"
    mock_exec_row.deadline_at = None
    mock_exec_row.cancelled_at = None
    mock_exec_row.lease_token = None
    mock_exec_row.lease_expiry = None

    mock_result_exec = MagicMock()
    mock_result_exec.scalar_one_or_none.return_value = mock_exec_row
    mock_session.execute.side_effect = [mock_result_exec]

    res = await publish_proposal_node(
        state,
        config={"configurable": {"session_factory": mock_factory}},
    )
    assert res["current_step"] == "proposal_publication_ineligible"
    assert res["status"] == "SUCCEEDED"
    assert res["proposal_status"] == "SUCCEEDED"

