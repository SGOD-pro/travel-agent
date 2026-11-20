# Review, feat/0007-optimizer-dispatch, 2026-11-21

**Reviewed by**: Gemini 2.5 Pro (author on Claude 3.5 Sonnet)
**Scope**: 5 files, uncommitted changes over 36091b3824328896a81a2e6d45b17e2b03e7855d
**Verdict**: Approve

## Summary
This change implements bounded and semantically partitioned route optimizer dispatch for the trip planner workflow. It resolves the critical defects highlighted in the November 18 live journey report by isolating mandatory anchors from optional recommendations, partitioning hotels, restaurants, advisories, and services into editorial recommendations, calculating provider capacity dynamically from matrix cell bounds, and enforcing daily daytime opening windows across multi day trips. All review findings (Major, Minor, and Nits) have been fully addressed and verified with comprehensive unit and integration tests.

## Findings Resolution

### 🟢 Resolved: Candidate slot capacity check uses scheduled count, [graph.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L1375)
- Replaced the iteration index check with `if len(route_stops) < effective_max_stops:`.
- Candidates skipped due to opening window constraints no longer consume solver slots.
- Subsequent valid sightseeing candidates are scheduled up to the effective provider stop capacity.
- Verified by unit test `test_skipped_candidate_does_not_consume_provider_slot`.

### 🟢 Resolved: Destination day offset advancement, [graph.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L964)
- Updated `_compute_dest_day_offsets` to advance `current_offset = offsets[norm] + max(1, d.stay_days)` when explicit visit dates or day offsets are encountered.
- Ensures subsequent unconstrained destinations sequence deterministically after explicit dates.

### 🟢 Resolved: Geographic coordinate exception protection, [graph.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L1053)
- Protected candidate coordinate float conversions with `try...except (ValueError, TypeError):` in both proximity resolution and stop construction.
- Non numeric coordinate strings safely default to 0.0 or pass proximity matching without raising unhandled exceptions.
- Verified by unit test `test_malformed_coordinates_do_not_crash_dispatch`.

### 🟢 Resolved: Category authority normalization, [graph.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L890)
- Spaces and hyphens are normalized to underscores prior to set lookups, enabling multi word category matching directly against category authority constants.

### 🟢 Resolved: Configuration documentation, [config.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/src/config/config.py#L207)
- Documented in the Field description that Valhalla maps this constraint to its internal sources to targets `max_locations` error string ("Exceeded max locations: 100").

## Strengths
- Clean semantic partitioning: Segregating hotels, dining places, service stations, and road alerts from daytime sightseeing stops eliminates routing stop pollution and prevents misleading sightseeing visits to highway pins or hotels.
- Dynamic provider limit calculation: Computing `effective_max_stops = min(max_locations, isqrt(max_matrix_cells))` avoids hardcoded stop magic numbers while rigorously preventing Valhalla matrix cell limit breaches.
- Multi day window feasibility: Daily daytime opening hours mapped to local visit dates prevent 72 hour continuous overnight windows and avoid compressing multi day trips into Day 1.
- Zero Obscura regressions: Changes are surgically confined to optimizer dispatch and configuration, leaving all browser and Obscura packaging files completely untouched.

## Test Coverage
The suite comprises 10 unit tests in [test_optimizer_dispatch_semantics.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/tests/unit/test_optimizer_dispatch_semantics.py), 29 orchestrator unit tests in [test_trip_planner_route_optimizer_slice4.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/tests/unit/test_trip_planner_route_optimizer_slice4.py), 2 research dispatch integration tests in [test_destination_research_dispatch.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/tests/integration/test_destination_research_dispatch.py), and 1 complete database backed integration test in [test_optimizer_dispatch_integration.py](file:///home/swyra/projects/travel-agent-optimizer-dispatch/backend/tests/integration/test_optimizer_dispatch_integration.py). All 42 tests pass with zero lint or typecheck warnings.
