# Review, main, 2026-10-26

**Reviewed by**: Gemini 3.8 Flash (author on Gemini 3.8 Flash)
**Scope**: 18 files, uncommitted changes
**Verdict**: Approve with nits

## Summary

Slice 3 of specification 0006 (`0006-route-graph-and-map-e2e`) implements infeasible and partial route handling, constraint diagnostic persistence, simplestyle failure map presentation, and clean separation between system job crashes and solver domain outcomes. Hard mandatory constraint violations persist requested stops as skipped records with bounded skip reasons and zero legs, while recording diagnostic strings in the PostgreSQL assumptions column. Partial routes with dropped optional venues render surviving legs with proper mode styling and mark dropped stops in red.

The integration test suite verified all three core scenarios on live PostgreSQL: mandatory constraint infeasibility with failure pins and job success, optional stop drops with surviving legs and partial status, and worker crash failure separation.

## Nits

* ⚪ `backend/src/functions/route_optimizer/replan.py:772`: When a mid trip replan proves infeasible, `infeasible_stops` sets `is_completed=False` for all stops. If the traveler had already finished stops prior to the replan, those stops should ideally retain `is_completed=True` so historical progress remains visible on the map.
* ⚪ `backend/src/functions/orchestrator/routing/geojson.py:76-78`: For infeasible routes with missing stop level skip reasons, fallback extraction pulls `(route.assumptions or [None])[0]`. Providing a concise default code such as `MANDATORY_CONSTRAINT_INFEASIBLE` when assumptions is empty prevents null skip reasons on failure pins.

## Strengths

* **Clean separation of solver outcomes from system failures**: Infeasible routes are treated as valid business outcomes where `execution.status = "SUCCEEDED"` and `route.status = "INFEASIBLE"`, preserving diagnostic records and stop coordinates without triggering spurious job retries or dead letter queue redrives.
* **Schema bound skip reason sanitization**: `RouteRepository.save_route` slices `s.skip_reason[:64]`, protecting against PostgreSQL string truncation exceptions when solver diagnostic strings exceed the column limit.
* **Standards compliant GeoJSON presentation**: Infeasible and skipped stops render with red simplestyle markers (`#dc2626`), distinct sequence symbols, and top level constraint diagnostics adhering to RFC 7946.
* **Lineage recursive CTE optimization**: `RouteRepository.get_route_lineage` resolves standalone ancestor and descendant version trees in a single PostgreSQL query using `WITH RECURSIVE`, resolving the performance nit from Slice 2.

## Test coverage

Test signal: `configured` (pytest with uv runner). Full test coverage across unit and live database integration suites:
* `backend/tests/integration/test_route_infeasible_partial_slice3.py`: 3 passed integration tests on live PostgreSQL covering mandatory window infeasibility lifecycle, optional stop drop partial route lifecycle, and system crash failure separation (all passed in 89.47s).
* `backend/tests/integration/test_route_graph_map_slice1.py` and `backend/tests/integration/test_route_replan_lineage_slice2.py`: 2 passed regression integration tests on live PostgreSQL verifying core graph execution, replan engine, and lineage trees (passed in 145.06s).
* `backend/tests/unit/test_route_optimizer_slice6.py` and `backend/tests/unit/test_route_optimizer_slice5.py`: 24 passed unit tests verifying worker parsing, cancellation, deadlines, replanning, and concurrency conflicts (passed in 52.14s).
