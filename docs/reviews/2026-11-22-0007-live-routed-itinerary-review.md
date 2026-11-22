# Review: 0007 Live Routed Itinerary Verification, 2026-11-22

* **Reviewer**: Senior Staff Reviewer
* **Scope**: Live backend execution artifact inspection, harness comparison over `bb058bd54fb9003441053a475a04c076918f4f83`, routing provider metrics, and itinerary proposal correctness
* **Live Routing Verdict**: PASS
* **Itinerary Correctness Verdict**: PARTIAL (DEFECTS CONFIRMED)

## Executive Summary

This review assesses the live backend execution of the travel planner pipeline for a two day road journey from Mysuru through Srirangapatna to Mandya across 25 to 26 November 2026. The execution ran against live external services: Amazon Bedrock (Nova Lite), SerpAPI, Scrapling, the public Valhalla routing engine at openstreetmap.de, Open-Meteo, Obscura, and an isolated PostgreSQL acceptance database. Remote LAYA was explicitly disabled for this run.

The evaluation demonstrates a clear dichotomy between transport routing and itinerary planning logic. Live routing achieved an unequivocal **PASS**: the Valhalla distance matrix returned HTTP 200 with all distances under 46 kilometers, all 9 road legs were computed and decoded with full geometry, the route was persisted under ID `b57c90a5-23b7-42f7-80da-e4d93c0ff7f8`, a complete 20 feature GeoJSON map was served by the authenticated API, and execution resumed cleanly across two checkpoint boundaries.

In contrast, itinerary domain correctness revealed four distinct defects categorized as **PARTIAL (DEFECTS CONFIRMED)**:
1. Lodging was budgeted for two nights (7,000 INR) instead of one night (3,500 INR) for a two day trip because destination stay days were summed without calendar clamping.
2. Meal expenses were completely absent from the budget breakdown despite explicit traveler request and successful food research.
3. A public city park (Shivananjappa Park) was scheduled as the overnight stay stop spanning 16 hours because accommodations are segregated from routing solver stops and anchor towns lacked verified existence claims.
4. An evening light and sound show was scheduled at 11:45 AM in broad daylight because missing operating hours defaulted to a generic daytime window under `HOURS_ASSUMED`.

## Harness Diff and Artifact Preservation

### Harness Diff Inspection

The test harness in `backend/scripts/run_live_backend_journey.py` was compared directly against commit `bb058bd54fb9003441053a475a04c076918f4f83`. The diff reveals strictly scoped, non intrusive changes:
* Updated the input brief payload from the previous Bengaluru corridor to the requested Mysuru to Srirangapatna to Mandya journey (25 to 26 November 2026, 12,000 INR budget, two adults, petrol car, local Karnataka food, daylight driving).
* Explicitly deactivated remote LAYA by setting `settings.LAYA_ENDPOINT_URL = ""` and updated the readiness probe to report `DISABLED`.
* Enriched database audit queries to inspect `route_stops`, `route_legs`, and task payload summaries to ensure complete observability of solver inputs and persisted road geometry.
* Zero application logic, schema definitions, or domain models were modified in the harness.

### Artifact Preservation

To maintain complete historical auditability, earlier run records were preserved without overwriting:
* The prior Bengaluru to Mysuru to Madikeri execution artifact (demonstrating Valhalla HTTP 400 distance rejection and partial publication recovery) was extracted and preserved at `backend/scripts/live_journey_result_bengaluru_mysuru_madikeri.json`.
* The current Mysuru to Srirangapatna to Mandya execution artifact was preserved at `backend/scripts/live_journey_result_mysuru_srirangapatna_mandya.json` and mirrored to `backend/scripts/live_journey_result.json`.

## Live Routing Verification: PASS

The live routing subsystem operated flawlessly without degraded fallbacks:

* **Valhalla Matrix Calculation**:
  * Endpoint: `/sources_to_targets`
  * Locations: 10 points (Mysuru origin plus 9 discovered attraction stops)
  * Status: HTTP 200 OK
  * Result: Full 10x10 matrix resolved. All pairwise distances remained below 46 kilometers, comfortably below the provider threshold of 150 kilometers.
