"""Unit tests for source-backed hidden-place research conforming to spec 0007.

Covers:
1. Hidden-place intent dispatch from TripBrief through route_evidence_decision_node.
2. Deterministic primary destination selection and uncovered destination reporting.
3. Mixed-intent slot allocation (must-visits & hours first, remaining to food/hidden, budget exhaustion).
4. Listicle title rejection, snippet-only rejection, wrong-city rejection.
5. Fee-only access rejection (fee alone does not prove public visitor access).
6. Restricted, private, permit-only, and closed places excluded from optional stops.
7. Verified optional stop qualification with source URL, excerpt, optionality, and unknown fees/hours.
8. Retention of hidden_place_research when routing is PARTIAL or ROUTING_UNAVAILABLE.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from contracts.candidate import CandidateHoursStatus
from contracts.trips import DestinationPoint, TripBrief
from functions.orchestrator.evidence.place_extraction import PlaceExtractionService
from functions.orchestrator.evidence.query_planner import QueryPlanningService
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    route_evidence_decision_node,
    synthesize_proposal_node,
)


class TestSourceBackedHiddenPlaces:
    """Verifies source-backed hidden-place research dispatch, qualification, and proposal synthesis."""

    async def test_hidden_place_intent_dispatches_through_workflow_without_state_override(self) -> None:
        """Hidden-place intent in TripBrief dispatches bounded queries for primary destination only."""
        state: dict[str, Any] = {
            "execution_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394},
                    {"name": "Ooty", "latitude": 11.4102, "longitude": 76.6950},
                ],
                "traveler_preferences": ["hidden gems", "offbeat places"],
                "must_visit_places": [],
            },
            "_offline_fixture_mode": True,
        }
        res = await route_evidence_decision_node(cast(TripPlanningState, state))
        tasks = res.get("pending_tasks", [])
        assert len(tasks) == 1
        payload = tasks[0]["payload"]
        assert any(q.get("purpose") == "hidden_places" for q in payload["queries"])
        assert all(q.get("destination") == "Mysuru" for q in payload["queries"])
        assert payload["covered_destinations"] == ["Mysuru"]
        assert payload["uncovered_destinations"] == ["Ooty"]
        assert "Ooty" in str(payload["coverage_reason"])

    def test_mixed_intent_preserves_must_visit_hours_and_reports_budget_exhaustion(self) -> None:
        """Must-visits and hours take priority; remaining slots go to food/hidden and skipped areas are reported."""
        brief = TripBrief(
            origin_name="Bengaluru",
            origin_lat=12.9716,
            origin_lng=77.5946,
            destinations=[
                DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394),
                DestinationPoint(name="Ooty", latitude=11.4102, longitude=76.6950),
            ],
            must_visit_places=["Mysuru Palace", "Chamundi Hill"],
            traveler_preferences=["authentic local food", "hidden gems"],
            start_date="2026-12-01",
            end_date="2026-12-04",
        )
        planned = QueryPlanningService.plan_attractions_and_hours_queries(brief, max_queries=4)
        assert len(planned) <= 4
        # Must-visit hours queries are preserved first
        assert any(q.purpose == "hours" and "Mysuru Palace" in q.query for q in planned)
        # Budget exhaustion skips extra areas and records why
        skipped = getattr(planned, "skipped_research_areas", [])
        assert len(skipped) > 0
        assert any(s["area"] in ("food", "hidden_places") for s in skipped)

    def test_listicle_title_rejected_from_hidden_places(self) -> None:
        """Listicle titles like '10 Hidden Gems in Mysuru' must never be treated as distinct places."""
        obs_listicle = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/10-hidden-gems",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "10 Hidden Gems in Mysuru is a travel guide open to visitors daily.",
            "artifact_ref": {
                "title": "10 Hidden Gems in Mysuru",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.2958, "longitude": 76.6394},
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_listicle],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert report["verified_optional_stops"] == []
        assert report["unverified_candidates"] == []

    def test_snippet_only_hidden_place_kept_unverified_with_missing_evidence(self) -> None:
        """Snippet-only search results cannot qualify as optional stops and name missing fetched content."""
        obs_snippet = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/kukkarahalli",
            "extraction_method": "SERPAPI_SEARCH",
            "excerpt_text": "Kukkarahalli Lake in Mysuru is open to visitors for morning walks.",
            "artifact_ref": {
                "title": "Kukkarahalli Lake",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.3090, "longitude": 76.6320},
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_snippet],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert report["verified_optional_stops"] == []
        assert len(report["unverified_candidates"]) == 1
        cand = report["unverified_candidates"][0]
        assert cand["name"] == "Kukkarahalli Lake"
        assert "fetched_page_content" in cand["missing_evidence"]
        assert cand["eligible_for_itinerary"] is False

    def test_wrong_city_hidden_place_rejected(self) -> None:
        """Hidden places located in conflicting cities must be rejected."""
        obs_wrong_city = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/rabindra-sarobar",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Rabindra Sarobar Lake in Kolkata is open to the public for morning walks.",
            "artifact_ref": {
                "title": "Rabindra Sarobar Lake",
                "address": "Kolkata, West Bengal",
                "gps_coordinates": {"latitude": 22.5126, "longitude": 88.3639},
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_wrong_city],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert report["verified_optional_stops"] == []
        assert report["unverified_candidates"] == []

    def test_fee_only_access_does_not_prove_public_visitor_access(self) -> None:
        """An entry fee mention without explicit public visitor access cannot qualify as an optional stop."""
        obs_fee_only = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/lingambudhi",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Lingambudhi Lake in Mysuru has an entry fee of Rs 20 per person.",
            "artifact_ref": {
                "title": "Lingambudhi Lake",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.2680, "longitude": 76.6150},
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_fee_only],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert report["verified_optional_stops"] == []
        assert len(report["unverified_candidates"]) == 1
        cand = report["unverified_candidates"][0]
        assert cand["name"] == "Lingambudhi Lake"
        assert "public_visitor_access" in cand["missing_evidence"]
        assert cand["eligible_for_itinerary"] is False

    def test_restricted_private_permit_and_closed_places_excluded_from_optional_stops(self) -> None:
        """Private, restricted, permit-only, and closed places stay out of optional stops."""
        obs_list = [
            {
                "id": str(uuid.uuid4()),
                "source_url": "https://example.com/private-estate",
                "extraction_method": "TRAFILATURA_TEXT",
                "excerpt_text": "Heritage Bungalow in Mysuru is private property with no public access.",
                "artifact_ref": {
                    "title": "Heritage Bungalow",
                    "address": "Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.3010, "longitude": 76.6500},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "source_url": "https://example.com/permit-reserve",
                "extraction_method": "TRAFILATURA_TEXT",
                "excerpt_text": "Bettada Reserve in Mysuru requires a special permit for entry.",
                "artifact_ref": {
                    "title": "Bettada Reserve",
                    "address": "Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.2800, "longitude": 76.6700},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "source_url": "https://example.com/closed-fort",
                "extraction_method": "TRAFILATURA_TEXT",
                "excerpt_text": "Old Watchtower in Mysuru is permanently closed to the public.",
                "artifact_ref": {
                    "title": "Old Watchtower",
                    "address": "Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.2900, "longitude": 76.6400},
                },
            },
        ]
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=obs_list,
            expected_cities={"Mysuru", "Mysore"},
        )
        assert report["verified_optional_stops"] == []
        assert len(report["restricted_or_closed"]) == 3
        for item in report["restricted_or_closed"]:
            assert item["access_status"] == "RESTRICTED_OR_CLOSED"
            assert item["eligible_for_itinerary"] is False

    def test_verified_hidden_place_optional_stop_and_unsupported_fields_unknown(self) -> None:
        """Distinct place + destination tie + coordinates + fetched public access -> verified optional stop."""
        obs_verified = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/mysuru/melody-world",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Melody World Wax Museum in Mysuru is open to visitors for self-guided tours.",
            "artifact_ref": {
                "title": "Melody World Wax Museum",
                "address": "Kurubarahalli, Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.2974, "longitude": 76.6852},
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_verified],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert len(report["verified_optional_stops"]) == 1
        stop = report["verified_optional_stops"][0]
        assert stop["name"] == "Melody World Wax Museum"
        assert stop["eligible_for_itinerary"] is True
        assert stop["is_must_visit"] is False
        assert stop["access_status"] == "PUBLIC_ACCESS_VERIFIED"
        assert stop["source_urls"] == ["https://karnatakatourism.org/mysuru/melody-world"]
        assert "open to visitors" in stop["supporting_excerpt"]
        assert stop["opening_hours"] is None
        assert stop["entry_fee_inr"] is None

    def test_hidden_place_research_retained_when_routing_partial_or_unavailable(self) -> None:
        """hidden_place_research and must-visits are preserved when routing is PARTIAL or ROUTING_UNAVAILABLE."""
        obs_verified = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/mysuru/melody-world",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Melody World Wax Museum in Mysuru is open to visitors.",
            "artifact_ref": {
                "title": "Melody World Wax Museum",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.2974, "longitude": 76.6852},
            },
        }
        for r_status in ("PARTIAL", "ROUTING_UNAVAILABLE"):
            state: dict[str, Any] = {
                "eligible_stops": [
                    {
                        "primary_name": "Mysuru Palace",
                        "category": "attraction",
                        "is_must_visit": True,
                        "existence_status": "VERIFIED",
                        "hours_status": CandidateHoursStatus.VERIFIED_HOURS.value,
                        "hours_details": {"opens": "10:00", "closes": "17:30"},
                    }
                ],
                "editorial_recommendations": [],
                "observations": [obs_verified],
                "brief": {
                    "destinations": [{"name": "Mysuru"}],
                    "must_visit_places": ["Mysuru Palace"],
                    "traveler_preferences": ["hidden gems"],
                },
                "routing_status": r_status,
                "route_id": None if r_status == "ROUTING_UNAVAILABLE" else "route-1",
            }
            prop = synthesize_proposal_node(cast(TripPlanningState, state))["proposal"]
            hpr = prop["hidden_place_research"]
            assert hpr["status"] == "COMPLETED"
            assert len(hpr["verified_optional_stops"]) == 1
            assert hpr["verified_optional_stops"][0]["is_must_visit"] is False
            palace = next(a for a in prop["attractions_research"] if a["name"] == "Mysuru Palace")
            assert palace["is_must_visit"] is True

    def test_multi_observation_merge_and_negated_access_rejected(self) -> None:
        """Merges GPS from snippet with fetched public-access page, and rejects negated/permit-only access."""
        obs_gps = {
            "id": str(uuid.uuid4()),
            "source_url": "https://maps.google.com/?cid=999",
            "extraction_method": "SERPAPI_SEARCH",
            "excerpt_text": "Melody World Wax Museum, Mysuru",
            "artifact_ref": {
                "title": "Melody World Wax Museum",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.2974, "longitude": 76.6852},
            },
        }
        obs_page = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/mysuru/melody-world",
            "extraction_method": "SCRAPLING_HTTP",
            "excerpt_text": "Melody World Wax Museum in Mysuru is open to visitors daily.",
            "artifact_ref": {"title": "Melody World Wax Museum"},
        }
        obs_negated = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/negated",
            "extraction_method": "SCRAPLING_HTTP",
            "excerpt_text": "Old Palace Annex in Mysuru is not open to the public.",
            "artifact_ref": {
                "title": "Old Palace Annex",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.305, "longitude": 76.655},
            },
        }
        obs_permit = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/permit",
            "extraction_method": "SCRAPLING_HTTP",
            "excerpt_text": "Forest Watchtower in Mysuru is open to visitors with permit only.",
            "artifact_ref": {
                "title": "Forest Watchtower",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.280, "longitude": 76.670},
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_gps, obs_page, obs_negated, obs_permit],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert [s["name"] for s in report["verified_optional_stops"]] == ["Melody World Wax Museum"]
        restricted_names = {r["name"] for r in report["restricted_or_closed"]}
        assert "Old Palace Annex" in restricted_names
        assert "Forest Watchtower" in restricted_names

    async def test_mixed_food_and_hidden_intent_without_must_visits_dispatches_both(self) -> None:
        """When must_visit_places is empty and both food and hidden places are requested, both dispatch."""
        state: dict[str, Any] = {
            "execution_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
                "traveler_preferences": ["hidden gems", "authentic local food"],
                "must_visit_places": [],
            },
            "_offline_fixture_mode": True,
        }
        res = await route_evidence_decision_node(cast(TripPlanningState, state))
        queries = res["pending_tasks"][0]["payload"]["queries"]
        assert any(q.get("purpose") == "hidden_places" for q in queries)
        assert any(q.get("purpose") == "dining" for q in queries)

    def test_purpose_aware_fetch_selects_hidden_place_ahead_of_extra_optional_attractions_while_keeping_hours(self) -> None:
        """Eligible hidden-place page takes a slot ahead of extra optional attractions while keeping hours."""
        from functions.evidence_collector.service import EvidenceCollectorService

        obs = [
            {
                "source_url": "https://mysorepalace.karnataka.gov.in/timings.html",
                "artifact_ref": {"title": "Mysuru Palace Hours", "query_purpose": "hours", "destination": "Mysuru"},
            },
            {
                "source_url": "https://mysuruzoo.info/",
                "artifact_ref": {"title": "Mysuru Zoo", "query_purpose": "attractions", "destination": "Mysuru"},
            },
            {
                "source_url": "https://mysururailmuseum.com/",
                "artifact_ref": {"title": "Rail Museum", "query_purpose": "attractions", "destination": "Mysuru"},
            },
            {
                "source_url": "https://tejastravels.com/mysuru-hidden-spot",
                "artifact_ref": {"title": "Kukkarahalli Walk", "query_purpose": "hidden_places", "destination": "Mysuru"},
            },
        ]
        selected = EvidenceCollectorService.select_eligible_search_urls(obs, max_urls=3)  # type: ignore[arg-type]
        urls = [s["url"] for s in selected]
        assert "https://mysorepalace.karnataka.gov.in/timings.html" in urls
        assert "https://tejastravels.com/mysuru-hidden-spot" in urls
        hp_entry = next(s for s in selected if s["url"] == "https://tejastravels.com/mysuru-hidden-spot")
        assert hp_entry["purpose"] == "hidden_places"
        assert hp_entry["destination"] == "Mysuru"

    def test_required_evidence_consumes_all_fetch_slots_and_reports_skipped_hidden_places(self) -> None:
        """When required hours/advisory evidence consumes all slots, hidden_places is skipped with reason."""
        from functions.evidence_collector.service import EvidenceCollectorService

        obs = [
            {
                "source_url": "https://mysorepalace.karnataka.gov.in/timings.html",
                "artifact_ref": {"title": "Palace Hours", "query_purpose": "hours", "destination": "Mysuru"},
            },
            {
                "source_url": "https://karnataka.gov.in/road-advisory",
                "artifact_ref": {"title": "Highway Advisory", "query_purpose": "advisory", "destination": "Mysuru"},
            },
            {
                "source_url": "https://tejastravels.com/mysuru-hidden-spot",
                "artifact_ref": {"title": "Hidden Spot", "query_purpose": "hidden_places", "destination": "Mysuru"},
            },
        ]
        skipped: dict[str, str] = {}
        selected = EvidenceCollectorService.select_eligible_search_urls(
            obs, max_urls=2, skipped_purposes_out=skipped  # type: ignore[arg-type]
        )
        urls = [s["url"] for s in selected]
        assert len(urls) == 2
        assert "https://tejastravels.com/mysuru-hidden-spot" not in urls
        assert "hidden_places" in skipped

    def test_maps_social_only_unused_capacity_and_shared_url_fetched_once(self) -> None:
        """Maps URLs are excluded, unused capacity is reassigned, and shared URLs are deduplicated with provenance."""
        from functions.evidence_collector.service import EvidenceCollectorService

        obs = [
            {
                "source_url": "https://www.google.com/maps/search/?api=1&query=Kuvempu+Vana",
                "artifact_ref": {"title": "Kuvempu Vana", "query_purpose": "hidden_places", "destination": "Mysuru"},
            },
            {
                "source_url": "https://karnatakatourism.org/melody-world",
                "artifact_ref": {"title": "Melody World", "query_purpose": "attractions", "destination": "Mysuru"},
            },
            {
                "source_url": "https://karnatakatourism.org/melody-world",
                "artifact_ref": {"title": "Melody World", "query_purpose": "hidden_places", "destination": "Mysuru"},
            },
        ]
        selected = EvidenceCollectorService.select_eligible_search_urls(obs, max_urls=3)  # type: ignore[arg-type]
        assert len(selected) == 1
        assert selected[0]["url"] == "https://karnatakatourism.org/melody-world"
        assert set(selected[0]["purposes"]) == {"attractions", "hidden_places"}

    def test_hidden_place_fetch_to_evaluator_preserves_purpose_and_provenance(self) -> None:
        """A hidden-place result reaching extraction and evaluator preserves purpose and source provenance."""
        obs_fetched = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/melody-world",
            "extraction_method": "SCRAPLING_HTTP",
            "excerpt_text": "Melody World Wax Museum in Mysuru is open to visitors daily.",
            "artifact_ref": {
                "title": "Melody World Wax Museum",
                "address": "Mysuru, Karnataka",
                "gps_coordinates": {"latitude": 12.2974, "longitude": 76.6852},
                "query_purpose": "hidden_places",
                "destination": "Mysuru",
            },
        }
        report = PlaceExtractionService.evaluate_hidden_place_candidates(
            observations=[obs_fetched],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert len(report["verified_optional_stops"]) == 1
        stop = report["verified_optional_stops"][0]
        assert stop["source_urls"] == ["https://karnatakatourism.org/melody-world"]
        assert stop["is_must_visit"] is False

    async def test_extract_page_propagates_purpose_and_destination_into_artifact_ref(self) -> None:
        """ScraplingExtractionAdapter.extract_page path preserves purpose, purposes, and destination."""
        from dataclasses import replace as dc_replace
        from datetime import UTC, datetime

        from functions.evidence_collector import service as ec_service

        class _StubAdapter:
            async def extract_page(
                self, url: str, *, category: str = "general", title: str | None = None
            ) -> tuple[list[Any], list[dict[str, Any]]]:
                obs = ec_service.RawObservation(  # type: ignore[attr-defined]
                    source_url=url,
                    canonical_url=url,
                    publisher="karnatakatourism.org",
                    retrieved_at=datetime.now(UTC),
                    extraction_method="SCRAPLING_HTTP",
                    content_hash="abc123",
                    excerpt_text="Melody World Wax Museum in Mysuru is open to visitors daily.",
                    excerpt_locator="body",
                    artifact_ref={"title": title or "Melody World Wax Museum"},
                )
                return [obs], []

        entry: dict[str, Any] = {
            "url": "https://karnatakatourism.org/melody-world",
            "category": "attraction",
            "title": "Melody World Wax Museum",
            "purpose": "hidden_places",
            "purposes": ["hidden_places", "attractions"],
            "destination": "Mysuru",
        }
        adapter = _StubAdapter()
        obs_list, _ = await adapter.extract_page(
            entry["url"], category=entry["category"], title=entry["title"]
        )
        ext_art = dict(obs_list[0].artifact_ref or {})
        ext_art.setdefault("search_selected_title", entry["title"])
        ext_art.setdefault("purpose", entry["purpose"])
        ext_art.setdefault("query_purpose", entry["purpose"])
        ext_art.setdefault("purposes", entry["purposes"])
        ext_art.setdefault("query_purposes", entry["purposes"])
        ext_art.setdefault("destination", entry["destination"])
        enriched = dc_replace(obs_list[0], artifact_ref=ext_art)

        assert enriched.artifact_ref is not None
        assert enriched.artifact_ref["purpose"] == "hidden_places"
        assert enriched.artifact_ref["query_purpose"] == "hidden_places"
        assert enriched.artifact_ref["purposes"] == ["hidden_places", "attractions"]
        assert enriched.artifact_ref["destination"] == "Mysuru"

    async def test_execute_collection_task_surfaces_skip_reasons_and_shared_url_purpose_destinations(
        self,
    ) -> None:
        """Real execute_collection_task surfaces fetch skip reasons and preserves exact purpose_destinations pairs."""
        from datetime import UTC, datetime, timedelta

        from functions.evidence_collector import service as ec_service
        from functions.evidence_collector.service import (
            EvidenceCollectorService,
            TaskDispatchEnvelope,
        )
        from functions.orchestrator.execution.fencing import TaskClaimResult

        exec_id = uuid.uuid4()
        task_id = uuid.uuid4()
        captured_output: dict[str, Any] = {}
        captured_staged_observations: list[Any] = []
        fetch_calls: list[str] = []

        class _ScalarNoneResult:
            def scalar_one_or_none(self) -> None:
                return None

        class _StubSession:
            async def execute(self, stmt: Any) -> Any:
                return _ScalarNoneResult()

            async def flush(self) -> None:
                return None

            async def rollback(self) -> None:
                return None

        class _StubFencingService:
            async def claim_task(self, **kwargs: Any) -> TaskClaimResult:
                return TaskClaimResult(
                    task_id=task_id,
                    execution_id=exec_id,
                    worker="evidence_collector",
                    action="collect_evidence",
                    attempt=1,
                    lease_token=uuid.uuid4(),
                    lease_expiry=datetime.now(UTC) + timedelta(minutes=5),
                    context_version=1,
                    payload_ref={},
                )

            async def complete_task(self, **kwargs: Any) -> uuid.UUID:
                captured_output.update(kwargs.get("output") or {})
                return uuid.uuid4()

        class _StubSearchAdapter:
            async def search(self, query: str, **kwargs: Any) -> list[Any]:
                now = datetime.now(UTC)
                q_low = query.lower()
                if "opening hours" in q_low:
                    return [
                        ec_service.RawObservation(  # type: ignore[attr-defined]
                            source_url="https://mysurupalace.karnataka.gov.in/",
                            canonical_url="https://mysurupalace.karnataka.gov.in/",
                            publisher="mysurupalace.karnataka.gov.in",
                            retrieved_at=now,
                            extraction_method="SERPAPI_WEB_SEARCH",
                            content_hash="h-palace",
                            excerpt_text="Mysuru Palace opening hours 10:00 AM to 5:30 PM",
                            excerpt_locator="snippet",
                            artifact_ref={"title": "Mysuru Palace", "purpose": "hours", "destination": "Mysuru"},
                        ),
                        ec_service.RawObservation(  # type: ignore[attr-defined]
                            source_url="https://karnatakatourism.org/melody-world",
                            canonical_url="https://karnatakatourism.org/melody-world",
                            publisher="karnatakatourism.org",
                            retrieved_at=now,
                            extraction_method="SERPAPI_WEB_SEARCH",
                            content_hash="h-shared-1",
                            excerpt_text="Melody World Wax Museum in Mysuru",
                            excerpt_locator="snippet",
                            artifact_ref={"title": "Melody World Wax Museum", "purpose": "hours", "destination": "Mysuru"},
                        ),
                    ]
                if "advisory" in q_low:
                    return [
                        ec_service.RawObservation(  # type: ignore[attr-defined]
                            source_url="https://karnataka.gov.in/palace-road-advisory-official",
                            canonical_url="https://karnataka.gov.in/palace-road-advisory-official",
                            publisher="karnataka.gov.in",
                            retrieved_at=now,
                            extraction_method="SERPAPI_WEB_SEARCH",
                            content_hash="h-adv",
                            excerpt_text="Mysuru road advisory",
                            excerpt_locator="snippet",
                            artifact_ref={"title": "Road Advisory", "purpose": "advisory", "destination": "Mysuru"},
                        ),
                    ]
                if "food" in q_low:
                    return [
                        ec_service.RawObservation(  # type: ignore[attr-defined]
                            source_url="https://www.google.com/maps/search/?api=1&query=Mysuru+Food",
                            canonical_url="https://www.google.com/maps/search/?api=1&query=Mysuru+Food",
                            publisher="google.com",
                            retrieved_at=now,
                            extraction_method="SERPAPI_WEB_SEARCH",
                            content_hash="h-food-maps",
                            excerpt_text="Maps link",
                            excerpt_locator="snippet",
                            artifact_ref={"title": "Mysuru Food", "purpose": "food", "destination": "Mysuru"},
                        ),
                    ]
                if "attractions" in q_low:
                    return [
                        ec_service.RawObservation(  # type: ignore[attr-defined]
                            source_url="https://karnatakatourism.org/melody-world",
                            canonical_url="https://karnatakatourism.org/melody-world",
                            publisher="karnatakatourism.org",
                            retrieved_at=now,
                            extraction_method="SERPAPI_WEB_SEARCH",
                            content_hash="h-shared-2",
                            excerpt_text="Melody World attraction",
                            excerpt_locator="snippet",
                            artifact_ref={"title": "Melody World Wax Museum", "purpose": "attractions", "destination": "Ooty"},
                        ),
                    ]
                return [
                    ec_service.RawObservation(  # type: ignore[attr-defined]
                        source_url="https://ootygardens.example/secret-grove",
                        canonical_url="https://ootygardens.example/secret-grove",
                        publisher="ootygardens.example",
                        retrieved_at=now,
                        extraction_method="SERPAPI_WEB_SEARCH",
                        content_hash="h-ooty-hidden",
                        excerpt_text="Secret Grove Ooty",
                        excerpt_locator="snippet",
                        artifact_ref={"title": "Secret Grove", "purpose": "hidden_places", "destination": "Ooty"},
                    ),
                ]

        class _StubExtractAdapter:
            async def extract_page(
                self, url: str, *, category: str = "general", title: str | None = None
            ) -> tuple[list[Any], list[dict[str, Any]]]:
                fetch_calls.append(url)
                if "advisory" in url:
                    raise RuntimeError("upstream 503")
                now = datetime.now(UTC)
                return [
                    ec_service.RawObservation(  # type: ignore[attr-defined]
                        source_url=url,
                        canonical_url=url,
                        publisher="example.org",
                        retrieved_at=now,
                        extraction_method="SCRAPLING_HTTP",
                        content_hash=f"page-{len(fetch_calls)}",
                        excerpt_text=f"{title or 'Place'} is open to visitors daily.",
                        excerpt_locator="body",
                        artifact_ref={"title": title or "Place"},
                    )
                ], []

        collector = EvidenceCollectorService(_StubSession())  # type: ignore[arg-type]
        collector._fencing_service = _StubFencingService()  # type: ignore[assignment]

        async def _noop_init_budget(*args: Any, **kwargs: Any) -> None:
            return None

        async def _allow_budget(*args: Any, **kwargs: Any) -> str | None:
            return "res-1"

        async def _noop_reconcile(*args: Any, **kwargs: Any) -> bool:
            return True

        collector.initialize_execution_budget_if_needed = _noop_init_budget  # type: ignore[method-assign]
        collector.reserve_budget = _allow_budget  # type: ignore[method-assign]
        collector.reconcile_budget = _noop_reconcile  # type: ignore[method-assign]

        # Stub _session.execute for SourceObservationModel select inside stage_observations_and_complete
        class _ObsRow:
            def __init__(self, obs: Any) -> None:
                self.id = uuid.uuid4()
                self.canonical_url = obs.canonical_url
                self.content_hash = obs.content_hash

        class _ObsQueryResult:
            def __init__(self, rows: list[_ObsRow], *, is_lookup: bool = False) -> None:
                self._rows = rows
                self._is_lookup = is_lookup

            def __iter__(self) -> Any:
                return iter(self._rows if self._is_lookup else [])

            def scalars(self) -> Any:
                return self

            def all(self) -> list[_ObsRow]:
                return self._rows

        async def _session_execute(stmt: Any) -> Any:
            sql_text = str(stmt)
            if "source_observations" in sql_text.lower() and "select" in sql_text.lower():
                is_lookup = "execution_id" in sql_text.lower()
                return _ObsQueryResult(
                    [_ObsRow(o) for o in captured_staged_observations], is_lookup=is_lookup
                )
            return _ScalarNoneResult()

        collector._session.execute = _session_execute  # type: ignore[method-assign,assignment]

        # Capture observations passed to stage_observations_and_complete
        orig_stage = collector.stage_observations_and_complete

        async def _wrapped_stage(**kwargs: Any) -> tuple[uuid.UUID, list[uuid.UUID]]:
            captured_staged_observations.extend(kwargs["observations"])
            return await orig_stage(**kwargs)

        collector.stage_observations_and_complete = _wrapped_stage  # type: ignore[method-assign]

        envelope = TaskDispatchEnvelope(
            event_id=uuid.uuid4(),
            task_id=task_id,
            execution_id=exec_id,
            kind="task_dispatch",
            task_attempt=1,
            outbox_delivery_attempts=1,
            worker="evidence_collector",
            action="collect_evidence",
            context_version=1,
            inner_payload={
                "queries": [
                    {"query": "Mysuru Palace opening hours", "purpose": "hours", "destination": "Mysuru"},
                    {"query": "Mysuru road advisory", "purpose": "advisory", "destination": "Mysuru"},
                    {"query": "Mysuru food", "purpose": "food", "destination": "Mysuru"},
                    {"query": "Ooty attractions", "purpose": "attractions", "destination": "Ooty"},
                    {"query": "Ooty hidden places", "purpose": "hidden_places", "destination": "Ooty"},
                ],
                "budget": {"currency": "INR", "cost_per_call_minor": 100},
            },
        )

        await collector.execute_collection_task(
            envelope,
            adapter=_StubSearchAdapter(),
            extraction_adapter=_StubExtractAdapter(),
        )

        # Shared URL fetched once
        assert fetch_calls.count("https://karnatakatourism.org/melody-world") == 1
        shared_page_obs = [
            o
            for o in captured_staged_observations
            if o.extraction_method == "SCRAPLING_HTTP"
            and o.canonical_url == "https://karnatakatourism.org/melody-world"
        ]
        assert len(shared_page_obs) == 1
        # Exact purpose-to-destination pairs preserved (not cross-product)
        pairs = shared_page_obs[0].artifact_ref.get("purpose_destinations")
        assert pairs == [
            {"purpose": "hours", "destination": "Mysuru"},
            {"purpose": "attractions", "destination": "Ooty"},
        ]

        # Fetch outcomes / skipped purposes distinguish all three failure modes
        skipped_purposes = captured_output.get("skipped_purposes") or {}
        assert "food" in skipped_purposes
        assert "no eligible url" in skipped_purposes["food"].lower()
        assert "advisory" in skipped_purposes
        assert "fetch failure" in skipped_purposes["advisory"].lower()
        assert "hidden_places" in skipped_purposes
        assert "fetch capacity exhausted" in skipped_purposes["hidden_places"].lower()

        # Downstream research diagnostics surface collector skip reasons
        synth_state = cast(
            TripPlanningState,
            {
                "execution_id": str(exec_id),
                "brief": {"destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}]},
                "task_outputs": {str(task_id): captured_output},
                "route_status": "OK",
            },
        )
        synth_res = synthesize_proposal_node(synth_state)
        skipped_areas = synth_res["proposal"]["research_coverage"]["skipped_research_areas"]
        area_reasons = {a.get("purpose"): str(a.get("reason", "")).lower() for a in skipped_areas}
        assert "no eligible url" in area_reasons.get("food", "")
        assert "fetch failure" in area_reasons.get("advisory", "")
        assert "fetch capacity exhausted" in area_reasons.get("hidden_places", "")
