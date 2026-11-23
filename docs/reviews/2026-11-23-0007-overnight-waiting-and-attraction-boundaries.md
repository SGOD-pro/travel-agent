# Code Review: Slice 0007 Overnight Waiting and Attraction Visit Boundaries

* **Reviewer**: Senior Code Reviewer
* **Date**: 2026-11-23
* **Base Revision**: `a18d32df5f41343b08288dba6c510a896aa5a6cc`
* **Verdict**: Approve

## Executive Summary

This review assesses the uncommitted changes for slice 0007 addressing overnight waiting slack and attraction visit boundaries.

In the historical live execution of the Mysuru to Srirangapatna to Mandya corridor across 25 and 26 November 2026, the route solver scheduled Shivananjappa Park with arrival at 16:59:52 IST on Day 1, departure at 10:00:00 IST on Day 2, waiting time of 57608 seconds (16 hours), and dwell time of 3600 seconds (1 hour). This schedule resulted in a public park absorbing an overnight layover, producing an itinerary where travelers were scheduled to stay inside a park overnight.

The changes under review resolve this defect cleanly across all routing and orchestration tiers:
1. When waiting spans overnight (`arr_time.date() < service_start.date() or wait >= 4 * 3600`), the attraction visit arrival timestamp (`planned_arrival_at`) is clamped to the service window start time (`service_start`).
2. Dwell duration matches `planned_departure_at - planned_arrival_at` exactly, and visit intervals finish strictly within permitted operating windows.
3. Overnight waiting slack continues to be recorded honestly in `wait_seconds` and `total_wait_seconds`.
4. Parks and general attractions are never falsely relabeled as hotels or lodging.
5. Unassigned accommodation uncertainty is explicitly surfaced in route assumptions without inventing fictitious hotels, guessed coordinates, or hallucinated connecting road geometry.
6. Same day routes arriving before opening continue to preserve arrival at transit arrival without regression, maintaining full Slice 1 parity.
7. Both historical live execution artifacts are preserved under distinct filenames.
8. Static analysis with ruff and mypy is completely clean, and unit test suites pass without error.

The solution is minimal, surgical, robust, and fully conformant with project guidelines and specification 0007.

## Scope of Changes Inspected

The diff against base revision `a18d32df5f41343b08288dba6c510a896aa5a6cc` touches 72 added lines across six production files plus one new test suite:

- `backend/src/functions/route_optimizer/solver.py`: Clamps `planned_arrival_at` to `service_start` for overnight waiting stops, and records an unassigned accommodation assumption when waiting exceeds four hours.
- `backend/src/functions/route_optimizer/service.py`: Hydrates road legs with leg arrival matching transit arrival on Day 1, while clamping stop planned arrival to Day 2 service window start, preserving route assumptions.
- `backend/src/functions/route_optimizer/replan.py`: Applies identical clamping logic during route replanning, ensuring continuity if a replan recalculates overnight schedules.
- `backend/src/functions/route_optimizer/multimodal.py`: Applies identical clamping logic during multimodal routing transitions and pedestrian hub splits.
- `backend/src/functions/optimizer/solver.py`: Implements defensive clamping in `ItineraryScheduler` facade to guard against legacy unadjusted stop inputs.
- `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`: Implements defensive clamping in `consume_route_result_node` when mapping persisted route stops to API scheduled stops.
- `backend/tests/unit/test_route_optimizer_overnight_boundaries.py`: New comprehensive test suite covering the reproduced Mysuru scenario, same day pre opening slack, worker hydration, graph consumption, and unassigned accommodation integrity.

## Detailed Verification

### 1. Reproduction and Resolution of the Shivananjappa Park Defect

The reproduced Mysuru to Mandya corridor test (`test_reproduced_mysuru_shivananjappa_park_overnight_boundary`) configures Tipu Sultan Summer Palace with departure at 16:07:45 IST on 2026-11-25 (10:37:45 UTC) and Shivananjappa Park with an operating window between 09:00:00 IST and 18:00:00 IST on 2026-11-26 (03:30:00 to 12:30:00 UTC).

