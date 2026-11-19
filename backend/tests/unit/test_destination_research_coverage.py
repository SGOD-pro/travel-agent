"""Unit tests for bounded destination research coverage and search routing.

Conforms to 0007 bounded destination research requirements:
- Distribute research across brief destinations (e.g. Mysuru + Madikeri).
- Preserve call/cost ceilings (<= 4 queries, 200 minor INR).
- Single-destination full coverage (attractions, dining, lodging, advisory).
- More destinations than available query slots (uncovered destinations and reason).
- Advisory versus venue engine selection at adapter boundary (google vs google_maps).
- Credential redaction in HTTP client logging.
- Preservation of advisory distinction in place extraction and observation metadata.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from contracts.trips import DestinationPoint, TripBrief
from functions.evidence_collector.serpapi_adapter import (
    CredentialRedactionFilter,
    SerpApiAdapter,
)
from functions.orchestrator.evidence.place_extraction import PlaceExtractionService
from functions.orchestrator.evidence.query_planner import (
    DEFAULT_COST_PER_CALL_MINOR,
    DEFAULT_MAX_QUERIES,
    PlannedQuery,
    PlannedQueryList,
    QueryPlanningService,
)


def test_multi_destination_coverage_mysuru_madikeri() -> None:
    """Verifies Mysuru + Madikeri research distributes across both destinations within bounds."""
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(
                name="Mysuru",
                latitude=12.2958,
                longitude=76.6394,
                stay_days=2,
            ),
            DestinationPoint(
                name="Madikeri",
                latitude=12.4244,
                longitude=75.7382,
                stay_days=2,
            ),
        ],
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 5),
        traveler_preferences=["coffee plantations", "historic palaces", "local karnataka thali"],
    )

    queries = QueryPlanningService.plan_destination_queries(brief, max_queries=4)

    assert isinstance(queries, PlannedQueryList)
    assert len(queries) <= DEFAULT_MAX_QUERIES
    assert len(queries) == 4

    # Both destinations are covered
    assert "Mysuru" in queries.covered_destinations
    assert "Madikeri" in queries.covered_destinations
    assert queries.uncovered_destinations == []
    assert queries.is_complete_coverage is True
    assert queries.coverage_reason is None

    # Slot 1: Mysuru attractions on google_maps
    mysuru_q = next((q for q in queries if q.destination == "Mysuru" and q.purpose == "attractions"), None)
    assert mysuru_q is not None
    assert "Mysuru" in mysuru_q.query
    assert mysuru_q.engine == "google_maps"

    # Slot 2: Madikeri attractions on google_maps
    madikeri_q = next((q for q in queries if q.destination == "Madikeri" and q.purpose == "attractions"), None)
    assert madikeri_q is not None
    assert "Madikeri" in madikeri_q.query
    assert madikeri_q.engine == "google_maps"

    # Slot 3: Dining on google_maps
    dining_q = next((q for q in queries if q.purpose == "dining"), None)
    assert dining_q is not None
    assert dining_q.engine == "google_maps"

    # Slot 4: Road travel advisory on google web search
    adv_q = next((q for q in queries if q.purpose == "advisory"), None)
    assert adv_q is not None
    assert adv_q.engine == "google"
    assert "road travel advisory" in adv_q.query or "route conditions" in adv_q.query

    # Budget ceiling verification
    budget = QueryPlanningService.calculate_collector_budget(queries)
    assert budget["remaining_tool_calls"] == 4
    assert budget["cost_per_call_minor"] == DEFAULT_COST_PER_CALL_MINOR  # 50
    assert budget["remaining_cost_minor"] == 200
    assert budget["currency"] == "INR"


def test_single_destination_behavior() -> None:
    """Verifies single-destination brief covers attractions, dining, lodging, and advisory."""
    brief_dict = {
        "origin_name": "Kolkata",
        "destinations": [{"name": "Darjeeling", "latitude": 27.0410, "longitude": 88.2663}],
        "dietary_preferences": ["vegetarian"],
        "budget_tier": "MODERATE",
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)

    assert isinstance(queries, PlannedQueryList)
    assert len(queries) == 4
    assert queries.covered_destinations == ["Darjeeling"]
    assert queries.uncovered_destinations == []
    assert queries.is_complete_coverage is True

    purposes = [q.purpose for q in queries]
    assert "attractions" in purposes
    assert "dining" in purposes
    assert "lodging" in purposes
    assert "advisory" in purposes

    # Engine routing: venue queries use google_maps, advisory uses google
    for q in queries:
        if q.purpose == "advisory":
            assert q.engine == "google"
        else:
            assert q.engine == "google_maps"


def test_more_destinations_than_available_query_slots() -> None:
    """Verifies budget preservation when destination count exceeds available query slots."""
    brief_dict = {
        "origin_name": "Bengaluru",
        "destinations": [
            {"name": "Mysuru"},
            {"name": "Madikeri"},
            {"name": "Ooty"},
            {"name": "Coonoor"},
            {"name": "Wayanad"},
        ],
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)

    # Ceiling is strictly preserved
    assert len(queries) == 4
    # 3 destination slots + 1 advisory slot
    assert queries.covered_destinations == ["Mysuru", "Madikeri", "Ooty"]
    assert queries.uncovered_destinations == ["Coonoor", "Wayanad"]
    assert queries.is_complete_coverage is False
    assert queries.coverage_reason is not None
    assert "Destination count (5) exceeds available research query slots (3)" in queries.coverage_reason
    assert "Coonoor, Wayanad" in queries.coverage_reason

    # Never claim complete coverage
    assert queries.is_complete_coverage is False


def test_structure_and_validate_queries_rebalances_missing_destinations() -> None:
    """Verifies that query validation ensures destination coverage across multiple destinations."""
    raw_queries = [
        "Mysuru palace entry tickets timings",
        "Mysuru zoo animals family visit",
        "Mysuru authentic local food",
        "Mysuru silk saree shopping",
    ]
    destinations = ["Mysuru", "Madikeri"]

    structured = QueryPlanningService.structure_and_validate_queries(
        queries=raw_queries,
        destinations=destinations,
        origin="Bengaluru",
        max_queries=4,
    )

    assert len(structured) <= 4
    covered = structured.covered_destinations
    assert "Mysuru" in covered
    assert "Madikeri" in covered
    assert structured.is_complete_coverage is True


@pytest.mark.asyncio
async def test_advisory_versus_venue_engine_selection_at_adapter_boundary() -> None:
    """Verifies SerpApiAdapter routes advisory to engine=google and venues to engine=google_maps."""
    adapter = SerpApiAdapter(api_key="test_key_sample")

    captured_requests: list[dict[str, Any]] = []

    mock_client = AsyncMock()

    async def mock_get(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> MagicMock:
        captured_requests.append(dict(params or {}))
        resp = MagicMock()
        resp.status_code = 200
        # Return empty organic/place results
        resp.json.return_value = {
            "search_metadata": {"id": "test_search_1", "status": "Success"},
            "organic_results": [
                {
                    "title": "Route Advisory NH275",
                    "link": "https://example.com/advisory",
                    "snippet": "Road work ongoing between Mysuru and Kushalnagar.",
                }
            ],
            "places_results": [
                {
                    "title": "Mysuru Palace",
                    "link": "https://example.com/mysuru-palace",
                    "description": "Historic royal palace in Mysuru.",
                    "gps_coordinates": {"latitude": 12.3051, "longitude": 76.6551},
                }
            ],
        }
        return resp

    mock_client.get = mock_get

    with patch("httpx.AsyncClient", return_value=mock_client):
        # 1. Search with advisory purpose -> engine="google"
        adv_obs = await adapter.search(
            "Bengaluru to Mysuru road conditions advisory",
            extra_params={"engine": "google", "purpose": "advisory", "destination": "Mysuru"},
        )
        assert len(captured_requests) == 1
        assert captured_requests[0]["engine"] == "google"
        assert captured_requests[0]["q"] == "Bengaluru to Mysuru road conditions advisory"
        # Non-SerpAPI extra parameters must not be sent over HTTP
        assert "purpose" not in captured_requests[0]
        assert "destination" not in captured_requests[0]
        # But observation metadata carries purpose and engine
        assert len(adv_obs) > 0
        assert adv_obs[0].artifact_ref is not None
        assert adv_obs[0].artifact_ref["query_purpose"] == "advisory"
        assert adv_obs[0].artifact_ref["engine"] == "google"

        # 2. Search with venue purpose -> engine="google_maps"
        captured_requests.clear()
        venue_obs = await adapter.search(
            "Mysuru top attractions",
            extra_params={"engine": "google_maps", "purpose": "attractions", "destination": "Mysuru"},
        )
        assert len(captured_requests) == 1
        assert captured_requests[0]["engine"] == "google_maps"
        assert captured_requests[0]["q"] == "Mysuru top attractions"
        assert "purpose" not in captured_requests[0]
        assert len(venue_obs) > 0
        assert venue_obs[0].artifact_ref is not None
        assert venue_obs[0].artifact_ref["query_purpose"] == "attractions"
        assert venue_obs[0].artifact_ref["engine"] == "google_maps"


def test_serpapi_credential_logging_redaction() -> None:
    """Verifies that CredentialRedactionFilter redacts api_key parameter in logging records across msg and args."""
    import httpx

    redaction_filter = CredentialRedactionFilter()

    # Case 1: Pre-rendered string in record.msg
    record_msg = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="HTTP Request: GET https://serpapi.com/search?api_key=secret_token_12345&q=Mysuru 'HTTP/1.1 200 OK'",
        args=(),
        exc_info=None,
    )
    assert redaction_filter.filter(record_msg) is True
    assert "secret_token_12345" not in record_msg.msg
    assert "api_key=[REDACTED]" in record_msg.msg

    # Case 2: Parameterized args tuple matching real httpx / httpcore format
    record_args = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", httpx.URL("https://serpapi.com/search?api_key=secret_param_67890&q=Madikeri"), "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    assert redaction_filter.filter(record_args) is True
    assert "secret_param_67890" not in str(record_args.args)
    assert "api_key=%5BREDACTED%5D" in str(record_args.args) or "api_key=[REDACTED]" in str(record_args.args)
    formatted = record_args.getMessage()
    assert "secret_param_67890" not in formatted
    assert "api_key=%5BREDACTED%5D" in formatted or "api_key=[REDACTED]" in formatted

    # Case 3: Parameterized dictionary args
    record_dict = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Request to %(url)s completed",
        args=({"url": "https://serpapi.com/search?api_key=secret_dict_555&engine=google"},),
        exc_info=None,
    )
    assert redaction_filter.filter(record_dict) is True
    assert "secret_dict_555" not in str(record_dict.args)
    assert "api_key=[REDACTED]" in record_dict.getMessage()


def test_place_extraction_ignores_advisory_observations() -> None:
    """Verifies that place extraction skips observations with query_purpose=advisory."""
    advisory_obs = {
        "source_url": "https://roads.karnataka.gov.in/advisories/nh275",
        "publisher": "Karnataka PWD",
        "excerpt_text": "NH275 four-lane expressway between Bengaluru and Mysuru is open with toll plazas active.",
        "artifact_ref": {
            "title": "NH275 Expressway Status",
            "gps_coordinates": {"latitude": 12.5, "longitude": 76.9},
            "query_purpose": "advisory",
            "purpose": "advisory",
            "category": "advisory",
            "destination": "Mysuru",
        },
    }

    venue_obs = {
        "source_url": "https://karnatakatourism.org/mysuru-palace",
        "publisher": "Karnataka Tourism",
        "excerpt_text": "Mysuru Palace is an iconic Indo-Saracenic royal residence located at 12.3051, 76.6551.",
        "artifact_ref": {
            "title": "Mysuru Palace",
            "gps_coordinates": {"latitude": 12.3051, "longitude": 76.6551},
            "query_purpose": "attractions",
            "purpose": "attractions",
            "destination": "Mysuru",
        },
    }

    candidates = PlaceExtractionService.extract_candidates_from_observations([advisory_obs, venue_obs])

    # Advisory observation must NEVER become a candidate place
    candidate_names = [c.name.lower() for c in candidates]
    assert not any("road" in name or "nh275" in name or "expressway" in name for name in candidate_names)
    assert any("palace" in name for name in candidate_names)


def test_zero_destinations_behavior() -> None:
    """Verifies planning behavior when trip brief contains no destinations."""
    brief_dict = {
        "origin_name": "Bengaluru",
        "destinations": [],
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)

    assert len(queries) == 4
    assert queries.covered_destinations == []
    assert queries.uncovered_destinations == []
    assert queries.is_complete_coverage is False
    assert queries.coverage_reason == "No destinations provided in trip brief."


def test_three_destinations_coverage_behavior() -> None:
    """Verifies planning behavior when trip brief contains exactly 3 destinations."""
    brief_dict = {
        "origin_name": "Bengaluru",
        "destinations": [
            {"name": "Mysuru"},
            {"name": "Madikeri"},
            {"name": "Hassan"},
        ],
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)

    assert len(queries) == 4
    assert queries.covered_destinations == ["Mysuru", "Madikeri", "Hassan"]
    assert queries.uncovered_destinations == []
    assert queries.is_complete_coverage is True
    assert queries.coverage_reason is None


def test_planned_query_mentions_and_dict_membership() -> None:
    """Verifies that PlannedQuery handles standard dict membership and explicit mentions method."""
    q = PlannedQuery(
        "Mysuru top attractions sightseeing places to visit",
        purpose="attractions",
        destination="Mysuru",
        engine="google_maps",
    )

    # Standard dict membership checks keys only
    assert "purpose" in q
    assert "destination" in q
    assert "query" in q
    assert "engine" in q
    assert "Mysuru" not in q  # "Mysuru" is not a dict key!

    # Explicit mentions method checks text substring
    assert q.mentions("Mysuru")
    assert q.mentions("attractions")
    assert not q.mentions("Madikeri")


def test_planned_query_list_slicing_preserves_metadata() -> None:
    """Verifies that slicing PlannedQueryList preserves coverage metadata."""
    brief_dict = {
        "origin_name": "Bengaluru",
        "destinations": [
            {"name": "Mysuru"},
            {"name": "Madikeri"},
            {"name": "Ooty"},
            {"name": "Coonoor"},
        ],
    }

    queries = QueryPlanningService.plan_destination_queries(brief_dict, max_queries=4)
    assert isinstance(queries, PlannedQueryList)
    assert queries.is_complete_coverage is False
    assert len(queries.uncovered_destinations) > 0

    sliced = queries[:2]
    assert isinstance(sliced, PlannedQueryList)
    assert len(sliced) == 2
    assert sliced.covered_destinations == queries.covered_destinations
    assert sliced.uncovered_destinations == queries.uncovered_destinations
    assert sliced.coverage_reason == queries.coverage_reason
    assert sliced.is_complete_coverage == queries.is_complete_coverage


def test_structure_and_validate_queries_empty_destinations() -> None:
    """Verifies that structure_and_validate_queries with empty destinations sets explicit reason."""
    raw_queries = [
        "top places to visit",
        "best restaurants to eat",
    ]
    res = QueryPlanningService.structure_and_validate_queries(raw_queries, destinations=[])
    assert res.covered_destinations == []
    assert res.uncovered_destinations == []
    assert res.is_complete_coverage is False
    assert res.coverage_reason == "No destinations provided in trip brief."


def test_structure_and_validate_queries_eviction_derives_correct_coverage() -> None:
    """Verifies that evicting a query does not leave an evicted destination in covered_destinations."""
    # Start with 3 queries for DestA and 1 query for DestB
    raw_queries = [
        {"query": "DestA attractions", "purpose": "attractions", "destination": "DestA", "engine": "google_maps"},
        {"query": "DestA dining", "purpose": "dining", "destination": "DestA", "engine": "google_maps"},
        {"query": "DestB attractions", "purpose": "attractions", "destination": "DestB", "engine": "google_maps"},
        {"query": "DestA road conditions", "purpose": "advisory", "destination": "DestA", "engine": "google"},
    ]
    # Trip brief has DestA, DestB, DestC. DestC is missing and must replace a query.
    res = QueryPlanningService.structure_and_validate_queries(
        raw_queries,
        destinations=["DestA", "DestB", "DestC"],
        max_queries=4,
    )
    # The duplicate DestA query should be replaced with DestC
    covered = set(res.covered_destinations)
    assert "DestC" in covered
    # And covered_destinations must match actual non-advisory query destinations
    actual_dests = {q.destination for q in res if q.purpose != "advisory"}
    assert set(res.covered_destinations) == actual_dests


def test_plan_destination_queries_constrained_budget_derives_correct_coverage() -> None:
    """Verifies that plan_destination_queries with max_queries < destination count derives accurate coverage."""
    brief = TripBrief(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        destinations=[
            DestinationPoint(
                name="Mysuru",
                latitude=12.2958,
                longitude=76.6394,
                stay_days=2,
            ),
            DestinationPoint(
                name="Madikeri",
                latitude=12.4244,
                longitude=75.7382,
                stay_days=2,
            ),
        ],
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 5),
    )

    queries = QueryPlanningService.plan_destination_queries(brief, max_queries=1)
    assert len(queries) == 1
    assert queries.covered_destinations == ["Mysuru"]
    assert queries.uncovered_destinations == ["Madikeri"]
    assert queries.is_complete_coverage is False
    assert queries.coverage_reason is not None
    assert "Uncovered destinations: Madikeri" in queries.coverage_reason



