"""Unit tests for LangGraph end-to-end trip planning workflow."""

from datetime import date

import pytest

from travel.domain.trips import DestinationPoint, TransportMode, TripBrief
from travel.workflows.trip_planning import trip_planning_workflow


@pytest.mark.asyncio
async def test_langgraph_trip_planning_workflow() -> None:
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394, stay_days=2),
            DestinationPoint(name="Coorg", latitude=12.4244, longitude=75.7382, stay_days=2),
        ],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 5),
        adults=2,
        rooms=1,
        preferred_modes=[TransportMode.CAR_PETROL],
    )

    initial_state = {
        "trip_id": "test-trip-123",
        "version": 1,
        "brief": brief.model_dump(mode="json"),
    }

    result = await trip_planning_workflow.ainvoke(initial_state)

    assert result["status"] == "completed"
    assert "proposal" in result
    proposal = result["proposal"]
    assert proposal["status"] == "ready_for_approval"

    # Verify schedule structure
    assert "schedule" in proposal
    assert len(proposal["schedule"]["stops"]) == 3  # Origin + 2 destinations

    # Verify budget summary presence and non-coercion
    assert "budget_summary" in proposal
    assert proposal["budget_summary"]["is_complete"] is False  # Unknown tolls present
    assert proposal["budget_summary"]["unknown_count"] > 0
