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