* **Road Leg Computation and Persistence**:
  * Endpoint: `/route`
  * Computed Legs: 9 sequential road legs connecting 10 stops
  * Status: HTTP 200 OK across all legs
  * Total Distance: 64,881 meters (64.88 kilometers)
  * Total Travel Time: 11,891 seconds (3 hours 18 minutes 11 seconds)
  * Estimated Fuel: 443.35 INR (based on petrol consumption benchmark of 15 km/l)
  * Route Record: Durably saved in `routes` table with ID `b57c90a5-23b7-42f7-80da-e4d93c0ff7f8`, `status = 'ACTIVE'`, and `solver_status = 'OPTIMAL'`.
  * Detail Tables: 10 rows written to `route_stops` with precise dwell and arrival times; 9 rows written to `route_legs` with transport mode `car_petrol` and provider `valhalla`.
* **GeoJSON Map Delivery**:
  * Authenticated Endpoint: `GET /api/v1/trips/{trip_id}/map`
  * Status: HTTP 200 OK
  * Payload: FeatureCollection containing 20 features (10 Point features with stop sequences and names, 9 LineString features with decoded polyline6 road coordinates, and 1 bounding box).
* **Durable Checkpoint Resumption**:
  * Checkpoint 1: Captured upon completion of evidence gathering (Task `93cc9cd6-5426-4a6e-bb7c-8bb94062ff9c`, action `collect`). Resumed cleanly by `GraphRunnerService`.
  * Checkpoint 2: Captured upon completion of route optimization (Task `8b67111e-dbcc-45a4-9740-df9736894afa`, action `solve`). Resumed cleanly by `GraphRunnerService`.
  * Overall Duration: 34.3 seconds from API submission to published proposal with status `SUCCEEDED`.
* **Remote LAYA Isolation**:
  * Verified zero remote network calls made to remote LAYA.
  * Audit confirmed exactly two decision events in `laya_decisions`, both resolved via `DETERMINISTIC_SINGLETON` bypass.

## Itinerary Correctness Analysis: PARTIAL (DEFECTS CONFIRMED)

Detailed inspection of the published itinerary and database records revealed four significant domain correctness defects:

### Defect 1: Lodging Nights Overcounting

* **Source Location**: `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` lines 2663 to 2674 in `compute_budget_node`.
* **Artifact Reference**: `live_journey_result_mysuru_srirangapatna_mandya.json` under `api_journey.itinerary_endpoint.body.budget_lines`.
* **Mechanism**:
  The budget computation node iterates over destinations in the brief:
  ```python
  for d in brief.destinations:
      stay_days = d.stay_days or 1
      budget_lines.append(
          BudgetLine(
              category="accommodation",
              description=f"Lodging in {d.name} ({stay_days} nights)",
              amount=Decimal(str(d.stay_days * 3500)),
              ...
          )
      )
  ```
  The brief specifies a trip from 2026-11-25 to 2026-11-26 (two days, exactly one calendar night). However, because the brief lists two destination stops (Srirangapatna with `stay_days=1` and Mandya with `stay_days=1`), the loop generates two separate accommodation lines:
  * `hotel_stay_Srirangapatna (1 rooms, 1 nights)`: 3,500.00 INR
  * `hotel_stay_Mandya (1 rooms, 1 nights)`: 3,500.00 INR
  Total accommodation is budgeted at 7,000.00 INR instead of 3,500.00 INR.
* **Correction Required**:
  Accommodation nights must be clamped to total trip calendar nights `(end_date - start_date).days`. When multiple destinations exist on a multi day trip, stay nights must be apportioned or clamped rather than naively accumulated per stop.

### Defect 2: Missing Meals Budget Representation

* **Source Location**: `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` lines 2649 to 2700 in `compute_budget_node`.
* **Artifact Reference**: `live_journey_result_mysuru_srirangapatna_mandya.json` under `api_journey.itinerary_endpoint.body.budget_lines`.
* **Mechanism**:
  The traveler brief explicitly specified: `"Budget ₹12,000 total for one night of lodging, meals, fuel, and tolls"`.
  The evidence collection pipeline performed culinary research and discovered `Dakshina paaka veg restaurant@blr- mys expressway`. However, `compute_budget_node` only computes lines for `transport` (fuel), `tolls_and_fees` (tolls), `accommodation` (lodging), and `activities` (entrance fees).
  There is zero logic to compute or estimate meal expenses. The published budget lines contain zero items under category `food` or `meals`, failing to provide either an estimated daily dining allowance or an explicit unknown budget line for meals.
* **Correction Required**:
  Add meal budget synthesis in `compute_budget_node` that incorporates either benchmark daily dining estimates or explicit unpriced budget lines for food.

### Defect 3: Attraction Scheduled as Overnight Lodging

