"""Regression tests for source backed must visit attractions and opening hours research.

Written test first for spec 0007 requirements:
1. Wrong city pages rejected even without GPS coordinates.
2. Search snippet only hours never grant VERIFIED_HOURS.
3. Conflicting hours across sources marked conflicted and surfaced, never VERIFIED_HOURS.
4. Separate opening hours, ticketing hours, and last entry cutoffs with source URL,
   local timezone, and days covered enforced in solver windows.
5. Closed on trip day handling differs cleanly between recommended attractions
   (deferred/excluded) and traveler must visit stops (partial status + explicit conflict).
6. Traveler must visit places stay distinct from research recommendations (never promote
   recommended sightseeing to is_must_visit=True), uncovered destinations are reported,
   focused attractions and hours queries fit budget limits, official venue/tourism URLs
   are preferred over generic aggregators, LLM fallback validates structured output, and
   attractions remain visible when routing is unavailable.
"""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest

from contracts.candidate import (
    CandidateExistenceStatus,
    CandidateHoursStatus,
    RegisteredCandidate,
)
from contracts.trips import DestinationPoint, TripBrief
from functions.evidence_collector.service import EvidenceCollectorService
from functions.orchestrator.evidence.claim_ingestion import _match_observation_entity
from functions.orchestrator.evidence.eligibility import DeterministicEvidenceEligibilityGate
from functions.orchestrator.evidence.hours_extraction import (
    extract_structured_hours_from_text,
    extract_structured_hours_with_llm,
)
from functions.orchestrator.evidence.policy import ClaimVerificationPolicy
from functions.orchestrator.evidence.query_planner import (
    DEFAULT_MAX_QUERIES,
    QueryPlanningService,
)
from functions.orchestrator.evidence.schemas import (
    ClaimCategory,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    dispatch_route_optimizer_node,
    synthesize_proposal_node,
)
from functions.orchestrator.places.schemas import CoordinatePoint


