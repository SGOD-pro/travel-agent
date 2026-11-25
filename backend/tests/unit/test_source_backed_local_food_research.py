"""Unit tests for source-backed local food research conforming to spec 0007.

Requirements:
1. Dish-as-venue: Regional dishes without physical venue keywords are rejected as place candidates.
2. Wrong-city venue matches: Venues located in conflicting cities are excluded.
3. Unsupported dish-to-venue claims: Venues and dishes are paired only when attributable source content supports it.
4. Exhausted research budgets: Explicit covered and uncovered destinations and reasons when budget is exhausted.
5. Unsupported prices, hours, and dietary claims kept strictly unknown.
6. Food suggestions remain optional (is_must_visit = False) and never displace traveler must-visits.
7. Retain useful food findings when routing is unavailable.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from contracts.candidate import (
    CandidateHoursStatus,
)
from contracts.trips import DestinationPoint, TripBrief
from functions.orchestrator.evidence.place_extraction import (
    PlaceExtractionService,
)
from functions.orchestrator.evidence.query_planner import (
    QueryPlanningService,
)
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    _build_food_research,
    route_evidence_decision_node,
    synthesize_proposal_node,
)


class TestSourceBackedLocalFoodResearch:
    """Verifies source-backed local food research behaviors and regressions."""

    def test_dish_as_venue_rejected_from_place_candidates(self) -> None:
        """Culinary dish names without venue indicators must never be extracted as place candidates."""
        # 1. Direct dish classification check
        assert PlaceExtractionService.is_culinary_dish("Mysore Pak") is True
        assert PlaceExtractionService.is_culinary_dish("Mysuru Pak") is True
        assert PlaceExtractionService.is_culinary_dish("Bisi Bele Bath") is True
        assert PlaceExtractionService.is_culinary_dish("Masala Dosa") is True
        assert PlaceExtractionService.is_culinary_dish("Thatte Idli") is True

        # Venues serving dishes must NOT be rejected
        assert PlaceExtractionService.is_culinary_dish("Guru Sweet Mart") is False
        assert PlaceExtractionService.is_culinary_dish("Mylari Hotel") is False
        assert PlaceExtractionService.is_culinary_dish("Vidyarthi Bhavan") is False
        assert PlaceExtractionService.is_culinary_dish("CTR Shri Sagar Restaurant") is False

        # 2. Extraction from observations rejects dish-as-venue
        obs_dish = {
            "id": str(uuid.uuid4()),
            "publisher": "google_maps",
            "source_url": "https://maps.google.com/?cid=111",
            "excerpt_text": "Mysore Pak is a rich traditional sweet made with chickpea flour and ghee.",
            "artifact_ref": {
                "title": "Mysore Pak",
                "gps_coordinates": {"latitude": 12.3051, "longitude": 76.6551},
                "address": "Devaraja Mohalla, Mysuru",
            },
        }
        obs_venue = {
            "id": str(uuid.uuid4()),
            "publisher": "google_maps",
            "source_url": "https://maps.google.com/?cid=222",
            "excerpt_text": "Guru Sweet Mart is famous for authentic Mysore Pak prepared with pure ghee.",
            "artifact_ref": {
                "title": "Guru Sweet Mart",
                "gps_coordinates": {"latitude": 12.3080, "longitude": 76.6520},
                "address": "Sayyaji Rao Rd, Devaraja Mohalla, Mysuru, Karnataka",
            },
        }

        candidates = PlaceExtractionService.extract_candidates_from_observations([obs_dish, obs_venue])
        cand_names = [c.name for c in candidates]
        assert "Mysore Pak" not in cand_names
        assert "Guru Sweet Mart" in cand_names

        # 3. _build_food_research skips dish candidates
        state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {
                    "primary_name": "Mysore Pak",
                    "category": "restaurant",
                    "point": {"lat": 12.3051, "lng": 76.6551},
                    "existence_status": "VERIFIED",
                },
                {
                    "primary_name": "Guru Sweet Mart",
                    "category": "restaurant",
                    "point": {"lat": 12.3080, "lng": 76.6520},
                    "existence_status": "VERIFIED",
                },
            ],
            "observations": [obs_venue],
            "brief": {"destinations": [{"name": "Mysuru"}]},
        }
        food_res = _build_food_research(cast(TripPlanningState, state))
        rec_names = [d["name"] for d in food_res["dining_recommendations"]]
        assert "Mysore Pak" not in rec_names
        assert "Guru Sweet Mart" in rec_names

    def test_wrong_city_venue_matches_rejected(self) -> None:
        """Venues with explicit addresses or titles in conflicting cities must be filtered out."""
        obs_kolkata = {
            "id": str(uuid.uuid4()),
            "publisher": "google_maps",
            "source_url": "https://maps.google.com/?cid=333",
            "excerpt_text": "Arsalan serves legendary Kolkata Biryani at Park Circus.",
            "artifact_ref": {
                "title": "Arsalan Restaurant",
                "gps_coordinates": {"latitude": 22.5411, "longitude": 88.3639},
                "address": "119 Park St, Park Circus, Kolkata, West Bengal",
            },
        }
        obs_mysuru = {
            "id": str(uuid.uuid4()),
            "publisher": "google_maps",
            "source_url": "https://maps.google.com/?cid=444",
            "excerpt_text": "Hotel Mylari is a legendary eatery in Mysuru famous for breakfast dosa.",
            "artifact_ref": {
                "title": "Hotel Mylari",
                "gps_coordinates": {"latitude": 12.3100, "longitude": 76.6600},
                "address": "Nazarbad, Mysuru, Karnataka",
            },
        }

        # Candidate extraction filters out wrong-city places when expected_cities is given
        candidates = PlaceExtractionService.extract_candidates_from_observations(
            [obs_kolkata, obs_mysuru],
            expected_cities={"Mysuru", "Mysore"},
        )
        cand_names = [c.name for c in candidates]
        assert "Arsalan Restaurant" not in cand_names
        assert "Hotel Mylari" in cand_names

        # Food research filters out wrong-city candidates
        state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {
                    "primary_name": "Arsalan Restaurant",
                    "category": "restaurant",
                    "address": "Park Circus, Kolkata, West Bengal",
                    "diagnostics": {"address": "Park Circus, Kolkata, West Bengal"},
                    "point": {"lat": 22.5411, "lng": 88.3639},
                    "existence_status": "VERIFIED",
                },
                {
                    "primary_name": "Hotel Mylari",
                    "category": "restaurant",
                    "address": "Nazarbad, Mysuru, Karnataka",
                    "diagnostics": {"address": "Nazarbad, Mysuru, Karnataka"},
                    "point": {"lat": 12.3100, "lng": 76.6600},
                    "existence_status": "VERIFIED",
                },
            ],
            "observations": [obs_mysuru],
            "brief": {"destinations": [{"name": "Mysuru"}]},
        }
        food_res = _build_food_research(cast(TripPlanningState, state))
        rec_names = [d["name"] for d in food_res["dining_recommendations"]]
        assert "Arsalan Restaurant" not in rec_names
        assert "Hotel Mylari" in rec_names

    def test_unsupported_dish_to_venue_claims_kept_unpaired(self) -> None:
        """A venue is paired with a dish only when attributable source content supports that pairing."""
        obs_guru = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/mysuru-guru-sweets",
            "excerpt_text": "Guru Sweet Mart in Sayyaji Rao Road is the birthplace of Mysore Pak.",
            "artifact_ref": {
                "title": "Guru Sweet Mart",
                "address": "Sayyaji Rao Rd, Devaraja Mohalla, Mysuru",
            },
        }
        obs_mylari = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/mysuru-mylari",
            "excerpt_text": "Original Hotel Mylari in Nazarbad is renowned for soft Mysore Masala Dosa.",
            "artifact_ref": {
                "title": "Hotel Mylari",
                "address": "Nazarbad, Mysuru",
            },
        }

        venues = [
            {"name": "Guru Sweet Mart", "source_urls": ["https://karnatakatourism.org/mysuru-guru-sweets"]},
            {"name": "Hotel Mylari", "source_urls": ["https://karnatakatourism.org/mysuru-mylari"]},
            {"name": "Unknown Cafe", "source_urls": ["https://example.com/other"]},
        ]
        dishes = [
            {"name": "Mysore Pak", "dish_type": "sweet"},
            {"name": "Mysore Masala Dosa", "dish_type": "breakfast"},
            {"name": "Bisi Bele Bath", "dish_type": "main_course"},
        ]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_guru, obs_mylari],
            expected_cities={"Mysuru", "Mysore"},
        )

        guru = next(v for v in venues if v["name"] == "Guru Sweet Mart")
        mylari = next(v for v in venues if v["name"] == "Hotel Mylari")
        unknown = next(v for v in venues if v["name"] == "Unknown Cafe")

        # Guru Sweet Mart is paired with Mysore Pak, but NOT Mysore Masala Dosa or Bisi Bele Bath
        assert "Mysore Pak" in guru["serves_dishes"]
        assert "Mysore Masala Dosa" not in guru["serves_dishes"]
        assert "Bisi Bele Bath" not in guru["serves_dishes"]

        # Hotel Mylari is paired with Mysore Masala Dosa, but NOT Mysore Pak or Bisi Bele Bath
        assert "Mysore Masala Dosa" in mylari["serves_dishes"]
        assert "Mysore Pak" not in mylari["serves_dishes"]
        assert "Bisi Bele Bath" not in mylari["serves_dishes"]

        # Unknown Cafe has no supporting observations and remains empty
        assert len(unknown["serves_dishes"]) == 0

        # Dish paired_venues check
        mysore_pak = next(d for d in dishes if d["name"] == "Mysore Pak")
        masala_dosa = next(d for d in dishes if d["name"] == "Mysore Masala Dosa")
        bisi_bele_bath = next(d for d in dishes if d["name"] == "Bisi Bele Bath")

        assert "Guru Sweet Mart" in mysore_pak["paired_venues"]
        assert "Hotel Mylari" not in mysore_pak["paired_venues"]

        assert "Hotel Mylari" in masala_dosa["paired_venues"]
        assert "Guru Sweet Mart" not in masala_dosa["paired_venues"]

        # Bisi Bele Bath has no source pairing and must remain strictly empty
        assert len(bisi_bele_bath["paired_venues"]) == 0

    def test_snippet_only_dish_to_venue_claims_rejected(self) -> None:
        """Snippet-only search observations must not pair a dish to a venue; fetched page content is required."""
        obs_snippet = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/snippet-only",
            "extraction_method": "SERPAPI_SEARCH",
            "excerpt_text": "Guru Sweet Mart in Mysuru serves Mysore Pak.",
            "artifact_ref": {
                "title": "Guru Sweet Mart",
                "address": "Sayyaji Rao Rd, Mysuru",
            },
        }
        venues = [{"name": "Guru Sweet Mart", "source_urls": ["https://example.com/snippet-only"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_snippet],
            expected_cities={"Mysuru", "Mysore"},
        )

        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_exhausted_research_budgets_reported_explicitly(self) -> None:
        """When destination count exceeds query ceiling, explicit uncovered destinations and reasons are reported."""
        brief = TripBrief(
            origin_name="Bengaluru",
            origin_lat=12.9716,
            origin_lng=77.5946,
            destinations=[
                DestinationPoint(name="Mysuru", latitude=12.2958, longitude=76.6394),
                DestinationPoint(name="Ooty", latitude=11.4102, longitude=76.6950),
                DestinationPoint(name="Madikeri", latitude=12.4244, longitude=75.7382),
                DestinationPoint(name="Hassan", latitude=13.0033, longitude=76.1004),
                DestinationPoint(name="Chikmagalur", latitude=13.3153, longitude=75.7754),
            ],
            start_date="2026-12-01",
            end_date="2026-12-05",
        )

        planned_list = QueryPlanningService.plan_food_queries(brief, max_queries=4)
        assert len(planned_list) == 4
        assert planned_list.covered_destinations == ["Mysuru", "Ooty", "Madikeri", "Hassan"]
        assert planned_list.uncovered_destinations == ["Chikmagalur"]
        assert planned_list.coverage_reason is not None
        assert "Chikmagalur" in planned_list.coverage_reason
        assert "ceiling (4)" in planned_list.coverage_reason

        # In _build_food_research, partial coverage reports status PARTIAL and diagnostic reason
        state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {
                    "primary_name": "Guru Sweet Mart",
                    "category": "restaurant",
                    "existence_status": "VERIFIED",
                }
            ],
            "observations": [
                {
                    "source_url": "https://example.com/mysuru",
                    "excerpt_text": "Mysuru is famous for Mysore Pak sweet.",
                }
            ],
            "brief": brief,
            "covered_destinations": planned_list.covered_destinations,
            "uncovered_destinations": planned_list.uncovered_destinations,
            "coverage_reason": planned_list.coverage_reason,
        }
        food_res = _build_food_research(cast(TripPlanningState, state))
        assert food_res["status"] == "PARTIAL"
        assert food_res["uncovered_destinations"] == ["Chikmagalur"]
        assert "Chikmagalur" in str(food_res["reason"])

    def test_unsupported_prices_hours_dietary_claims_kept_unknown(self) -> None:
        """Dining recommendations default unsupported pricing, hours, and dietary claims to None/unknown."""
        state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {
                    "primary_name": "Guru Sweet Mart",
                    "category": "restaurant",
                    "existence_status": "VERIFIED",
                    "provenance": {"source_urls": ["https://example.com/guru"]},
                }
            ],
            "observations": [
                {
                    "source_url": "https://example.com/guru",
                    "excerpt_text": "Guru Sweet Mart serves Mysore Pak.",
                }
            ],
            "brief": {"destinations": [{"name": "Mysuru"}]},
        }
        food_res = _build_food_research(cast(TripPlanningState, state))
        assert len(food_res["dining_recommendations"]) == 1
        rec = food_res["dining_recommendations"][0]

        assert rec["price_estimate_inr"] is None
        assert rec["opening_hours"] is None
        assert rec["dietary_claims"] is None
        assert "pricing: UNKNOWN" in rec["unavailable_details"]
        assert "opening_hours: UNKNOWN" in rec["unavailable_details"]
        assert "dietary_claims: UNKNOWN" in rec["unavailable_details"]

    def test_food_suggestions_optional_and_must_visit_preserved(self) -> None:
        """Dining items must have is_must_visit = False and must never displace traveler must-visits."""
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
            "editorial_recommendations": [
                {
                    "primary_name": "Guru Sweet Mart",
                    "category": "restaurant",
                    "existence_status": "VERIFIED",
                }
            ],
            "observations": [
                {
                    "source_url": "https://example.com/guru",
                    "excerpt_text": "Guru Sweet Mart serves Mysore Pak sweet.",
                }
            ],
            "brief": {
                "destinations": [{"name": "Mysuru"}],
                "must_visit_places": ["Mysuru Palace"],
            },
            "routing_status": "ROUTED",
            "route_id": "route-123",
        }

        food_res = _build_food_research(cast(TripPlanningState, state))
        rec = food_res["dining_recommendations"][0]
        assert rec["is_must_visit"] is False

        prop_out = synthesize_proposal_node(cast(TripPlanningState, state))
        proposal = prop_out["proposal"]

        # Traveler must-visit is preserved and marked is_must_visit=True
        attractions = proposal["attractions_research"]
        palace = next(a for a in attractions if a["name"] == "Mysuru Palace")
        assert palace["is_must_visit"] is True

        # Food recommendations remain in food_research, not converted to mandatory route stops
        assert len(proposal["food_research"]["dining_recommendations"]) == 1
        assert proposal["food_research"]["dining_recommendations"][0]["is_must_visit"] is False

    def test_food_research_retained_when_routing_unavailable(self) -> None:
        """When routing is unavailable, proposal preserves food research findings honestly."""
        state: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {
                    "primary_name": "Guru Sweet Mart",
                    "category": "restaurant",
                    "existence_status": "VERIFIED",
                }
            ],
            "observations": [
                {
                    "source_url": "https://example.com/guru",
                    "excerpt_text": "Guru Sweet Mart serves authentic Mysore Pak.",
                }
            ],
            "brief": {"destinations": [{"name": "Mysuru"}]},
            "routing_status": "ROUTING_UNAVAILABLE",
            "route_id": None,
        }

        prop_out = synthesize_proposal_node(cast(TripPlanningState, state))
        proposal = prop_out["proposal"]

        assert proposal["status"] == "partial"
        assert proposal["routing_status"] == "ROUTING_UNAVAILABLE"
        assert proposal["food_research"] is not None
        assert len(proposal["food_research"]["local_dishes"]) > 0
        assert len(proposal["food_research"]["dining_recommendations"]) > 0
        assert proposal["food_research"]["dining_recommendations"][0]["name"] == "Guru Sweet Mart"

    async def test_food_intent_from_trip_brief_dispatches_food_queries_without_state_override(self) -> None:
        """A food request entering through TripBrief dispatches food queries without setting research_focus."""
        state: dict[str, Any] = {
            "execution_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
                "traveler_preferences": ["authentic local food", "street food"],
                "must_visit_places": [],
            },
            "_offline_fixture_mode": True,
        }
        res = await route_evidence_decision_node(cast(TripPlanningState, state))
        tasks = res.get("pending_tasks", [])
        assert len(tasks) == 1
        payload = tasks[0]["payload"]
        assert payload["categories"] == ["dining", "food"]
        assert any(q.get("purpose") == "dining" for q in payload["queries"])

    async def test_mixed_request_preserves_must_visit_and_hours_with_food_slot(self) -> None:
        """A mixed request with must_visit_places and food preferences preserves hours queries and adds food."""
        state: dict[str, Any] = {
            "execution_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
                "traveler_preferences": ["heritage architecture", "authentic local cuisine"],
                "must_visit_places": ["Mysuru Palace"],
            },
            "_offline_fixture_mode": True,
        }
        res = await route_evidence_decision_node(cast(TripPlanningState, state))
        tasks = res.get("pending_tasks", [])
        assert len(tasks) == 1
        queries = tasks[0]["payload"]["queries"]
        assert any(q.get("purpose") == "hours" for q in queries)
        assert any(q.get("purpose") == "dining" for q in queries)

    def test_unrelated_mentions_and_different_venue_on_same_page_rejected(self) -> None:
        """Names appearing on the same page or served by a different venue must not be falsely paired."""
        obs_page = {
            "id": str(uuid.uuid4()),
            "source_url": "https://karnatakatourism.org/mysuru-food-guide",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": (
                "Guru Sweet Mart is a famous shop in Mysuru, while Hotel Mylari serves Mysore Masala Dosa. "
                "Separately, Bisi Bele Bath is popular across Karnataka."
            ),
            "artifact_ref": {"title": "Mysuru Food Guide", "address": "Mysuru"},
        }
        venues = [
            {"name": "Guru Sweet Mart", "source_urls": ["https://karnatakatourism.org/mysuru-food-guide"]},
            {"name": "Hotel Mylari", "source_urls": ["https://karnatakatourism.org/mysuru-food-guide"]},
        ]
        dishes = [
            {"name": "Mysore Masala Dosa", "dish_type": "breakfast"},
            {"name": "Bisi Bele Bath", "dish_type": "main_course"},
        ]
        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_page],
            expected_cities={"Mysuru", "Mysore"},
        )
        guru = next(v for v in venues if v["name"] == "Guru Sweet Mart")
        mylari = next(v for v in venues if v["name"] == "Hotel Mylari")

        assert guru["serves_dishes"] == []
        assert mylari["serves_dishes"] == ["Mysore Masala Dosa"]
        pairings = cast(list[dict[str, str]], mylari.get("dish_pairings", []))
        assert len(pairings) == 1
        assert pairings[0]["source_url"] == "https://karnatakatourism.org/mysuru-food-guide"
        assert "Hotel Mylari serves Mysore Masala Dosa" in pairings[0]["evidence_excerpt"]

    def test_negated_dish_to_venue_claim_rejected(self) -> None:
        """Negated statements must not be treated as positive dish-to-venue pairings."""
        obs_neg = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/mysuru-review",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Guru Sweet Mart does not serve Mysore Masala Dosa.",
            "artifact_ref": {"title": "Mysuru Review", "address": "Mysuru"},
        }
        venues = [{"name": "Guru Sweet Mart", "source_urls": ["https://example.com/mysuru-review"]}]
        dishes = [{"name": "Mysore Masala Dosa", "dish_type": "breakfast"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_neg],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_url_or_metadata_only_match_rejected(self) -> None:
        """Matches appearing only in URL or search metadata without fetched page text must be rejected."""
        obs_meta = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/guru-sweet-mart-mysore-pak",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Welcome to our Mysuru travel guide.",
            "artifact_ref": {
                "title": "Guru Sweet Mart Mysore Pak",
                "snippet": "Guru Sweet Mart Mysore Pak",
                "address": "Mysuru",
            },
        }
        venues = [{"name": "Guru Sweet Mart", "source_urls": ["https://example.com/guru-sweet-mart-mysore-pak"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_meta],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_same_clause_cooccurrence_without_serving_relationship_rejected(self) -> None:
        """Same-clause co-occurrence without a serving or menu relationship must not pair dish to venue."""
        obs_non_serving = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/mysuru-blog",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "We bought Mysore Pak across the street after dinner at Gufha Restaurant.",
            "artifact_ref": {"title": "Mysuru Blog", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": ["https://example.com/mysuru-blog"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_non_serving],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_positive_venue_menu_item_scoped_to_venue_accepted(self) -> None:
        """A menu item scoped to the matched venue and location supports a dish-to-venue pairing."""
        obs_menu = {
            "id": str(uuid.uuid4()),
            "source_url": "https://gurusweetmart.in/menu",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Guru Sweet Mart Menu:\n- Mysore Pak (Pure Ghee)\n- Badam Halwa",
            "artifact_ref": {"title": "Guru Sweet Mart", "address": "Mysuru"},
        }
        venues = [{"name": "Guru Sweet Mart", "source_urls": ["https://gurusweetmart.in/menu"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_menu],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == ["Mysore Pak"]
        pairings = cast(list[dict[str, str]], venues[0].get("dish_pairings", []))
        assert len(pairings) == 1
        assert pairings[0]["source_url"] == "https://gurusweetmart.in/menu"
        assert "Mysore Pak" in pairings[0]["evidence_excerpt"]

    def test_different_subject_across_from_venue_rejected(self) -> None:
        """A serving statement whose subject is another shop across from the venue must remain unpaired."""
        obs = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/mysuru-street",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "The shop across from Gufha Restaurant serves Mysore Pak.",
            "artifact_ref": {"title": "Mysuru Street", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": ["https://example.com/mysuru-street"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_sold_out_status_notice_alone_rejected(self) -> None:
        """A sold-out status notice under a menu heading without a menu item listing must not pair."""
        obs = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/gufha-menu",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant Menu:\nMysore Pak is sold out today.",
            "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": ["https://example.com/gufha-menu"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_explicit_subject_serving_statement_accepted(self) -> None:
        """An explicit serving statement where the matched venue is the subject pairs with URL and excerpt."""
        obs = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/gufha",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant serves Mysore Pak.",
            "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": ["https://example.com/gufha"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == ["Mysore Pak"]
        pairings = cast(list[dict[str, Any]], venues[0].get("dish_pairings", []))
        assert len(pairings) == 1
        assert pairings[0]["source_url"] == "https://example.com/gufha"
        assert "Gufha Restaurant serves Mysore Pak" in pairings[0]["evidence_excerpt"]
        assert pairings[0].get("availability") == "UNKNOWN"

    def test_venue_menu_listing_accepted_with_unknown_availability(self) -> None:
        """A genuine menu item under the venue menu pairs with unknown availability."""
        obs = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/gufha-menu",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant Menu:\n- Mysore Pak",
            "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": ["https://example.com/gufha-menu"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == ["Mysore Pak"]
        pairings = cast(list[dict[str, Any]], venues[0].get("dish_pairings", []))
        assert len(pairings) == 1
        assert pairings[0]["source_url"] == "https://example.com/gufha-menu"
        assert "Mysore Pak" in pairings[0]["evidence_excerpt"]
        assert pairings[0].get("availability") == "UNKNOWN"

    def test_historical_serving_statement_and_unusable_url_rejected(self) -> None:
        """Historical serving claims and observations without a usable http(s) URL must remain unpaired."""
        obs_hist = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/gufha-history",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant used to serve Mysore Pak.",
            "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
        }
        obs_no_url = {
            "id": str(uuid.uuid4()),
            "source_url": "",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant serves Mysore Pak.",
            "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": []}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs_hist, obs_no_url],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == []
        assert len(dishes[0]["paired_venues"]) == 0

    def test_menu_listing_with_sold_out_notice_preserves_listing_without_in_stock(self) -> None:
        """When both a valid menu listing and a sold-out notice exist, preserve listing with sold-out availability."""
        obs = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/gufha-menu",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant Menu:\n- Mysore Pak\nMysore Pak is sold out today.",
            "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
        }
        venues = [{"name": "Gufha Restaurant", "source_urls": ["https://example.com/gufha-menu"]}]
        dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues,
            dishes=dishes,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues[0]["serves_dishes"] == ["Mysore Pak"]
        pairings = cast(list[dict[str, Any]], venues[0].get("dish_pairings", []))
        assert len(pairings) == 1
        assert pairings[0].get("availability") == "SOLD_OUT"
        assert pairings[0].get("in_stock") is not True

    def test_indirect_prose_and_menu_referral_rejected(self) -> None:
        """Indirect prose ('recommends a shop that serves', 'serves coffee with a view of') and menu referrals must remain unpaired."""
        cases = [
            "Gufha Restaurant recommends a shop that serves Mysore Pak.",
            "Gufha Restaurant in Mysuru recommends a shop that serves Mysore Pak.",
            "Gufha Restaurant in Mysuru does not serve Mysore Pak.",
            "Gufha Restaurant in Mysuru used to serve Mysore Pak.",
            "Gufha Restaurant serves coffee with a view of the Mysore Pak shop.",
            "Gufha Restaurant Menu:\n- Ask the shop next door for Mysore Pak",
            "Gufha Restaurant Menu:\n- Mysore Pak - Ask the shop next door",
        ]
        for idx, excerpt in enumerate(cases):
            obs = {
                "id": str(uuid.uuid4()),
                "source_url": f"https://example.com/case-{idx}",
                "extraction_method": "TRAFILATURA_TEXT",
                "excerpt_text": excerpt,
                "artifact_ref": {"title": "Gufha Restaurant", "address": "Mysuru"},
            }
            venues = [{"name": "Gufha Restaurant", "source_urls": [f"https://example.com/case-{idx}"]}]
            dishes = [{"name": "Mysore Pak", "dish_type": "sweet"}]

            PlaceExtractionService.pair_dishes_and_venues(
                venues=venues,
                dishes=dishes,
                observations=[obs],
                expected_cities={"Mysuru", "Mysore"},
            )
            assert venues[0]["serves_dishes"] == [], f"Failed for: {excerpt}"
            assert venues[0]["dish_pairings"] == [], f"Failed for: {excerpt}"
            assert len(dishes[0]["paired_venues"]) == 0, f"Failed for: {excerpt}"
            assert len(dishes[0]["venue_pairings"]) == 0, f"Failed for: {excerpt}"

    def test_menu_heading_owner_bound_to_menu_owner_not_parenthetical_reference(self) -> None:
        """Menu heading 'Other Cafe Menu (near Gufha Restaurant):' must bind to Other Cafe, never Gufha Restaurant."""
        obs = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/other-cafe-menu",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Other Cafe Menu (near Gufha Restaurant):\n- Mysore Pak",
            "artifact_ref": {"title": "Other Cafe", "address": "Mysuru"},
        }
        # 1. Both Other Cafe and Gufha Restaurant are candidates -> pairs only with Other Cafe
        venues_both = [
            {"name": "Gufha Restaurant", "source_urls": ["https://example.com/other-cafe-menu"]},
            {"name": "Other Cafe", "source_urls": ["https://example.com/other-cafe-menu"]},
        ]
        dishes_both = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues_both,
            dishes=dishes_both,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        gufha = next(v for v in venues_both if v["name"] == "Gufha Restaurant")
        other = next(v for v in venues_both if v["name"] == "Other Cafe")
        assert gufha["serves_dishes"] == []
        assert other["serves_dishes"] == ["Mysore Pak"]
        assert list(dishes_both[0]["paired_venues"]) == ["Other Cafe"]

        # 2. Only Gufha Restaurant is a candidate -> remains unpaired
        venues_only_gufha = [
            {"name": "Gufha Restaurant", "source_urls": ["https://example.com/other-cafe-menu"]},
        ]
        dishes_only_gufha = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(
            venues=venues_only_gufha,
            dishes=dishes_only_gufha,
            observations=[obs],
            expected_cities={"Mysuru", "Mysore"},
        )
        assert venues_only_gufha[0]["serves_dishes"] == []
        assert len(dishes_only_gufha[0]["paired_venues"]) == 0

    def test_menu_heading_ownership_r3_to_r6_and_c1_to_c4(self) -> None:
        """Verifies R3-R6 and C1-C4 menu-heading ownership rules."""
        # R3: Multi-owner heading ("Menu of Other Cafe and Gufha Restaurant:") -> neither pairs
        obs_r3 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/r3",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Menu of Other Cafe and Gufha Restaurant:\n- Mysore Pak",
            "artifact_ref": {"address": "Mysuru"},
        }
        v_r3: list[dict[str, Any]] = [{"name": "Other Cafe"}, {"name": "Gufha Restaurant"}]
        d_r3 = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_r3, d_r3, [obs_r3], expected_cities={"Mysuru"})
        assert v_r3[0]["serves_dishes"] == [] and v_r3[1]["serves_dishes"] == []

        # R4: Qualifier-only heading ("Menu recommended by Gufha Restaurant:") -> Gufha stays unpaired
        obs_r4 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/r4",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Menu recommended by Gufha Restaurant:\n- Mysore Pak",
            "artifact_ref": {"address": "Mysuru"},
        }
        v_r4: list[dict[str, Any]] = [{"name": "Gufha Restaurant"}]
        d_r4 = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_r4, d_r4, [obs_r4], expected_cities={"Mysuru"})
        assert v_r4[0]["serves_dishes"] == []

        # R5: Gufha's menu lists Masala Dosa, then unknown-owner menu recommends Gufha and lists Mysore Pak
        obs_r5 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/r5",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": (
                "Gufha Restaurant's Menu:\n- Masala Dosa\n"
                "Menu recommended by Gufha Restaurant:\n- Mysore Pak"
            ),
            "artifact_ref": {"address": "Mysuru"},
        }
        v_r5: list[dict[str, Any]] = [{"name": "Gufha Restaurant"}]
        d_r5 = [{"name": "Masala Dosa", "dish_type": "breakfast"}, {"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_r5, d_r5, [obs_r5], expected_cities={"Mysuru"})
        assert v_r5[0]["serves_dishes"] == ["Masala Dosa"]

        # R6: Gufha's menu lists Masala Dosa, then Other Cafe's menu lists Mysore Pak
        obs_r6 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/r6",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": (
                "Gufha Restaurant - Menu:\n- Masala Dosa\n"
                "Menu of Other Cafe:\n- Mysore Pak"
            ),
            "artifact_ref": {"address": "Mysuru"},
        }
        v_r6: list[dict[str, Any]] = [{"name": "Gufha Restaurant"}, {"name": "Other Cafe"}]
        d_r6 = [{"name": "Masala Dosa", "dish_type": "breakfast"}, {"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_r6, d_r6, [obs_r6], expected_cities={"Mysuru"})
        assert v_r6[0]["serves_dishes"] == ["Masala Dosa"]
        assert v_r6[1]["serves_dishes"] == ["Mysore Pak"]

        # C1: "Gufha Restaurant Menu:" and "Menu at Gufha Restaurant:" each list Mysore Pak
        for idx, heading in enumerate(["Gufha Restaurant Menu:", "Menu at Gufha Restaurant:"]):
            obs_c1 = {
                "id": str(uuid.uuid4()),
                "source_url": f"https://example.com/c1-{idx}",
                "extraction_method": "TRAFILATURA_TEXT",
                "excerpt_text": f"{heading}\n- Mysore Pak",
                "artifact_ref": {"address": "Mysuru"},
            }
            v_c1: list[dict[str, Any]] = [{"name": "Gufha Restaurant"}]
            d_c1 = [{"name": "Mysore Pak", "dish_type": "sweet"}]
            PlaceExtractionService.pair_dishes_and_venues(v_c1, d_c1, [obs_c1], expected_cities={"Mysuru"})
            assert v_c1[0]["serves_dishes"] == ["Mysore Pak"]
            p = cast(list[dict[str, Any]], v_c1[0]["dish_pairings"])[0]
            assert p["source_url"] == f"https://example.com/c1-{idx}"
            assert p["availability"] == "UNKNOWN"

        # C2: Conflicting city -> neither pairs
        obs_c2 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/c2",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Other Cafe Menu (near Gufha Restaurant):\n- Mysore Pak",
            "artifact_ref": {"address": "Kolkata, West Bengal"},
        }
        v_c2: list[dict[str, Any]] = [{"name": "Other Cafe"}, {"name": "Gufha Restaurant"}]
        d_c2 = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_c2, d_c2, [obs_c2], expected_cities={"Mysuru", "Mysore"})
        assert v_c2[0]["serves_dishes"] == [] and v_c2[1]["serves_dishes"] == []

        # C3: Other Cafe's menu lists "- Mysore Pak - sold out" -> SOLD_OUT, no in-stock claim
        obs_c3 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/c3",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Other Cafe Menu:\n- Mysore Pak - sold out",
            "artifact_ref": {"address": "Mysuru"},
        }
        v_c3: list[dict[str, Any]] = [{"name": "Other Cafe"}]
        d_c3 = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_c3, d_c3, [obs_c3], expected_cities={"Mysuru"})
        assert v_c3[0]["serves_dishes"] == ["Mysore Pak"]
        p_c3 = cast(list[dict[str, Any]], v_c3[0]["dish_pairings"])[0]
        assert p_c3["availability"] == "SOLD_OUT"
        assert p_c3.get("in_stock") is not True

        # C4: Two candidate names normalize to the same owner -> neither pairs
        obs_c4 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/c4",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Gufha Restaurant Menu:\n- Mysore Pak",
            "artifact_ref": {"address": "Mysuru"},
        }
        v_c4: list[dict[str, Any]] = [{"name": "Gufha Restaurant"}, {"name": "The Gufha Restaurant"}]
        d_c4 = [{"name": "Mysore Pak", "dish_type": "sweet"}]
        PlaceExtractionService.pair_dishes_and_venues(v_c4, d_c4, [obs_c4], expected_cities={"Mysuru"})
        assert v_c4[0]["serves_dishes"] == [] and v_c4[1]["serves_dishes"] == []

    def test_menu_heading_ownership_workflow_r1_and_c1(self) -> None:
        """Drives R1 and C1 through _build_food_research and synthesize_proposal_node."""
        obs_r1 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/r1-wf",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Other Cafe Menu (near Gufha Restaurant):\n- Mysore Pak",
            "artifact_ref": {"address": "Mysuru"},
        }
        state_r1: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {"primary_name": "Gufha Restaurant", "category": "restaurant", "existence_status": "VERIFIED"}
            ],
            "observations": [obs_r1],
            "brief": {"destinations": [{"name": "Mysuru"}], "traveler_preferences": ["authentic local food"]},
        }
        prop_r1 = synthesize_proposal_node(cast(TripPlanningState, state_r1))["proposal"]
        assert prop_r1["food_research"]["dining_recommendations"][0]["serves_dishes"] == []

        obs_c1 = {
            "id": str(uuid.uuid4()),
            "source_url": "https://example.com/c1-wf",
            "extraction_method": "TRAFILATURA_TEXT",
            "excerpt_text": "Menu at Gufha Restaurant:\n- Mysore Pak",
            "artifact_ref": {"address": "Mysuru"},
        }
        state_c1: dict[str, Any] = {
            "eligible_stops": [],
            "editorial_recommendations": [
                {"primary_name": "Gufha Restaurant", "category": "restaurant", "existence_status": "VERIFIED"}
            ],
            "observations": [obs_c1],
            "brief": {"destinations": [{"name": "Mysuru"}], "traveler_preferences": ["authentic local food"]},
        }
        prop_c1 = synthesize_proposal_node(cast(TripPlanningState, state_c1))["proposal"]
        assert prop_c1["food_research"]["dining_recommendations"][0]["serves_dishes"] == ["Mysore Pak"]
