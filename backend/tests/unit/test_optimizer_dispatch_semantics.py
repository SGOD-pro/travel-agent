"""Unit tests for bounded and semantically correct route optimizer dispatch.

Conforms to spec 0007 AC-3, AC-4, AC-9, and optimizer dispatch requirements:
1. Distinguishes requested anchors, optional sightseeing, dining choices,
   accommodation options, services and advisory observations.
2. Verified existence alone does not make a place mandatory.
3. Excess candidates are deterministically selected up to endpoint capacity,
   preserving remaining candidates with explicit deferral reasons.
4. Provider limits distinguish location limits from matrix cell limits,
   without hardcoding nine stops plus origin.
5. Multi day windows, local time, dwell duration and daily opening hours
   are preserved without collapsing multi day trips into Day 1.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from contracts.routing import RouteRequest, StopType
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    _classify_candidate_category,
    dispatch_route_optimizer_node,
)


class TestOptimizerDispatchSemantics:
    """Tests covering semantic categorization, mandatory status and provider limits."""

    def test_classify_candidate_category_helper(self) -> None:
        """Verifies semantic category classification across venue types."""
        assert _classify_candidate_category("hotel", "Grand Palace Hotel") == "accommodation"
        assert _classify_candidate_category("resort", "Coffee County Stay") == "accommodation"
        assert _classify_candidate_category("lodging", "City Guest House") == "accommodation"
        assert _classify_candidate_category("restaurant", "Mylari Dosa") == "dining"
        assert _classify_candidate_category("cafe", "Corner Coffee Cafe") == "dining"
        assert _classify_candidate_category("advisory", "Highway Pin") == "advisory"
        assert _classify_candidate_category("", "NH275 Expressway Advisory") == "advisory"
        assert _classify_candidate_category("service", "Indian Oil Fuel") == "service"
        assert _classify_candidate_category("attraction", "Mysore Palace") == "sightseeing"
        assert _classify_candidate_category("museum", "Rail Museum") == "sightseeing"

    def test_mixed_attractions_hotels_restaurants_and_advisories(self) -> None:
        """Verifies candidate partitioning where hotels, restaurants, services and advisories

        are deferred or excluded, while sightseeing attractions enter solver as optional stops.
        """
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "owner_id": str(uuid.uuid4()),
            "trip_id": str(uuid.uuid4()),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Mysuru",
                        "latitude": 12.2958,
                        "longitude": 76.6394,
                        "stay_days": 1,
                        "confidence": 1.0,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "15000.00", "currency": "INR"},
                "preferred_modes": ["car_petrol"],
            },
            "eligible_stops": [
                # Brief destination match (verified anchor)
                {
                    "primary_name": "Mysuru",
                    "category": "destination",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
                # Sightseeing attractions (should enter solver as optional)
                {
                    "primary_name": "Mysore Palace",
                    "category": "heritage",
                    "point": {"lat": 12.3051, "lng": 76.6551},
                    "suggested_stay_minutes": 90,
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "window_start": "10:00",
                    "window_end": "17:30",
                },
                {
                    "primary_name": "Chamundi Hills",
                    "category": "viewpoint",
                    "point": {"lat": 12.2724, "lng": 76.6705},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
                # Accommodation candidates (should be deferred to lodging recommendations)
                {
                    "primary_name": "Hotel Mysore Royale",
                    "category": "hotel",
                    "point": {"lat": 12.3100, "lng": 76.6500},
                    "existence_status": "VERIFIED",
                },
                {
                    "primary_name": "Rio Meridian Resort",
                    "category": "resort",
                    "point": {"lat": 12.3200, "lng": 76.6400},
                    "existence_status": "VERIFIED",
                },
                # Dining candidates (should be deferred to dining recommendations)
                {
                    "primary_name": "Hotel Original Mylari",
                    "category": "restaurant",
                    "point": {"lat": 12.3080, "lng": 76.6580},
                    "existence_status": "VERIFIED",
                },
                {
                    "primary_name": "Malgudi Cafe",
                    "category": "cafe",
                    "point": {"lat": 12.3090, "lng": 76.6590},
                    "existence_status": "VERIFIED",
                },
                # Advisory candidate (should be excluded from route stops)
                {
                    "primary_name": "Mysore Rd",
                    "category": "advisory",
                    "point": {"lat": 12.5000, "lng": 77.0000},
                    "existence_status": "VERIFIED",
                },
                # Service facility (should be deferred)
                {
                    "primary_name": "Highway Fuel Station",
                    "category": "gas_station",
                    "point": {"lat": 12.6000, "lng": 76.9000},
                    "existence_status": "VERIFIED",
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        scheduled_names = [s.name for s in route_req.stops]

        # Scheduled stops must include origin, brief anchor, and sightseeing attractions only
        assert scheduled_names == ["Bengaluru", "Mysuru", "Mysore Palace", "Chamundi Hills"]

        # Origin and brief destination are mandatory anchors
        origin_stop = route_req.stops[0]
        assert origin_stop.name == "Bengaluru"
        assert origin_stop.stop_type == StopType.ORIGIN
        assert origin_stop.is_must_visit is True
        assert origin_stop.dwell_seconds == 0

        anchor_stop = route_req.stops[1]
        assert anchor_stop.name == "Mysuru"
        assert anchor_stop.stop_type == StopType.DESTINATION
        assert anchor_stop.is_must_visit is True

        # Discovered attractions are optional recommendations
        palace_stop = route_req.stops[2]
        assert palace_stop.name == "Mysore Palace"
        assert palace_stop.is_must_visit is False
        assert palace_stop.dwell_seconds == 90 * 60

        chamundi_stop = route_req.stops[3]
        assert chamundi_stop.name == "Chamundi Hills"
        assert chamundi_stop.is_must_visit is False
        assert chamundi_stop.dwell_seconds == 60 * 60

        # Accommodations, dining and advisories must not be in solver stops
        assert "Hotel Mysore Royale" not in scheduled_names
        assert "Rio Meridian Resort" not in scheduled_names
        assert "Hotel Original Mylari" not in scheduled_names
        assert "Malgudi Cafe" not in scheduled_names
        assert "Mysore Rd" not in scheduled_names
        assert "Highway Fuel Station" not in scheduled_names

        # Verify editorial recommendations contain deferred entries with explicit reasons
        editorial = outcome["editorial_recommendations"]
        recs_by_name = {r["primary_name"]: r for r in editorial}

        assert "Hotel Mysore Royale" in recs_by_name
        assert "accommodation option preserved for lodging" in recs_by_name["Hotel Mysore Royale"]["diagnostics"]["deferral_reason"]

        assert "Hotel Original Mylari" in recs_by_name
        assert "dining choice preserved for dining" in recs_by_name["Hotel Original Mylari"]["diagnostics"]["deferral_reason"]

        assert "Mysore Rd" in recs_by_name
        assert "advisory observation preserved for route alerts" in recs_by_name["Mysore Rd"]["diagnostics"]["rejection_reason"]

        assert "Highway Fuel Station" in recs_by_name
        assert "service facility preserved" in recs_by_name["Highway Fuel Station"]["diagnostics"]["deferral_reason"]

    def test_requested_mandatory_anchors_vs_optional_recommendations(self) -> None:
        """Verifies that verified existence alone does not make a place mandatory."""
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Mysuru",
                        "latitude": 12.2958,
                        "longitude": 76.6394,
                        "stay_days": 1,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Mysuru",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                },
                {
                    "primary_name": "St Philomena Church",
                    "category": "heritage",
                    "point": {"lat": 12.3211, "lng": 76.6578},
                    "suggested_stay_minutes": 45,
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        assert len(route_req.stops) == 3
        origin = route_req.stops[0]
        assert origin.name == "Bengaluru"
        assert origin.is_must_visit is True

        anchor = route_req.stops[1]
        assert anchor.name == "Mysuru"
        assert anchor.is_must_visit is True

        discovered = route_req.stops[2]
        assert discovered.name == "St Philomena Church"
        assert discovered.is_must_visit is False
        assert discovered.dwell_seconds == 45 * 60

    def test_excess_candidates_deterministic_selection_and_defer_reasons(self) -> None:
        """Verifies capacity enforcement capping stops to provider limits and preserving

        excess candidates with explicit reasons.
        """
        exec_id = uuid.uuid4()
        # Provider allows 64 matrix cells -> isqrt(64) = 8 stops total
        # Origin (1) + Destination (1) = 2 mandatory stops -> 6 slots available for optional POIs
        candidates = [
            {
                "primary_name": f"Attraction {i}",
                "category": "heritage",
                "point": {"lat": 12.3000 + (i * 0.01), "lng": 76.6500 + (i * 0.01)},
                "suggested_stay_minutes": 40,
                "existence_status": "VERIFIED",
                "hours_status": "HOURS_ASSUMED",
            }
            for i in range(1, 11)  # 10 attractions total
        ]

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "max_locations": 100,
            "max_matrix_cells": 64,  # Cap is 8 stops
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Mysuru",
                        "latitude": 12.2958,
                        "longitude": 76.6394,
                        "stay_days": 1,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Mysuru",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                },
                *candidates,
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        # Total scheduled stops must equal 8 (1 origin + 1 anchor + 6 attractions)
        assert len(route_req.stops) == 8
        scheduled_attraction_names = [s.name for s in route_req.stops[2:]]
        assert scheduled_attraction_names == [f"Attraction {i}" for i in range(1, 7)]

        # Exactly 4 excess attractions must be deferred to editorial recommendations
        editorial = outcome["editorial_recommendations"]
        deferred_names = [r["primary_name"] for r in editorial]
        assert deferred_names == ["Attraction 7", "Attraction 8", "Attraction 9", "Attraction 10"]

        for r in editorial:
            diag = r["diagnostics"]["deferral_reason"]
            assert "candidate exceeds provider matrix cell capacity" in diag
            assert "capacity=8 stops" in diag
            assert "max cells=64" in diag

    def test_provider_limits_distinguish_locations_from_matrix_cells(self) -> None:
        """Verifies that location limits and matrix cell limits are distinguished cleanly,

        and neither hardcodes nine stops plus origin.
        """
        exec_id = uuid.uuid4()
        candidates = [
            {
                "primary_name": f"Heritage Site {i}",
                "category": "heritage",
                "point": {"lat": 12.3000 + (i * 0.01), "lng": 76.6500 + (i * 0.01)},
                "suggested_stay_minutes": 30,
                "existence_status": "VERIFIED",
                "hours_status": "HOURS_ASSUMED",
            }
            for i in range(1, 15)  # 14 candidates
        ]

        # Case A: Location limit binds (max_locations=5, max_matrix_cells=100 -> effective=min(5,10)=5)
        state_loc_limit: TripPlanningState = {
            "execution_id": str(exec_id),
            "max_locations": 5,
            "max_matrix_cells": 100,
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394, "stay_days": 1}],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {"primary_name": "Mysuru", "point": {"lat": 12.2958, "lng": 76.6394}, "existence_status": "VERIFIED"},
                *candidates,
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        res_a = dispatch_route_optimizer_node(state_loc_limit)
        req_a = RouteRequest.model_validate(res_a["pending_tasks"][0]["payload"])
        assert len(req_a.stops) == 5

        # Case B: Matrix cell limit binds (max_locations=20, max_matrix_cells=49 -> isqrt(49)=7 -> effective=7)
        state_cell_limit: TripPlanningState = {
            "execution_id": str(exec_id),
            "max_locations": 20,
            "max_matrix_cells": 49,
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394, "stay_days": 1}],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {"primary_name": "Mysuru", "point": {"lat": 12.2958, "lng": 76.6394}, "existence_status": "VERIFIED"},
                *candidates,
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        res_b = dispatch_route_optimizer_node(state_cell_limit)
        req_b = RouteRequest.model_validate(res_b["pending_tasks"][0]["payload"])
        assert len(req_b.stops) == 7

        # Case C: Endpoint allowing 12 stops does not artificially cap to 10
        state_larger_endpoint: TripPlanningState = {
            "execution_id": str(exec_id),
            "max_locations": 15,
            "max_matrix_cells": 144,  # isqrt(144) = 12
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394, "stay_days": 1}],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {"primary_name": "Mysuru", "point": {"lat": 12.2958, "lng": 76.6394}, "existence_status": "VERIFIED"},
                *candidates,
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        res_c = dispatch_route_optimizer_node(state_larger_endpoint)
        req_c = RouteRequest.model_validate(res_c["pending_tasks"][0]["payload"])
        assert len(req_c.stops) == 12

    def test_mandatory_anchors_exceeding_capacity_reports_diagnostic(self) -> None:
        """Verifies explicit diagnostic when mandatory anchors alone exceed provider capacity."""
        exec_id = uuid.uuid4()
        # 5 destinations + 1 origin = 6 mandatory stops, but provider only allows 4 locations
        dests = [
            {"name": f"Anchor City {i}", "latitude": 12.0 + i, "longitude": 76.0 + i, "stay_days": 1}
            for i in range(1, 6)
        ]
        eligible_dests = [
            {"primary_name": f"Anchor City {i}", "point": {"lat": 12.0 + i, "lng": 76.0 + i}, "existence_status": "VERIFIED"}
            for i in range(1, 6)
        ]

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "max_locations": 4,
            "max_matrix_cells": 16,
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": dests,
                "start_date": "2026-11-20",
                "end_date": "2026-11-25",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "20000.00", "currency": "INR"},
            },
            "eligible_stops": eligible_dests,
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        diags = outcome.get("provider_diagnostics", {})
        assert "optimizer_capacity" in diags
        assert "Mandatory anchors (count=6) exceed provider capacity" in diags["optimizer_capacity"]

    def test_multiday_windows_local_time_dwell_and_opening_hour_feasibility(self) -> None:
        """Verifies multi day itinerary distribution across distinct calendar dates,

        confirming venues have daily daytime opening hours rather than continuous overnight windows,
        and dwell exceeding daily window is deferred.
        """
        exec_id = uuid.uuid4()
        # 3 day trip: Nov 20 (Day 0), Nov 21 (Day 1), Nov 22 (Day 2)
        brief_data = {
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Mysuru",
                    "latitude": 12.2958,
                    "longitude": 76.6394,
                    "stay_days": 1,
                    "day_offset": 0,
                },
                {
                    "name": "Madikeri",
                    "latitude": 12.4244,
                    "longitude": 75.7382,
                    "stay_days": 1,
                    "day_offset": 1,
                },
            ],
            "start_date": "2026-11-20",
            "end_date": "2026-11-22",
            "timezone": "Asia/Kolkata",
            "travelers": 2,
            "rooms": 1,
            "budget": {"amount": "20000.00", "currency": "INR"},
        }

        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": brief_data,
            "eligible_stops": [
                # Mysuru anchor on Day 1 (Nov 20)
                {
                    "primary_name": "Mysuru",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
                # Madikeri anchor on Day 2 (Nov 21)
                {
                    "primary_name": "Madikeri",
                    "point": {"lat": 12.4244, "lng": 75.7382},
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                },
                # Candidate near Mysuru (should be scheduled on Nov 20)
                {
                    "primary_name": "Mysore Zoo",
                    "category": "nature",
                    "point": {"lat": 12.3020, "lng": 76.6640},
                    "suggested_stay_minutes": 120,
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "window_start": "08:30",
                    "window_end": "17:30",
                },
                # Candidate near Madikeri (should be scheduled on Nov 21)
                {
                    "primary_name": "Raja Seat",
                    "category": "viewpoint",
                    "point": {"lat": 12.4180, "lng": 75.7350},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "window_start": "06:00",
                    "window_end": "19:00",
                },
                # Candidate with dwell (600m = 10h) exceeding 9h daily window (deferred)
                {
                    "primary_name": "Exceeding Dwell Park",
                    "category": "nature",
                    "point": {"lat": 12.3100, "lng": 76.6500},
                    "suggested_stay_minutes": 600,  # 10 hours
                    "existence_status": "VERIFIED",
                    "hours_status": "HOURS_ASSUMED",
                    "window_start": "09:00",
                    "window_end": "18:00",  # 9 hours window
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        task_spec = outcome["pending_tasks"][0]
        route_req = RouteRequest.model_validate(task_spec["payload"])

        stops_by_name = {s.name: s for s in route_req.stops}

        assert "Mysuru" in stops_by_name
        assert "Madikeri" in stops_by_name
        assert "Mysore Zoo" in stops_by_name
        assert "Raja Seat" in stops_by_name
        assert "Exceeding Dwell Park" not in stops_by_name

        # Mysore Zoo must be scheduled on Day 1 (Nov 20)
        zoo_stop = stops_by_name["Mysore Zoo"]
        # 08:30 IST = 03:00 UTC on 2026-11-20
        assert zoo_stop.window_start == datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
        # 17:30 IST = 12:00 UTC on 2026-11-20
        assert zoo_stop.window_end == datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        assert zoo_stop.is_must_visit is False
        assert zoo_stop.dwell_seconds == 120 * 60

        # Raja Seat must be scheduled on Day 2 (Nov 21), NOT collapsed into Day 1
        raja_stop = stops_by_name["Raja Seat"]
        # 06:00 IST = 00:30 UTC on 2026-11-21
        assert raja_stop.window_start == datetime(2026, 11, 21, 0, 30, tzinfo=UTC)
        # 19:00 IST = 13:30 UTC on 2026-11-21
        assert raja_stop.window_end == datetime(2026, 11, 21, 13, 30, tzinfo=UTC)
        assert raja_stop.is_must_visit is False
        assert raja_stop.dwell_seconds == 60 * 60

        # Verify daily window duration is 13 hours, not a multi day continuous span
        win_duration = (raja_stop.window_end - raja_stop.window_start).total_seconds()
        assert win_duration == 13 * 3600

        # Verify Exceeding Dwell Park was deferred with explicit reason
        editorial = outcome["editorial_recommendations"]
        deferred_dwell = next(r for r in editorial if r["primary_name"] == "Exceeding Dwell Park")
        assert "suggested dwell (36000s) exceeds available daily opening window (32400s)" in deferred_dwell["diagnostics"]["deferral_reason"]

    def test_no_duplicate_anchor_in_editorial_recommendations(self) -> None:
        """Verifies unverified anchor city is not duplicated in editorial recommendations."""
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Unverified Hilltop",
                        "latitude": 12.5000,
                        "longitude": 75.8000,
                        "stay_days": 1,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [],
            # Pre existing editorial recommendation from prior node
            "editorial_recommendations": [
                {
                    "primary_name": "Unverified Hilltop",
                    "category": "destination",
                    "point": {"lat": 12.5000, "lng": 75.8000},
                    "uncertainty": "EDITORIAL_DISCOVERY",
                    "is_candidate": True,
                    "existence_status": "UNVERIFIED",
                }
            ],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        editorial = outcome["editorial_recommendations"]
        names = [r["primary_name"] for r in editorial if r.get("primary_name") == "Unverified Hilltop"]
        # Must appear exactly once, never duplicated
        assert len(names) == 1

    def test_skipped_candidate_does_not_consume_provider_slot(self) -> None:
        """Verifies candidate skipped due to opening window constraint does not consume provider slot.

        Ensures subsequent valid candidates are scheduled up to effective_max_stops.
        """
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Mysuru",
                        "latitude": 12.2958,
                        "longitude": 76.6394,
                        "stay_days": 1,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-20",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            # Capacity bounded to 3 stops: 2 anchors (origin + Mysuru) + 1 candidate slot
            "max_locations": 3,
            "max_matrix_cells": 16,  # sqrt(16) = 4, min(3, 4) = 3 effective stops
            "eligible_stops": [
                {
                    "primary_name": "Mysuru",
                    "category": "destination",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                },
                # First candidate: suggested stay exceeds daily opening window (skipped via continue)
                {
                    "primary_name": "Overlong Sight",
                    "category": "heritage",
                    "point": {"lat": 12.3000, "lng": 76.6500},
                    "suggested_stay_minutes": 600,  # 10 hours
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "window_start": "10:00",
                    "window_end": "14:00",  # 4 hour window
                },
                # Second candidate: valid dwell and window, should be scheduled into available slot
                {
                    "primary_name": "Valid Next Sight",
                    "category": "heritage",
                    "point": {"lat": 12.3100, "lng": 76.6600},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                    "hours_status": "VERIFIED_HOURS",
                    "window_start": "10:00",
                    "window_end": "17:00",
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        req = RouteRequest.model_validate(outcome["pending_tasks"][0]["payload"])
        stop_names = [s.name for s in req.stops]

        # Origin + Mysuru + Valid Next Sight = 3 stops scheduled
        assert len(req.stops) == 3
        assert "Bengaluru" in stop_names
        assert "Mysuru" in stop_names
        assert "Valid Next Sight" in stop_names
        assert "Overlong Sight" not in stop_names

        # Overlong Sight must be deferred with diagnostic explanation
        editorial = outcome["editorial_recommendations"]
        deferred_overlong = next(r for r in editorial if r["primary_name"] == "Overlong Sight")
        assert "suggested dwell (36000s) exceeds available daily opening window (14400s)" in deferred_overlong["diagnostics"]["deferral_reason"]

    def test_malformed_coordinates_do_not_crash_dispatch(self) -> None:
        """Verifies candidate with non numeric coordinate string defaults safely to 0.0 without exception."""
        exec_id = uuid.uuid4()
        state: TripPlanningState = {
            "execution_id": str(exec_id),
            "brief": {
                "origin_name": "Bengaluru",
                "origin_lat": 12.9716,
                "origin_lng": 77.5946,
                "destinations": [
                    {
                        "name": "Mysuru",
                        "latitude": 12.2958,
                        "longitude": 76.6394,
                        "stay_days": 1,
                    }
                ],
                "start_date": "2026-11-20",
                "end_date": "2026-11-20",
                "travelers": 2,
                "rooms": 1,
                "budget": {"amount": "10000.00", "currency": "INR"},
            },
            "eligible_stops": [
                {
                    "primary_name": "Mysuru",
                    "category": "destination",
                    "point": {"lat": 12.2958, "lng": 76.6394},
                    "existence_status": "VERIFIED",
                },
                {
                    "primary_name": "Bad Coordinate Sight",
                    "category": "heritage",
                    "point": {"lat": "invalid_latitude", "lng": None},
                    "suggested_stay_minutes": 60,
                    "existence_status": "VERIFIED",
                },
            ],
            "editorial_recommendations": [],
            "excluded_candidates": [],
        }

        outcome = dispatch_route_optimizer_node(state)
        req = RouteRequest.model_validate(outcome["pending_tasks"][0]["payload"])
        bad_stop = next(s for s in req.stops if s.name == "Bad Coordinate Sight")
        assert bad_stop.location.latitude == 0.0
        assert bad_stop.location.longitude == 0.0