class _StubLlmClient:
    """Minimal stub implementing HostedLlmClientPort.generate."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.0,
    ) -> str:
        _ = (prompt, system_prompt, max_tokens, temperature)
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class TestQueryPlanningAndSourceSelection:
    """Focused attractions and visit hours query planning and URL prioritization."""

    def test_focused_attractions_hours_queries_respect_budget_and_report_uncovered(self) -> None:
        brief = TripBrief(
            origin_name="Raniganj",
            origin_lat=23.6200,
            origin_lng=87.1200,
            destinations=[
                DestinationPoint(
                    name="Ooty",
                    latitude=11.4102,
                    longitude=76.6950,
                    stay_days=2,
                ),
                DestinationPoint(
                    name="Coonoor",
                    latitude=11.3530,
                    longitude=76.7959,
                    stay_days=1,
                ),
                DestinationPoint(
                    name="Kotagiri",
                    latitude=11.4218,
                    longitude=76.8617,
                    stay_days=1,
                ),
            ],
            start_date=date(2026, 12, 4),
            end_date=date(2026, 12, 7),
            must_visit_places=["Government Botanical Garden"],
        )

        queries = QueryPlanningService.plan_attractions_and_hours_queries(
            brief, max_queries=DEFAULT_MAX_QUERIES
        )
        budget = QueryPlanningService.calculate_collector_budget(queries)

        assert len(queries) <= DEFAULT_MAX_QUERIES
        assert budget["remaining_cost_minor"] <= 200
        hours_queries = [q for q in queries if q.purpose == "hours"]
        assert hours_queries
        assert all(q.engine == "google" for q in hours_queries)
        assert any("Ooty" in q.query and ("hours" in q.query.lower() or "timing" in q.query.lower()) for q in hours_queries)
        assert any("Government Botanical Garden" in q.query for q in queries)
        assert "Kotagiri" in queries.uncovered_destinations
        assert queries.coverage_reason is not None

    def test_select_eligible_search_urls_prefers_official_venue_and_tourism_pages(self) -> None:
        observations = [
            {
                "extraction_method": "SERPAPI_SEARCH",
                "canonical_url": "https://www.tripadvisor.in/Attraction_Review-Ooty-Botanical_Garden.html",
                "excerpt_text": "Botanical Garden Ooty reviews",
            },
            {
                "extraction_method": "SERPAPI_SEARCH",
                "canonical_url": "https://nilgiris.nic.in/tourist-place/government-botanical-garden/",
                "excerpt_text": "Official Nilgiris district Government Botanical Garden page",
            },
            {
                "extraction_method": "SERPAPI_SEARCH",
                "canonical_url": "https://www.tamilnadutourism.tn.gov.in/destinations/ooty",
                "excerpt_text": "Tamil Nadu Tourism Ooty guide",
            },
        ]

        selected = EvidenceCollectorService.select_eligible_search_urls(observations, max_urls=2)
        selected_urls = [entry["url"] for entry in selected]

        assert len(selected_urls) == 2
        assert "https://nilgiris.nic.in/tourist-place/government-botanical-garden/" in selected_urls
        assert "https://www.tamilnadutourism.tn.gov.in/destinations/ooty" in selected_urls


class TestWrongCityRejection:
    """Wrong city matches must be rejected even when observation coordinates are absent."""

    def test_coordless_wrong_city_observation_is_rejected(self) -> None:
        candidate = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Botanical Garden",
            category="park",
            point=CoordinatePoint(lat=11.4189, lng=76.7115),
        )
        wrong_city_obs = {
            "id": str(uuid.uuid4()),
            "canonical_url": "https://wb.gov.in/acharya-botanical-garden-kolkata",
            "title": "Botanical Garden Kolkata Timings and Entry Fee",
            "excerpt_text": "Acharya Jagadish Chandra Bose Botanical Garden in Shibpur, Howrah, Kolkata, West Bengal is open Tuesday to Sunday.",
            "artifact_ref": {
                "city_hint": "Kolkata",
                "address": "Shibpur, Howrah, Kolkata, West Bengal",
            },
        }

        is_match, _, _ = _match_observation_entity(
            wrong_city_obs,
            candidate,
            expected_cities={"Ooty", "Udhagamandalam", "Nilgiris"},
        )
        assert is_match is False


class TestHoursVerificationAndExtraction:
    """Snippet gating, deterministic/LLM extraction, conflicts, and cutoffs."""

    @pytest.mark.asyncio
    async def test_snippet_only_hours_never_grant_verified_hours(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Rose Garden",
            category="garden",
            point=CoordinatePoint(lat=11.4041, lng=76.7081),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        snippet_obs = {
            "id": str(uuid.uuid4()),
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "extraction_method": "SERPAPI_SEARCH",
            "canonical_url": "https://example.org/search-snippet",
            "excerpt_text": "Government Rose Garden Ooty Open daily 8:30 AM to 6:30 PM",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "artifact_ref": {
                "engine": "google_maps",
                "gps": {"lat": 11.4041, "lng": 76.7081},
            },
        }

        claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Rose Garden",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [{"support_type": EvidenceSupportType.SUPPORTS.value, "observation": snippet_obs}],
            },
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Rose Garden",
                "category": ClaimCategory.HOURS.value,
                "verification": VerificationStatus.UNVERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "typed_value": {
                    "opens": "08:30",
                    "closes": "18:30",
                    "source_kind": "SERPAPI_SEARCH",
                },
                "evidence_links": [{"support_type": EvidenceSupportType.SUPPORTS.value, "observation": snippet_obs}],
            },
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[snippet_obs],
            fixture_claims=claims,
            visit_dates=[date(2026, 12, 5)],
        )

        assert len(report.eligible_stops) == 1
        evaluated = report.eligible_stops[0]
        assert evaluated.hours_status != CandidateHoursStatus.VERIFIED_HOURS

    @pytest.mark.asyncio
    async def test_conflicting_hours_sources_marked_conflicted_and_never_verified(self) -> None:
        gate = DeterministicEvidenceEligibilityGate(session=None)
        exec_id = uuid.uuid4()
        cand_id = uuid.uuid4()

        candidate = RegisteredCandidate(
            place_id=cand_id,
            primary_name="Tea Factory & Museum",
            category="museum",
            point=CoordinatePoint(lat=11.4125, lng=76.7263),
            existence_status=CandidateExistenceStatus.UNVERIFIED,
        )

        obs_official = {
            "id": str(uuid.uuid4()),
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "extraction_method": "SCRAPLING_HTTP",
            "canonical_url": "https://nilgiris.nic.in/tea-museum",
            "excerpt_text": "Open daily 09:00 AM - 06:00 PM.",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "artifact_ref": {"status": "SUCCESS", "gps": {"lat": 11.4125, "lng": 76.7263}},
        }
        obs_tourism = {
            "id": str(uuid.uuid4()),
            "execution_id": str(exec_id),
            "place_id": str(cand_id),
            "extraction_method": "SCRAPLING_HTTP",
            "canonical_url": "https://tamilnadutourism.tn.gov.in/tea-museum",
            "excerpt_text": "Visiting hours 10:00 AM - 04:30 PM, closed Tuesday.",
            "support_type": EvidenceSupportType.SUPPORTS.value,
            "artifact_ref": {"status": "SUCCESS", "gps": {"lat": 11.4125, "lng": 76.7263}},
        }

        from types import SimpleNamespace

        is_conflict, _ = ClaimVerificationPolicy().detect_contradiction(
            SimpleNamespace(
                subject_id="tea factory & museum",
                category=ClaimCategory.HOURS.value,
                place_id=cand_id,
                valid_from=None,
                valid_until=None,
                typed_value={"opens": "09:00", "closes": "18:00", "closed_days": []},
            ),
            SimpleNamespace(
                subject_id="tea factory & museum",
                category=ClaimCategory.HOURS.value,
                place_id=cand_id,
                valid_from=None,
                valid_until=None,
                typed_value={"opens": "10:00", "closes": "16:30", "closed_days": ["Tuesday"]},
            ),
        )
        assert is_conflict is True

        claims = [
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Tea Factory & Museum",
                "category": ClaimCategory.ATTRACTION.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "evidence_links": [{"support_type": EvidenceSupportType.SUPPORTS.value, "observation": obs_official}],
            },
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Tea Factory & Museum",
                "category": ClaimCategory.HOURS.value,
                "verification": VerificationStatus.CONFLICTED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "typed_value": {
                    "opens": "09:00",
                    "closes": "18:00",
                    "source_url": "https://nilgiris.nic.in/tea-museum",
                    "extraction_method": "SCRAPLING_HTTP",
                },
                "evidence_links": [{"support_type": EvidenceSupportType.SUPPORTS.value, "observation": obs_official}],
            },
            {
                "execution_id": str(exec_id),
                "place_id": str(cand_id),
                "subject_id": "Tea Factory & Museum",
                "category": ClaimCategory.HOURS.value,
                "verification": VerificationStatus.VERIFIED.value,
                "freshness": FreshnessStatus.FRESH.value,
                "typed_value": {
                    "opens": "10:00",
                    "closes": "16:30",
                    "closed_days": ["Tuesday"],
                    "source_url": "https://tamilnadutourism.tn.gov.in/tea-museum",
                    "extraction_method": "SCRAPLING_HTTP",
                },
                "evidence_links": [{"support_type": EvidenceSupportType.SUPPORTS.value, "observation": obs_tourism}],
            },
        ]

        report = await gate.evaluate_candidate_eligibility(
            candidates=[candidate],
            execution_id=exec_id,
            fixture_observations=[obs_official, obs_tourism],
            fixture_claims=claims,
            visit_dates=[date(2026, 12, 5)],
        )

        assert len(report.eligible_stops) == 1
        evaluated = report.eligible_stops[0]
        assert evaluated.hours_status == CandidateHoursStatus.HOURS_CONFLICTED
        assert evaluated.hours_status != CandidateHoursStatus.VERIFIED_HOURS
        assert "conflicting" in str(evaluated.diagnostics.get("hours_diagnostic", "")).lower() or "conflict" in str(evaluated.hours_details).lower()

    def test_extract_structured_hours_captures_ticketing_last_entry_and_closed_days(self) -> None:
        text = (
            "Nilgiri Heritage Museum is open 09:30 AM - 05:30 PM from Wednesday to Monday. "
            "Closed on Tuesday. "
            "Ticketing hours: 09:30 AM - 04:15 PM. Last entry at 04:30 PM. "
            "Seasonal schedule valid Oct to Mar."
        )
        extracted = extract_structured_hours_from_text(
            text,
            source_url="https://nilgiris.nic.in/heritage-museum",
            local_timezone="Asia/Kolkata",
        )

        assert extracted.opens == "09:30"
        assert extracted.closes == "17:30"
        assert extracted.ticketing_opens == "09:30"
        assert extracted.ticketing_closes == "16:15"
        assert extracted.last_entry == "16:30"
        assert "Tuesday" in extracted.closed_days
        assert extracted.local_timezone == "Asia/Kolkata"
        assert extracted.source_url == "https://nilgiris.nic.in/heritage-museum"

    @pytest.mark.asyncio
    async def test_llm_structured_hours_extraction_validates_schema_and_rejects_bad_output(self) -> None:
        bad_client = _StubLlmClient(['{"weekly_schedule": "not valid"}'])
        rejected = await extract_structured_hours_with_llm(
            bad_client,
            place_name="Nilgiri Heritage Museum",
            destination_name="Ooty",
            page_text="Open 9 AM to 5 PM daily except Tuesday.",
            source_url="https://nilgiris.nic.in/heritage-museum",
        )
        assert rejected is None

        good_client = _StubLlmClient(
            [
                '{"weekly_schedule": [{"day": "Monday", "is_closed": false, "opens": "09:00", "closes": "17:30", "ticketing_opens": "09:00", "ticketing_closes": "16:15", "last_entry": "16:30"}], "timezone": "Asia/Kolkata", "dates_covered": "All year"}'
            ]
        )
        accepted = await extract_structured_hours_with_llm(
            good_client,
            place_name="Nilgiri Heritage Museum",
            destination_name="Ooty",
            page_text="Open Monday 09:00 AM to 05:30 PM, ticket counter closes 04:15 PM, last admission 04:30 PM.",
            source_url="https://nilgiris.nic.in/heritage-museum",
        )
        assert accepted is not None
        assert accepted.opens == "09:00"
        assert accepted.ticketing_closes == "16:15"
        assert accepted.last_entry == "16:30"


class TestItinerarySchedulingAndClosedDayHandling:
    """Last entry cutoff enforcement, closed day deferral/conflicts, must visit vs recommendation."""

    def test_closed_on_trip_day_defers_recommendation_but_flags_must_visit_partial(self) -> None:
        exec_id = uuid.uuid4()

        # Trip date 2026-12-06 is a Sunday
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Raniganj",
                "origin_lat": 23.6200,
                "origin_lng": 87.1200,
                "destinations": [
                    {
                        "name": "Ooty",
                        "latitude": 11.4102,
                        "longitude": 76.6950,
                        "stay_days": 1,
                    }
                ],
                "must_visit_places": ["Tribal Research Center Museum"],
                "start_date": "2026-12-06",
                "end_date": "2026-12-07",
                "travelers": 2,
                "budget": {"amount": "20000.00", "currency": "INR"},
                "preferred_modes": ["car_petrol"],
            },
            "eligible_stops": [
                {
                    "primary_name": "Ooty",
                    "category": "destination",
                    "point": {"lat": 11.4102, "lng": 76.6950},
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
                {
                    "primary_name": "Tribal Research Center Museum",
                    "category": "museum",
                    "point": {"lat": 11.4300, "lng": 76.7050},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "CLOSED_ON_TRIP_DAY",
                    "is_must_visit": True,
                    "hours_details": {
                        "closed_days": ["Sunday"],
                        "source_url": "https://nilgiris.nic.in/tribal-museum",
                        "local_timezone": "Asia/Kolkata",
                    },
                },
                {
                    "primary_name": "Regional Science Park",
                    "category": "attraction",
                    "point": {"lat": 11.4090, "lng": 76.7010},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "CLOSED_ON_TRIP_DAY",
                    "is_must_visit": False,
                    "hours_details": {
                        "closed_days": ["Sunday"],
                        "source_url": "https://nilgiris.nic.in/science-park",
                        "local_timezone": "Asia/Kolkata",
                    },
                },
                {
                    "primary_name": "Pykara Lake Boat House",
                    "category": "attraction",
                    "point": {"lat": 11.4646, "lng": 76.5974},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_must_visit": False,
                    "window_start": time(9, 30),
                    "window_end": time(17, 30),
                    "hours_details": {
                        "opens": "09:30",
                        "closes": "17:30",
                        "ticketing_opens": "09:30",
                        "ticketing_closes": "16:15",
                        "last_entry": "16:30",
                        "local_timezone": "Asia/Kolkata",
                        "source_url": "https://nilgiris.nic.in/pykara",
                    },
                },
            ],
        }

        dispatched = dispatch_route_optimizer_node(state)
        from contracts.routing import RouteRequest

        request = RouteRequest.model_validate(dispatched["pending_tasks"][0]["payload"])
        stop_by_name = {s.name: s for s in request.stops}

        # Recommended Pykara stays optional (is_must_visit=False) and enforces ticketing cutoff 16:15
        assert "Pykara Lake Boat House" in stop_by_name
        pykara = stop_by_name["Pykara Lake Boat House"]
        assert pykara.is_must_visit is False
        assert pykara.window_end is not None
        # 17:15 IST equals 11:45 UTC (before 17:30 IST / 12:00 UTC close)
        pykara_utc_minutes = pykara.window_end.hour * 60 + pykara.window_end.minute
        assert pykara_utc_minutes == 11 * 60 + 45

        # Closed recommended attraction is excluded with reason
        assert "Regional Science Park" not in stop_by_name
        deferred_reasons = " ".join(str(x) for x in dispatched.get("deferred_candidates", []))
        assert "Regional Science Park" in deferred_reasons
        assert "Sunday" in deferred_reasons

        # Closed traveler must visit generates explicit conflict and keeps proposal partial
        conflicts = dispatched.get("must_visit_conflicts", [])
        assert any("Tribal Research Center Museum" in str(c) for c in conflicts)

    def test_real_world_formats_title_variants_and_multiday_open_date_selection(self) -> None:
        # Dot separated and 24h pm suffix (seen live on Mysuru Rail Museum website)
        dot_extracted = extract_structured_hours_from_text(
            "Timings : 10.00 am - 18.00pm. Closed Monday to Tuesday.",
            source_url="http://mysururailmuseum.com/",
        )
        assert dot_extracted is not None
        assert dot_extracted.opens == "10:00"
        assert dot_extracted.closes == "18:00"
        assert "Monday" in dot_extracted.closed_days
        assert "Tuesday" in dot_extracted.closed_days
        assert "Monday" not in dot_extracted.days_covered
        assert "Tuesday" not in dot_extracted.days_covered

        # Real page title carrying expected city and hours keywords matches cleanly
        cand = RegisteredCandidate(
            place_id=uuid.uuid4(),
            primary_name="Government Botanical Garden",
            category="garden",
            point=CoordinatePoint(lat=11.4189, lng=76.7115),
        )
        valid_titled_obs = {
            "id": str(uuid.uuid4()),
            "canonical_url": "https://nilgiris.nic.in/government-botanical-garden",
            "title": "Government Botanical Garden Ooty Timings",
            "extraction_method": "SCRAPLING_HTTP",
            "excerpt_text": "Open 07:00 AM to 06:30 PM daily.",
            "artifact_ref": {"page_title": "Government Botanical Garden Ooty Timings", "city_hint": "Ooty"},
        }
        is_match, _, _ = _match_observation_entity(
            valid_titled_obs,
            cand,
            expected_cities={"Ooty", "Udhagamandalam"},
        )
        assert is_match is True

        # Multi day trip Fri-Sun selects open Saturday window when tentative date is closed
        exec_id = uuid.uuid4()
        multi_state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394, "stay_days": 1}],
                "start_date": "2026-12-04",
                "end_date": "2026-12-06",
            },
            "eligible_stops": [
                {
                    "primary_name": "Mysuru",
                    "category": "destination",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
                {
                    "primary_name": "Weekend Museum",
                    "category": "museum",
                    "point": {"lat": 12.3010, "lng": 76.6500},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "hours_details": {
                        "opens": "09:00",
                        "closes": "17:00",
                        "closed_days": ["Friday"],
                        "open_visit_dates": [
                            {"date": "2026-12-05", "weekday": "Saturday", "opens": "10:00", "closes": "18:00"}
                        ],
                    },
                },
            ],
        }
        dispatched = dispatch_route_optimizer_node(multi_state)
        from contracts.routing import RouteRequest

        req = RouteRequest.model_validate(dispatched["pending_tasks"][0]["payload"])
        wm = next(s for s in req.stops if s.name == "Weekend Museum")
        assert wm.window_start is not None
        assert wm.window_start.date().isoformat() == "2026-12-05"

    def test_synthesis_preserves_attractions_on_routing_unavailable_and_reports_must_visit_partial(self) -> None:
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Raniganj",
                "origin_lat": 23.6200,
                "origin_lng": 87.1200,
                "destinations": [{"name": "Ooty", "latitude": 11.4102, "longitude": 76.6950}],
                "must_visit_places": ["Government Botanical Garden"],
                "start_date": "2026-12-06",
                "end_date": "2026-12-07",
                "travelers": 2,
                "budget": {"amount": "20000.00", "currency": "INR"},
                "preferred_modes": ["car_petrol"],
            },
            "routing_status": "ROUTING_UNAVAILABLE",
            "routing_failure_reason": "Valhalla distance cap exceeded",
            "must_visit_conflicts": [
                {
                    "place_name": "Government Botanical Garden",
                    "reason": "Opening hours unverified from search snippets only",
                    "hours_status": "HOURS_ASSUMED",
                }
            ],
            "eligible_stops": [
                {
                    "primary_name": "Government Botanical Garden",
                    "category": "garden",
                    "point": {"lat": 11.4189, "lng": 76.7115},
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                    "is_must_visit": True,
                },
                {
                    "primary_name": "Ooty Lake",
                    "category": "attraction",
                    "point": {"lat": 11.4064, "lng": 76.6897},
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "is_must_visit": False,
                    "hours_details": {
                        "opens": "09:00",
                        "closes": "18:00",
                        "source_url": "https://nilgiris.nic.in/ooty-lake",
                        "local_timezone": "Asia/Kolkata",
                    },
                },
            ],
            "destination_research": {
                "covered_destinations": ["Ooty"],
                "uncovered_destinations": ["Coonoor"],
                "coverage_reason": "Query cap reached after covering primary destination",
            },
        }

        synthesized = synthesize_proposal_node(state)
        proposal = synthesized["proposal"]

        assert proposal["status"] == "partial"
        assert proposal.get("must_visit_conflicts")
        assert any(c.get("name") == "Ooty Lake" for c in proposal.get("attractions_research", []))
        assert proposal.get("research_coverage", {}).get("uncovered_destinations") == ["Coonoor"]