* **Source Location**: `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` lines 1050 to 1090 in `dispatch_route_optimizer_node` and `backend/src/functions/route_optimizer/solver.py`.
* **Artifact Reference**: `live_journey_result_mysuru_srirangapatna_mandya.json` under `database_audit.routes[0].stops[6]`.
* **Mechanism**:
  In `dispatch_route_optimizer_node`, accommodation candidates are partitioned into `editorial_recommendations` and deliberately excluded from the solver stop pool. Furthermore, the destination anchor towns (such as Mandya city center) lacked verified existence claims and were excluded from solver stops.
  Consequently, the route solver received only the origin (Mysuru) and 9 attraction candidate POIs. To span the two day schedule across 25 and 26 November, the solver chose Stop 6 (`Shivananjappa Park` in Mandya, stop type `attraction`) as the final stop of Day 1 and scheduled:
  * Planned Arrival: `2026-11-25T11:29:52Z` (16:59:52 IST on Day 1)
  * Planned Departure: `2026-11-26T04:30:00Z` (10:00:00 IST on Day 2)
  * Wait Time: 57,608 seconds (16 hours 0 minutes 8 seconds)
  * Dwell Time: 3,600 seconds (1 hour)
  The itinerary effectively schedules travelers to sleep overnight in a public city park.
* **Correction Required**:
  For multi day itineraries, the solver must include designated overnight accommodation stops or town center lodging anchors. Attraction POIs must have max dwell or opening window constraints that prevent them from absorbing multi hour overnight layovers.

### Defect 4: Evening Light and Sound Show Scheduled in Daylight

* **Source Location**: `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` lines 1069 to 1075 in `dispatch_route_optimizer_node`.
* **Artifact Reference**: `live_journey_result_mysuru_srirangapatna_mandya.json` under `database_audit.routes[0].stops[2]`.
* **Mechanism**:
  For Stop 2, `Srirangapatna Light and Sound Show`, evidence collection established verified place existence via SerpAPI. However, no structured operating hours were discovered in source observations.
  When operating hours are missing, `dispatch_route_optimizer_node` applies a fallback daytime window:
  ```python
  open_time = time(9, 0)
  close_time = time(18, 0)
  label = "HOURS_ASSUMED"
  ```
  Because the window was set to 09:00 to 18:00 IST, the route optimizer scheduled this evening show at `11:45:59 IST` (arrival 11:45:59, departure 12:45:59). Scheduling a nocturnal light and sound show in the late morning sun is physically nonsensical.
* **Correction Required**:
  Improve POI categorization or time window inference so that nocturnal attractions (such as light shows, night markets, and observatories) do not default to standard daytime hours. When hours are unverified for nocturnal attractions, fall back to evening windows (e.g. 19:00 to 21:00 IST) or flag as unfeasible for daytime schedules.

## Summary Table of Audit Evidence

| Area | Component | Expected | Observed | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| Routing Engine | Valhalla Matrix | HTTP 200 (< 150 km) | HTTP 200 (all distances <= 46 km) | PASS |
| Road Geometry | Valhalla Route | 9 legs with polylines | 9 legs persisted, 64.88 km total | PASS |
| Map Visualization | API Map Endpoint | 20 GeoJSON features | 20 features (10 stops, 9 legs, 1 bbox) | PASS |
| Checkpoint Recovery| GraphRunnerService | Clean resume on interrupts | Resumed twice, 34.3s total time | PASS |
| Remote LAYA | Endpoint Isolation | Disabled, 0 remote calls | 0 calls, 2 singleton bypasses | PASS |
| Lodging Budget | compute_budget_node | 1 night (3,500 INR) | 2 nights (7,000 INR) | DEFECT |
| Meals Budget | compute_budget_node | Food budget lines present | 0 food budget lines | DEFECT |
| Overnight Stop | Solver Scheduling | Hotel or town anchor | Public park (16 hr overnight wait) | DEFECT |
| Attraction Hours | dispatch_optimizer | Evening hours window | 11:45 AM daylight schedule | DEFECT |

## Verdict and Next Steps

* **Live Routing**: **PASS**. The live routing engine, database schema, lease fencing, worker execution, and API delivery are robust, complete, and verified with live providers.
* **Itinerary Correctness**: **PARTIAL (DEFECTS CONFIRMED)**. The four identified defects reside strictly within orchestrator graph nodes and solver stop assignment, leaving the underlying routing and execution infrastructure intact.

**Recommended Bounded Next Action**:
Address Defect 1 (clamping lodging nights to calendar nights) and Defect 2 (adding meal budget representation) in `compute_budget_node` within `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` as a surgical fix slice, followed by unit test coverage.