Under the baseline logic, arrival at the park occurred at 16:59:52 IST on Day 1, while departure occurred at 10:00:00 IST on Day 2. The stop arrival was recorded on Day 1, resulting in a scheduled park visit duration of 17 hours.

With the changes applied:
- `wait_seconds` is recorded honestly as 57608 seconds (16 hours).
- `total_wait_seconds` on the route is 57608 seconds.
- `planned_arrival_at` is clamped to 03:30:00 UTC on 2026-11-26 (09:00:00 IST on Day 2).
- `planned_departure_at` is 04:30:00 UTC on 2026-11-26 (10:00:00 IST on Day 2).
- Dwell time is 3600 seconds (1 hour).
- Dwell duration matches `planned_departure_at - planned_arrival_at` exactly (3600 seconds).
- The visit interval finishes strictly before the closing time of 18:00:00 IST.

### 2. Clamping Logic Consistency Across Solver, Hydration, Replan, and Multimodal Modules

The overnight condition is defined consistently across all routing components:

```python
is_overnight_wait = wait > 0 and (
    arr_time.date() < service_start.date() or wait >= 4 * 3600
)
stop_arrival = service_start if is_overnight_wait else arr_time
```

This logic is implemented identically in:
- `OrToolsRoutingSolver.solve` (`backend/src/functions/route_optimizer/solver.py`)
- `RouteOptimizerWorkerService._hydrate_leg_geometries_and_revalidate_schedule` (`backend/src/functions/route_optimizer/service.py`)
- `RouteReplanService.replan_route` (`backend/src/functions/route_optimizer/replan.py`)
- `MultimodalTransitionService.plan_multimodal_route` (`backend/src/functions/route_optimizer/multimodal.py`)

In `RouteOptimizerWorkerService`, the road leg arrival (`leg.arrival_at`) is preserved as the actual transit arrival (`arr_time`), representing when driving arrived on Day 1. The attraction stop planned arrival (`stop.planned_arrival_at`) is clamped to Day 2 service start (`service_start`). This represents reality: transit arrived in Mandya the previous evening, travelers waited overnight, and the attraction visit commenced upon park opening the following morning.

### 3. Honest Slack Recording Without Mislabeling Attractions as Lodging

The changes avoid inventing or synthesizing lodging:
- The stop type remains `DESTINATION` or `WAYPOINT`.
- The stop name remains unaltered (`Shivananjappa Park Mandya`).
- No attraction is tagged with lodging or hotel categories.
- Total waiting time continues to reflect the full duration in `wait_seconds` and `total_wait_seconds`, preventing hidden or lost time in optimization metrics.

### 4. Preservation of Accommodation Uncertainty

In accordance with specification 0007 AC-3 and AC-15, the system must not fabricate hotel coordinates, check in policies, or connecting road legs when accommodation has not been assigned.

The route optimizer now automatically includes an explicit assumption whenever `total_wait_seconds` or any stop `wait_seconds` exceeds four hours:

```text
Overnight waiting interval with unassigned accommodation: lodging coordinates, verified hours, and connecting road geometry are unassigned
```

This assumption communicates the uncertainty transparently to API consumers and travelers without inventing unverified facts.

### 5. Preservation of Same Day Pre Opening Waiting Slack (Slice 1 Parity)

A crucial regression concern was whether same day routes with brief waiting slack before opening (for example, arriving 15 minutes before an attraction opens) would have their arrival time prematurely shifted.

The condition requires `arr_time.date() < service_start.date()` or `wait >= 4 * 3600`.

On same day routes arriving within normal pre opening slack:
- `arr_time.date() == service_start.date()`
- `wait < 4 * 3600` (e.g. 900 seconds)

As verified in `test_same_day_pre_opening_waiting_slack_preserved`:
- `planned_arrival_at` remains `arr_time` (09:15 UTC).
- `wait_seconds` remains 900 seconds.
- `planned_departure_at` is 10:30 UTC.
- No overnight assumption is attached.
- Full Slice 1 behavior is preserved without regression.

### 6. Defensive Protection in Orchestrator Graph and ItineraryScheduler

To protect against legacy database records or unadjusted stop objects from older solver versions, defensive guards were added:

