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

    def test_multiday_trip_distributes_stops_across_eligible_days_and_allows_empty_day(self) -> None:
        """2-day trip dispatches daily windows across both days without overnight visits or forced Day 2 stops."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from contracts.routing import RouteStatus
        from functions.route_optimizer.solver import OrToolsRoutingSolver

        ist = ZoneInfo("Asia/Kolkata")
        exec_id = uuid.uuid4()
        brief_2day = {
            "origin_name": "Bengaluru",
            "origin_lat": 12.9716,
            "origin_lng": 77.5946,
            "destinations": [
                {
                    "name": "Ooty",
                    "latitude": 11.4102,
                    "longitude": 76.6950,
                    "stay_days": 2,
                    "confidence": 1.0,
                }
            ],
            "must_visit_places": ["Government Botanical Garden"],
            "start_date": "2026-12-05",
            "end_date": "2026-12-06",
            "timezone": "Asia/Kolkata",
            "travelers": 2,
            "rooms": 1,
            "budget": {"amount": "15000.00", "currency": "INR"},
            "preferred_modes": ["car_petrol"],
        }

        garden = {
            "primary_name": "Government Botanical Garden",
            "category": "nature",
            "point": {"lat": 11.4188, "lng": 76.7113},
            "suggested_stay_minutes": 60,
            "is_must_visit": True,
            "existence_status": "VERIFIED",
            "hours_status": "VERIFIED_HOURS",
            "is_hours_assumed": False,
            "hours_details": {
                "opens": "07:00",
                "closes": "18:30",
                "open_visit_dates": [
                    {"date": "2026-12-05", "opens": "07:00", "closes": "18:30"},
                    {"date": "2026-12-06", "opens": "07:00", "closes": "18:30"},
                ],
            },
        }
        other_stops = [
            {
                "primary_name": f"Ooty Sight {i}",
                "category": "attraction",
                "point": {"lat": 11.41 + i * 0.005, "lng": 76.70 + i * 0.005},
                "suggested_stay_minutes": 60,
                "existence_status": "VERIFIED",
                "hours_status": "HOURS_ASSUMED",
                "is_hours_assumed": True,
            }
            for i in range(1, 8)
        ]
        # Pin one stop explicitly to Day 1
        other_stops[0]["visit_date"] = "2026-12-05"

        res = dispatch_route_optimizer_node(
            {
                "execution_id": str(exec_id),
                "brief": brief_2day,
                "eligible_stops": [garden, *other_stops],
                "editorial_recommendations": [],
                "excluded_candidates": [],
            }
        )
        req = RouteRequest.model_validate(res["pending_tasks"][0]["payload"])
        assert len(req.stops) == 9  # Bengaluru + 8 Ooty stops
        garden_stop = next(s for s in req.stops if s.name == "Government Botanical Garden")
        pinned_stop = next(s for s in req.stops if s.name == "Ooty Sight 1")
        unpinned_stop = next(s for s in req.stops if s.name == "Ooty Sight 2")
        assert len(garden_stop.daily_windows) == 2
        assert len(unpinned_stop.daily_windows) == 2
        assert pinned_stop.daily_windows == []

        # Matrix: Bengaluru <-> any Ooty stop = 13200s (3h40m), between Ooty stops = 300s (5m)
        n = len(req.stops)
        durations_s: list[list[int | None]] = [
            [0 if i == j else (13200 if i == 0 or j == 0 else 300) for j in range(n)]
            for i in range(n)
        ]
        distances_m: list[list[int | None]] = [
            [0 if i == j else (270000 if i == 0 or j == 0 else 2000) for j in range(n)]
            for i in range(n)
        ]

        sol = OrToolsRoutingSolver().solve(req, durations_s=durations_s, distances_m=distances_m)
        assert sol.status == RouteStatus.FEASIBLE
        assert len(sol.stops) == 9  # Zero dropped stops: overflow uses Day 2

        day1_stops = [
            s for s in sol.stops[1:]
            if s.planned_arrival_at and s.planned_arrival_at.astimezone(ist).date().isoformat() == "2026-12-05"
        ]
        day2_stops = [
            s for s in sol.stops[1:]
            if s.planned_arrival_at and s.planned_arrival_at.astimezone(ist).date().isoformat() == "2026-12-06"
        ]
        assert len(day1_stops) >= 4
        assert len(day2_stops) >= 2
        # Explicit Day 1 stop stays on Day 1
        assert any(s.name == "Ooty Sight 1" for s in day1_stops)
        # No overnight visits (all Day 2 stops start >= 07:00 IST and finish <= 18:30 IST)
        for s in day2_stops:
            assert s.planned_arrival_at is not None and s.planned_departure_at is not None
            arr_ist = s.planned_arrival_at.astimezone(ist)
            dep_ist = s.planned_departure_at.astimezone(ist)
            assert arr_ist >= datetime(2026, 12, 6, 7, 0, tzinfo=ist)
            assert dep_ist <= datetime(2026, 12, 6, 18, 30, tzinfo=ist)

        # Empty Day 2 is valid when a 2-day trip has only 1 stop
        res_single = dispatch_route_optimizer_node(
            {
                "execution_id": str(uuid.uuid4()),
                "brief": brief_2day,
                "eligible_stops": [garden],
                "editorial_recommendations": [],
                "excluded_candidates": [],
            }
        )
        req_single = RouteRequest.model_validate(res_single["pending_tasks"][0]["payload"])
        sol_single = OrToolsRoutingSolver().solve(
            req_single,
            durations_s=[[0, 13200], [13200, 0]],
            distances_m=[[0, 270000], [270000, 0]],
        )
        assert sol_single.status == RouteStatus.FEASIBLE
        assert len(sol_single.stops) == 2
        assert sol_single.stops[1].planned_arrival_at is not None
        assert sol_single.stops[1].planned_arrival_at.astimezone(ist).date().isoformat() == "2026-12-05"

    async def test_worker_hydration_honors_daily_windows_for_day2_and_detour_overflow(self) -> None:
        """_hydrate_leg_geometries_and_revalidate_schedule respects daily_windows on Day 2 and detour drops."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        from contracts.routing import (
            DailyWindow,
            OptimizedRoute,
            Point,
            RouteLegOutput,
            RouteObjective,
            RouteRequest,
            RouteStatus,
            RouteStopInput,
            ScheduledStopOutput,
            SolverStatus,
            StopType,
            TrafficBasis,
        )
        from contracts.trips import TransportMode
        from functions.route_optimizer.service import RouteOptimizerWorkerService
        from functions.route_optimizer.valhalla import ValhallaRouteResult

        day1_ws = datetime(2026, 12, 5, 3, 30, tzinfo=UTC)  # 09:00 IST
        day1_we = datetime(2026, 12, 5, 12, 30, tzinfo=UTC)  # 18:00 IST
        day2_ws = datetime(2026, 12, 6, 3, 30, tzinfo=UTC)  # 09:00 IST
        day2_we = datetime(2026, 12, 6, 12, 30, tzinfo=UTC)  # 18:00 IST
        dws = [
            DailyWindow(window_start=day1_ws, window_end=day1_we),
            DailyWindow(window_start=day2_ws, window_end=day2_we),
        ]

        origin = RouteStopInput(
            id=uuid.uuid4(),
            name="Origin",
            location=Point(latitude=11.41, longitude=76.70),
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
        )
        stop1 = RouteStopInput(
            id=uuid.uuid4(),
            name="Day1 Stop",
            location=Point(latitude=11.42, longitude=76.71),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            window_start=day1_ws,
            window_end=day1_we,
            daily_windows=dws,
            dwell_seconds=3600,
        )
        stop2 = RouteStopInput(
            id=uuid.uuid4(),
            name="Day2 Stop",
            location=Point(latitude=11.43, longitude=76.72),
            stop_type=StopType.DESTINATION,
            is_must_visit=False,
            window_start=day1_ws,
            window_end=day1_we,
            daily_windows=dws,
            dwell_seconds=3600,
        )

        req = RouteRequest(
            departure_at=day1_ws,
            return_deadline=datetime(2026, 12, 6, 14, 30, tzinfo=UTC),
            stops=[origin, stop1, stop2],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )

        # Initial route arrives at Day1 Stop at 16:45 IST (11:15Z), finishes at 17:45 IST (12:15Z),
        # so road arrival at Day2 Stop is 17:50 IST (12:20Z) -> waits overnight until Day 2 09:00 IST (03:30Z).
        initial_route = OptimizedRoute(
            status=RouteStatus.FEASIBLE,
            solver_status=SolverStatus.OPTIMAL,
            objective=RouteObjective.FASTEST,
            departure_at=day1_ws,
            stops=[
                ScheduledStopOutput(
                    id=origin.id,
                    stop_id=origin.id,
                    stop_order=0,
                    name=origin.name,
                    location=origin.location,
                    stop_type=StopType.ORIGIN,
                    planned_arrival_at=day1_ws,
                    planned_departure_at=day1_ws,
                ),
                ScheduledStopOutput(
                    id=stop1.id,
                    stop_id=stop1.id,
                    stop_order=1,
                    name=stop1.name,
                    location=stop1.location,
                    stop_type=StopType.DESTINATION,
                    is_must_visit=True,
                    window_start=day1_ws,
                    window_end=day1_we,
                    daily_windows=dws,
                    dwell_seconds=3600,
                    planned_arrival_at=datetime(2026, 12, 5, 11, 15, tzinfo=UTC),
                    planned_departure_at=datetime(2026, 12, 5, 12, 15, tzinfo=UTC),
                ),
                ScheduledStopOutput(
                    id=stop2.id,
                    stop_id=stop2.id,
                    stop_order=2,
                    name=stop2.name,
                    location=stop2.location,
                    stop_type=StopType.DESTINATION,
                    is_must_visit=False,
                    window_start=day2_ws,
                    window_end=day2_we,
                    daily_windows=dws,
                    dwell_seconds=3600,
                    planned_arrival_at=day2_ws,
                    planned_departure_at=day2_ws + timedelta(seconds=3600),
                ),
            ],
            legs=[
                RouteLegOutput(
                    leg_order=0,
                    from_stop_id=origin.id,
                    to_stop_id=stop1.id,
                    from_stop_name=origin.name,
                    to_stop_name=stop1.name,
                    transport_mode=TransportMode.CAR_PETROL,
                    departure_at=day1_ws,
                    arrival_at=datetime(2026, 12, 5, 11, 15, tzinfo=UTC),
                    distance_m=10000,
                    travel_seconds=27900,
                    traffic_basis=TrafficBasis.MODELED,
                ),
                RouteLegOutput(
                    leg_order=1,
                    from_stop_id=stop1.id,
                    to_stop_id=stop2.id,
                    from_stop_name=stop1.name,
                    to_stop_name=stop2.name,
                    transport_mode=TransportMode.CAR_PETROL,
                    departure_at=datetime(2026, 12, 5, 12, 15, tzinfo=UTC),
                    arrival_at=datetime(2026, 12, 5, 12, 20, tzinfo=UTC),
                    distance_m=2000,
                    travel_seconds=300,
                    traffic_basis=TrafficBasis.MODELED,
                ),
            ],
        )

        # Case 1: Normal hydration keeps Day2 Stop on Day 2 (09:00-10:00 IST) with updated window_start/end
        mock_valhalla = MagicMock()
        mock_valhalla.route = AsyncMock(
            side_effect=[
                ValhallaRouteResult(total_distance_m=10000, total_travel_seconds=27900, legs=[], geometry={"type": "LineString", "coordinates": [[76.70, 11.41], [76.71, 11.42]]}),
                ValhallaRouteResult(total_distance_m=2000, total_travel_seconds=300, legs=[], geometry={"type": "LineString", "coordinates": [[76.71, 11.42], [76.72, 11.43]]}),
            ]
        )
        svc = RouteOptimizerWorkerService(session=MagicMock(), valhalla_client=mock_valhalla)
        hydrated = await svc._hydrate_leg_geometries_and_revalidate_schedule(
            initial_route=initial_route,
            request=req,
            client=mock_valhalla,
            restrictions=None,
        )
        assert hydrated.status == RouteStatus.FEASIBLE
        day2_out = hydrated.stops[2]
        assert day2_out.planned_arrival_at == day2_ws
        assert day2_out.planned_departure_at == day2_ws + timedelta(seconds=3600)
        assert day2_out.window_start == day2_ws
        assert day2_out.window_end == day2_we

        # Case 2: Huge detour on leg 1 pushes arrival past Day 2 closing -> optional Day2 Stop is dropped
        mock_valhalla.route = AsyncMock(
            side_effect=[
                ValhallaRouteResult(total_distance_m=10000, total_travel_seconds=27900, legs=[], geometry={"type": "LineString", "coordinates": [[76.70, 11.41], [76.71, 11.42]]}),
                ValhallaRouteResult(total_distance_m=500000, total_travel_seconds=90000, legs=[], geometry={"type": "LineString", "coordinates": [[76.71, 11.42], [76.72, 11.43]]}),
            ]
        )
        svc._solve_route_with_provider = AsyncMock(  # type: ignore[method-assign]
            return_value=OptimizedRoute(
                status=RouteStatus.FEASIBLE,
                solver_status=SolverStatus.OPTIMAL,
                objective=RouteObjective.FASTEST,
                departure_at=day1_ws,
                stops=initial_route.stops[:2],
                legs=initial_route.legs[:1],
            )
        )
        detoured = await svc._hydrate_leg_geometries_and_revalidate_schedule(
            initial_route=initial_route,
            request=req,
            client=mock_valhalla,
            restrictions=None,
        )
        assert detoured.status == RouteStatus.PARTIAL
        assert len(detoured.dropped_stops) == 1
        assert detoured.dropped_stops[0].name == "Day2 Stop"

    async def test_multimodal_pedestrian_and_replan_hydration_honor_daily_windows_on_day2(self) -> None:
        """Pedestrian multimodal branch and RouteReplanService._hydrate_new_legs honor Day 2 daily_windows."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        from contracts.routing import (
            DailyWindow,
            OptimizedRoute,
            Point,
            RouteLegOutput,
            RouteObjective,
            RouteRequest,
            RouteStatus,
            RouteStopInput,
            ScheduledStopOutput,
            SolverStatus,
            StopType,
            TrafficBasis,
        )
        from contracts.trips import TransportMode
        from functions.route_optimizer.multimodal import MultimodalTransitionService
        from functions.route_optimizer.replan import RouteReplanService
        from functions.route_optimizer.valhalla import ValhallaRouteResult

        day1_ws = datetime(2026, 12, 5, 3, 30, tzinfo=UTC)
        day1_we = datetime(2026, 12, 5, 12, 30, tzinfo=UTC)
        day2_ws = datetime(2026, 12, 6, 3, 30, tzinfo=UTC)
        day2_we = datetime(2026, 12, 6, 12, 30, tzinfo=UTC)
        dws = [
            DailyWindow(window_start=day1_ws, window_end=day1_we),
            DailyWindow(window_start=day2_ws, window_end=day2_we),
        ]

        # 1. Pedestrian-only stop arriving late Day 1 waits overnight and completes on Day 2
        origin = RouteStopInput(
            id=uuid.uuid4(),
            name="Origin",
            location=Point(latitude=11.41, longitude=76.70),
            stop_type=StopType.ORIGIN,
            dwell_seconds=0,
        )
        ped_stop = RouteStopInput(
            id=uuid.uuid4(),
            name="Pedestrian Garden",
            location=Point(latitude=11.42, longitude=76.71),
            stop_type=StopType.DESTINATION,
            is_must_visit=True,
            is_pedestrian_only=True,
            window_start=day1_ws,
            window_end=day1_we,
            daily_windows=dws,
            dwell_seconds=3600,
        )
        mm_req = RouteRequest(
            departure_at=datetime(2026, 12, 5, 12, 15, tzinfo=UTC),  # 17:45 IST Day 1
            return_deadline=datetime(2026, 12, 6, 14, 30, tzinfo=UTC),
            stops=[origin, ped_stop],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )
        mock_valhalla = MagicMock()
        mock_valhalla.route = AsyncMock(
            return_value=ValhallaRouteResult(
                total_distance_m=1500,
                total_travel_seconds=600,
                legs=[],
                geometry={"type": "LineString", "coordinates": [[76.70, 11.41], [76.71, 11.42]]},
            )
        )
        from geoalchemy2.shape import from_shape
        from shapely.geometry import Point as ShapelyPoint

        mm_svc = MultimodalTransitionService(valhalla_client=mock_valhalla)
        hub_mock = MagicMock(
            id=uuid.uuid4(),
            primary_name="Garden Parking",
            point=from_shape(ShapelyPoint(76.709, 11.419), srid=4326),
        )
        mm_svc.find_verified_parking_hub = AsyncMock(return_value=(hub_mock, 120.0))  # type: ignore[method-assign]
        mm_route = await mm_svc.plan_multimodal_route(mm_req)
        assert mm_route.status == RouteStatus.FEASIBLE
        ped_out = next(s for s in mm_route.stops if s.name == "Pedestrian Garden")
        assert ped_out.planned_arrival_at == day2_ws
        assert ped_out.planned_departure_at == day2_ws + timedelta(seconds=3600)
        assert ped_out.window_start == day2_ws
        assert ped_out.window_end == day2_we
        assert ped_out.daily_windows == dws

        # 2. RouteReplanService._hydrate_new_legs preserves Day 2 active window
        replan_svc = RouteReplanService()
        sub_req = RouteRequest(
            departure_at=datetime(2026, 12, 5, 12, 15, tzinfo=UTC),
            return_deadline=datetime(2026, 12, 6, 14, 30, tzinfo=UTC),
            stops=[origin, ped_stop.model_copy(update={"is_pedestrian_only": False})],
            is_round_trip=False,
            transport_mode=TransportMode.CAR_PETROL,
        )
        replan_init = OptimizedRoute(
            status=RouteStatus.FEASIBLE,
            solver_status=SolverStatus.OPTIMAL,
            objective=RouteObjective.FASTEST,
            departure_at=sub_req.departure_at,
            stops=[
                ScheduledStopOutput(
                    id=origin.id,
                    stop_id=origin.id,
                    stop_order=0,
                    name=origin.name,
                    location=origin.location,
                    stop_type=StopType.ORIGIN,
                    planned_arrival_at=sub_req.departure_at,
                    planned_departure_at=sub_req.departure_at,
                ),
                ScheduledStopOutput(
                    id=ped_stop.id,
                    stop_id=ped_stop.id,
                    stop_order=1,
                    name=ped_stop.name,
                    location=ped_stop.location,
                    stop_type=StopType.DESTINATION,
                    is_must_visit=True,
                    window_start=day2_ws,
                    window_end=day2_we,
                    daily_windows=dws,
                    dwell_seconds=3600,
                    planned_arrival_at=day2_ws,
                    planned_departure_at=day2_ws + timedelta(seconds=3600),
                ),
            ],
            legs=[
                RouteLegOutput(
                    leg_order=0,
                    from_stop_id=origin.id,
                    to_stop_id=ped_stop.id,
                    from_stop_name=origin.name,
                    to_stop_name=ped_stop.name,
                    transport_mode=TransportMode.CAR_PETROL,
                    departure_at=sub_req.departure_at,
                    arrival_at=datetime(2026, 12, 5, 12, 25, tzinfo=UTC),
                    distance_m=1500,
                    travel_seconds=600,
                    traffic_basis=TrafficBasis.MODELED,
                )
            ],
        )
        hydrated_replan = await replan_svc._hydrate_new_legs(
            new_route_solution=replan_init,
            sub_request=sub_req,
            client=mock_valhalla,
            restrictions=None,
        )
        assert hydrated_replan.status == RouteStatus.FEASIBLE
        replan_out = hydrated_replan.stops[1]
        assert replan_out.planned_arrival_at == day2_ws
        assert replan_out.window_start == day2_ws
        assert replan_out.window_end == day2_we
        assert replan_out.daily_windows == dws