1. `consume_route_result_node` in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`:
   Checks if `wait_seconds >= 4 * 3600` and `stop_arrival.date() < s.planned_departure_at.date()`. If true, `stop_arrival` is safely recomputed as `s.planned_departure_at - timedelta(seconds=dwell_seconds_int)`.

2. `ItineraryScheduler` in `backend/src/functions/optimizer/solver.py`:
   Applies the same defensive calculation when mapping `stop_out` to `ScheduledStop`.

Tests `test_consume_route_result_defensive_clamp_for_legacy_unadjusted_arrival` and `test_itinerary_scheduler_overnight_schedule_mapping` prove that legacy inputs are sanitized before presentation to API clients.

### 7. Historical Artifact Preservation

The live execution artifacts are preserved with distinct filenames:
- `backend/scripts/live_journey_result_bengaluru_mysuru_madikeri.json`: 99,581 bytes, preserving the earlier Bengaluru corridor run with Valhalla distance rejection and partial publication recovery.
- `backend/scripts/live_journey_result_mysuru_srirangapatna_mandya.json`: 405,245 bytes, preserving the live Mysuru corridor run.
- `backend/scripts/live_journey_result.json`: 405,245 bytes, active link to the latest run.

Neither historical record has been lost or overwritten.

### 8. Remote LAYA Isolation and Out of Scope Items

- Remote LAYA remains disabled (`settings.LAYA_ENDPOINT_URL = ""`), with deterministic singleton bypasses handling orchestrator gates.
- Meal budget computation and nocturnal attraction operating hours inference remain strictly out of scope for later slices, maintaining surgical focus on overnight waiting and attraction visit boundaries.

### 9. Test Suite Execution and Static Verification

Verification was executed against the repository:

- `test_route_optimizer_overnight_boundaries.py`: 7 passed in 11.50 seconds.
- `test_route_optimizer_slice1.py` through `test_route_optimizer_slice7.py`: 75 passed in 101.34 seconds.
- `test_optimizer_dispatch_semantics.py` and `test_route_graph_state_machine_unit.py`: 21 passed in 1.36 seconds.
- Static check with ruff: `ruff check backend/src/functions/route_optimizer backend/src/functions/optimizer backend/src/functions/orchestrator backend/tests/unit/test_route_optimizer_overnight_boundaries.py` passed with 0 errors.
- Type check with mypy: `mypy src/functions/route_optimizer/...` passed with 0 errors across all 7 source files.

## Summary Table

| Verification Criterion | Expected | Observed | Status |
| :--- | :--- | :--- | :--- |
| Shivananjappa Park overnight resolution | Arrival clamped to Day 2 opening, dwell duration 1 hr | Arrival 09:00 Day 2, departure 10:00 Day 2, dwell 3600s | Verified |
| Solver, hydration, replan, multimodal clamping | Uniform clamping when wait spans overnight | Identical clamping logic across all four modules | Verified |
| Honest wait time recording | Wait seconds preserved, no fake lodging labels | wait_seconds is 57608s, total_wait_seconds is 57608s, no hotel label | Verified |
| Accommodation uncertainty | Assumption recorded without fake geometry | Explicit assumption added, zero fake coordinates or polylines | Verified |
| Same day pre opening wait slack | Arrival at transit arrival, no regression | Arrival at arr_time (09:15), wait 900s, Slice 1 preserved | Verified |
| Defensive graph and scheduler guards | Clamps legacy unadjusted arrival dates | Legacy Day 1 arrival clamped to Day 2 service window | Verified |
| Historical artifact preservation | Both live run JSON files preserved distinctly | Bengaluru (99 KB) and Mysuru (405 KB) preserved | Verified |
| Remote LAYA isolation | Disabled, zero remote network calls | LAYA endpoint remains disabled | Verified |
| Out of scope boundaries | Meals and nocturnal hours deferred | Intact and deferred to later slices | Verified |
| Static analysis and unit tests | Clean ruff, clean mypy, passing tests | 0 ruff errors, 0 mypy errors, 103 unit tests passed | Verified |

## Verdict

**Approve**.

The changes resolve the Shivananjappa Park overnight scheduling defect with high technical quality, surgical precision, zero architectural regressions, and complete fidelity to project guidelines and specification 0007.
