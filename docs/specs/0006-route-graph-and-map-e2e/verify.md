# Verify: Route Graph, Map GeoJSON, and End to End Vertical Slice, spec 0006, updated 2026-10-26
_Steps derived from spec 0006 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Acceptance criteria (Slice 1: Core Vertical Slice)

- [x] AC-1: `RouteOptimizerGraph` compiles as a dedicated LangGraph state machine with nodes for route initialization, solver task dispatch, worker result waiting, and route finalization, persisting checkpoint state safely using `TransactionBoundCheckpointer`. (Slice 1 complete)
- [x] AC-2: Asynchronous endpoint `POST /api/v1/optimization/routes` reserves owner scoped API idempotency atomically at acceptance, initiates `RouteOptimizerGraph` under execution workflow `ROUTE_OPTIMIZER_GRAPH`, returns HTTP 202 Accepted with canonical `job_id`, and enables status polling through `GET /api/v1/jobs/{job_id}`. (Slice 1 complete)
- [x] AC-3: Execution lifecycle conforms to dispatch, interrupt, worker completion, reconciliation, resume, and completion. Workflow `ROUTE_OPTIMIZER_GRAPH` transitions from `WAITING_TASKS` to `RUNNING` via `EXECUTION_RESUME` upon receipt confirmation, resuming the graph to load the persisted route using `RouteOptimizerWorkerOutput.route_id` without duplicate dispatch before marking the execution `SUCCEEDED`. (Slice 1 complete)
- [x] AC-4: Durable job to route discovery is provided via `GET /api/v1/optimization/routes/by-job/{job_id}` and `GET /api/v1/jobs/{job_id}` `result_ref` without relying on paginated timeline events. (Slice 1 complete)
- [x] AC-5: Endpoint `GET /api/v1/optimization/routes/{route_id}/map` returns a standards compliant GeoJSON FeatureCollection assembling PostGIS stop geometries (`route_stops.location`) and JSONB leg geometries (`route_legs.geometry`), styled with simplestyle presentation properties and computed `bbox`. (Slice 1 complete)
- [x] AC-6 (migration): Database migration adds `parent_route_id` nullable foreign key and index to table `routes`. (Slice 1 complete)
- [x] AC-9: Strict tenant ownership fencing is enforced across all route endpoints, returning HTTP 403 Forbidden for cross tenant access attempts and HTTP 404 Not Found when a route does not exist. (Slice 1 complete)

## Acceptance criteria (Slice 2: Replan Engine and Lineage)

- [x] AC-6 (replan engine): Mid trip replanning endpoint `POST /api/v1/optimization/routes/{route_id}/replans` accepts live traveler coordinates, replan timestamp, and completed stop IDs. Enforces optimistic concurrency checks against `expected_previous_version`, returning HTTP 409 Conflict on mismatch. Generates a new route version with `parent_route_id` set to the previous route and increments `route_version`. (Slice 2 complete)
- [x] AC-7: Lineage endpoint `GET /api/v1/optimization/routes/{route_id}/versions` returns complete version history tracing root to child replans, computing distance deltas, duration deltas, dropped stop IDs, and replan origin coordinates. (Slice 2 complete)
- [x] AC-9 (replan and versions): Enforces tenant isolation on replan submission and version history endpoints, returning HTTP 403 Forbidden on cross tenant attempts. (Slice 2 complete)

## Acceptance criteria (Slice 3: Infeasible and Partial Route Handling)

- [x] AC-8: Infeasible or partial routes persist requested stops, skip reasons, and constraint diagnostics in PostgreSQL, returning HTTP 200 OK from the map endpoint with stop points marked with failure properties and top level error metadata, keeping job failure distinct from a valid infeasible route outcome. (Slice 3 complete)

## Acceptance criteria (Slice 4: Frontend CorridorMap GeoJSON Integration)

- [x] AC-10: Frontend Leaflet CorridorMap component consumes GeoJSON FeatureCollection directly, rendering multi color paths (driving in blue, walking in emerald, detours in amber), custom stop pins with visit sequence numbers, parking icons, safe text escaped popups, and proper map attribution for OpenStreetMap and CARTO. (Slice 4 complete)
- [x] AC-11: Automated browser end to end suite with Playwright across local frontend, backend, and PostgreSQL, alongside a connected Valhalla verification gate. (Slice 5 complete)

## Specced surfaces (Slice 1)

- [x] Database migration `0013_add_parent_route_id.py` in `backend/infrastructure/migrations/versions/`:
  - Adds nullable `parent_route_id` foreign key referencing `routes.id` with `ondelete="SET NULL"`.
  - Creates index `ix_routes_parent_route_id` on `routes(parent_route_id)`.
  - Applied and verified against live PostgreSQL schema.
- [x] Domain contracts in `backend/src/contracts/routing.py`:
  - `GeoJsonFeature` RFC 7946 feature model with geometry and simplestyle properties.
  - `RouteMapGeoJsonResponse` FeatureCollection model with `bbox`, features, and top level route metadata.
  - `parent_route_id` added to `OptimizedRoute`.
- [x] GeoJSON assembler service in `backend/src/functions/orchestrator/routing/geojson.py`:
  - `assemble_route_map_geojson(route)` compiles PostGIS stop coordinates `[lon, lat]` and JSONB leg polylines.
  - Generates simplestyle attributes (`marker-color`, `marker-symbol`, `stroke`, `stroke-width`, `stroke-dasharray`).
  - Calculates and populates 4-element coordinate `bbox: [min_lon, min_lat, max_lon, max_lat]`.
- [x] LangGraph RouteOptimizerGraph in `backend/src/functions/orchestrator/graphs/route_optimizer/`:
  - `initialize_route_node`: validates request and emits pending task specifications.
  - `await_solver_result_node`: interrupt target (`interrupt_before=[RouteOptimizerNode.AWAIT_SOLVER_RESULT]`); clears pending tasks on resume to avoid duplicate dispatch.
  - `finalize_route_node`: extracts `RouteOptimizerWorkerOutput.route_id` and loads owner scoped `RouteModel`.
- [x] Graph runner execution engine in `backend/src/functions/orchestrator/execution/runner.py`:
  - Supports `ROUTE_OPTIMIZER_GRAPH` workflow dispatch and checkpoint resumption.
  - Atomically creates `TaskModel`, stages `TASK_DISPATCH` in outbox, and transitions to `WAITING_TASKS`.
- [x] Completion reconciliation in `backend/src/functions/orchestrator/execution/reconciliation.py`:
  - Resolves `ROUTE_OPTIMIZER_GRAPH` checkpoint and merges worker task outputs.
  - Transitions execution from `WAITING_TASKS` to `RUNNING` and stages `EXECUTION_RESUME` outbox event.
- [x] API endpoints in `backend/src/functions/api/routes/optimization.py` & `jobs.py`:
  - `POST /api/v1/optimization/routes`: reserves idempotency, creates `ROUTE_OPTIMIZER_GRAPH` execution, stages `EXECUTION_START`, returns 202 with `job_id`.
  - `GET /api/v1/optimization/routes/by-job/{job_id}`: durable route retrieval by execution ID with tenant fencing.
  - `GET /api/v1/optimization/routes/{route_id}/map`: returns RFC 7946 GeoJSON FeatureCollection with tenant fencing.
  - `GET /api/v1/jobs/{job_id}`: returns `result_ref` with `route_id` and `map_url` on `SUCCEEDED`.
- [x] End to end integration test suite in `backend/tests/integration/test_route_graph_map_slice1.py`:
  - Verified on live PostgreSQL covering API acceptance, idempotency replay, graph runner start, outbox queue routing, worker solve with injected `MockValhallaClient`, completion reconciliation, graph resumption without duplicate dispatch, job polling with `result_ref`, by-job discovery, GeoJSON map retrieval with simplestyle styling, and cross tenant 403 isolation.

## Specced surfaces (Slice 2)

- [x] Domain contracts in `backend/src/contracts/routing.py`:
  - `RouteVersionItem` model exposing `route_id`, `route_version`, `parent_route_id`, `created_at`, `status`, `total_distance_m`, `total_travel_seconds`, `distance_delta_m`, `duration_delta_seconds`, `dropped_stop_ids`, and `replan_origin`.
  - `RouteVersionHistoryResponse` model exposing `root_route_id`, `current_route_id`, and `versions` list.
- [x] Persistence repository in `backend/src/functions/orchestrator/routing/repository.py`:
  - `get_route_lineage(route_id, *, owner_id)` resolves version lineage for trip scoped and standalone routes by climbing upwards to root and descending to all child replans using a recursive PostgreSQL CTE.
  - Enforces single child replan policy: checks existing children and catches PostgreSQL unique constraint violations to raise `RouteVersionConflictError`.
- [x] Database migration `0014_enforce_single_child_replan.py` in `backend/infrastructure/migrations/versions/`:
  - Adds partial unique index `uq_routes_parent_route_id` on `routes(parent_route_id) WHERE parent_route_id IS NOT NULL`.
  - Applied and verified against live PostgreSQL schema.
- [x] Replan service in `backend/src/functions/route_optimizer/replan.py`:
  - Sets `parent_route_id = previous_route.id` on `OptimizedRoute`.
  - Accepts `execution_id` parameter and stores both `execution_id` and `parent_route_id` during persistence.
- [x] Worker service in `backend/src/functions/route_optimizer/service.py`:
  - Passes `envelope.execution_id` down to `replan_and_persist`.
- [x] LangGraph RouteOptimizerGraph in `backend/src/functions/orchestrator/graphs/route_optimizer/graph.py`:
  - `initialize_route_node` recognizes replan actions and payload structures, validates `RouteReplanRequest`, and emits tasks with `action="replan"`.
- [x] API endpoints in `backend/src/functions/api/routes/optimization.py`:
  - `POST /api/v1/optimization/routes/{route_id}/replans`: validates tenant ownership, locks parent route row via `with_for_update()`, checks for existing children, checks optimistic concurrency returning HTTP 409 Conflict on mismatch, reserves idempotency key atomically, creates `ROUTE_OPTIMIZER_GRAPH` execution, and stages `EXECUTION_START`.
  - `GET /api/v1/optimization/routes/{route_id}/versions`: verifies tenant ownership, retrieves complete lineage tree, computes distance and duration deltas against immediate parents, identifies dropped stops, and returns `RouteVersionHistoryResponse`.
- [x] Replan concurrency regression suite in `backend/tests/integration/test_route_replan_concurrency_slice2.py`:
  - Verified on live PostgreSQL testing atomic acceptance rejection of concurrent replans (202 for winner, 409 for loser), stale replan rejection (409), and database constraint enforcement (`RouteVersionConflictError`).
- [x] End to end integration test suite in `backend/tests/integration/test_route_replan_lineage_slice2.py`:
  - Verified on live PostgreSQL covering initial route creation, graph execution, worker solve, replan submission, optimistic concurrency conflict rejection, replan graph execution, worker replan solve, reconciliation resume, job discovery, versions lineage retrieval, and cross tenant 403 isolation.

## Specced surfaces (Slice 3)

- [x] Repository persistence and data models in `backend/src/functions/orchestrator/routing/`:
  - `RouteRepository.save_route` merges diagnostics into `assumptions` and truncates `skip_reason` to 64 characters to strictly adhere to the `route_stops` schema.
  - `RouteModel.to_optimized_route` populates `diagnostics` from `assumptions`.
- [x] Worker and replan services in `backend/src/functions/route_optimizer/`:
  - `RouteOptimizerWorkerService.execute_task` preserves all requested stops in `dropped_stops` with `is_skipped = True`, categorized `skip_reason`, and empty `legs` array on infeasible outcomes.
  - `RouteReplanService.replan_and_persist` preserves `is_completed = True` on already finished stops during infeasible replans, placing remaining impossible stops in `dropped_stops` with zero legs.
- [x] GeoJSON map assembler in `backend/src/functions/orchestrator/routing/geojson.py`:
  - Returns HTTP 200 OK with GeoJSON FeatureCollection when route status is `INFEASIBLE` or `PARTIAL`.
  - Marks infeasible and skipped stop points with `marker-color = "#dc2626"`, `is_skipped = True`, and `skip_reason`, falling back to `MANDATORY_CONSTRAINT_INFEASIBLE` when assumptions is empty.
  - Marks visited and completed stops with `marker-color = "#059669"` (or `#2563eb`), preserving `is_completed = True`.
  - Produces zero leg features for infeasible routes and valid styled LineString features for partial route surviving legs.
  - Exposes top level `diagnostics` array with constraint explanations.
- [x] LangGraph RouteOptimizerGraph in `backend/src/functions/orchestrator/graphs/route_optimizer/graph.py`:
  - Node `finalize_route_node` accepts `config: RunnableConfig` and resolves `session_factory` from configurable parameters.
  - Preserves infeasible routes and marks execution `SUCCEEDED` when a route record exists, keeping system job failure separated from valid infeasible route calculations.
- [x] End to end integration test suite in `backend/tests/integration/test_route_infeasible_partial_slice3.py`:
  - Verified on live PostgreSQL across 3 scenarios: mandatory window infeasibility outcome with failure pins, optional stop drop partial route with surviving legs, and system crash failure separation.
- [x] Unit test suite in `backend/tests/unit/test_route_map_geojson_slice3.py`:
  - Verified fallback skip reason code generation and completed stop preservation during infeasible replan persistence.

## Specced surfaces (Slice 4)

- [x] TypeScript contracts in `frontend/src/types/routing.ts`:
  - RFC 7946 GeoJSON Feature, FeatureCollection, Stop, Leg, and Restriction properties.
  - Lineage and version comparison interfaces.
  - Job status response interface.
- [x] Enhanced CorridorMap component in `frontend/src/components/map/CorridorMap.tsx` and `CorridorMapWrapper.tsx`:
  - Consumes `RouteMapGeoJsonResponse` GeoJSON FeatureCollection directly.
  - Renders multi color paths: driving in blue (`#3b82f6`), walking in emerald (`#10b981`), detours in amber (`#f59e0b`).
  - Renders custom stop pins with sequence numbers, parking icons (`"P"`), and red failure pins (`#dc2626`).
  - Constructs safe text escaped popups using DOM `textContent` APIs, preventing XSS injection.
  - Strictly preserves OpenStreetMap and CARTO tile layer attribution.
  - Displays trip summary card with total distance, elapsed duration, fuel cost estimate, and status badge (`FEASIBLE`, `PARTIAL`, `INFEASIBLE`).
- [x] Route optimization polling hook in `frontend/src/lib/useRouteOptimization.ts`:
  - Manages job submission with idempotency keys.
  - Implements exponential backoff polling (1s, 2s, 3s, max 5s) against `GET /api/v1/jobs/{job_id}`.
  - Supports automatic refresh recovery from URL query parameters and session storage.
  - Discovers routes via `result_ref` and `by-job/{job_id}` fallback.
  - Supports mid trip replanning submission and version history retrieval.
- [x] Interactive route page in `frontend/src/app/trips/[id]/route/page.tsx`:
  - Interactive CorridorMap with real GeoJSON FeatureCollection.
  - Mid trip dynamic replan form with device GPS geolocation and preset coordinates.
  - Completed stops checklist for freezing visited stops during replans.
  - Version selector dropdown for switching between root and detoured routes.
  - Breadcrumb navigation linked from `frontend/src/app/trips/[id]/page.tsx`.

## Specced surfaces (Slice 5 & Testing Distinction)

The route optimization test suite is explicitly divided into three distinct operational levels:

### Level 1: Mocked UI Regression Suite
- Location: `frontend/e2e/route-optimization-e2e.spec.ts`
- Purpose: Fast user interface rendering and interaction regression checking. Intercepts backend routes using Playwright route mocking.
- Scenarios tested:
  - Scenario 1: Base layout, breadcrumbs, metrics, and replan form layout.
  - Scenario 2: Polling progress banner and status transitions during solver execution.
  - Scenario 3: GeoJSON FeatureCollection rendering with multi color paths, numbered pins, parking icons, metrics, and OpenStreetMap and CARTO tile attribution.
  - Scenario 4: Safe text escaped popups verifying immunity against Cross Site Scripting.
  - Scenario 5: Dynamic mid trip replan form submission with completed stops checklist.
  - Scenario 6: Version lineage selector switching between root and detoured routes.
  - Scenario 7: Infeasible route failure pins with red styling, skip reasons, and diagnostic banner.
  - Scenario 8: Clean browser console execution without errors or exceptions.

### Level 2: Full Stack Browser Acceptance Suite
- Location: `frontend/e2e/route-optimization-acceptance.spec.ts`
- Purpose: Real local end to end browser acceptance proving user flows across live components.
- Stack under test: Real Next.js frontend on port 3000, real FastAPI backend on port 8000, live PostgreSQL database, real LangGraph execution runner (`GraphRunnerService`), worker consumer (`process_record`), and completion reconciliation (`CompletionReconciliationService`).
- Provider injection: Injects only the routing provider (`MockValhallaClient`). Does not intercept application APIs via `page.route()`.
- Controlled test principals: Dev default principal (`USER_A_ID: 00000000-0000-0000-0000-000000000001`) and unauthorized tenant (`USER_B_ID: 99999999-9999-9999-9999-999999999999`), preserving the development auth decision.
- Scenarios demonstrated:
  - Scenario 1: Live Submission, Real Polling, and Map Retrieval.
  - Scenario 2: Refresh Recovery restoring active route from URL query and session storage.
  - Scenario 3: Mid Trip Changed Road Replan and Version Switching Lineage.
  - Scenario 4: Multimodal Route Execution (Parking Hub, Walking Leg, Vehicle Return, Drive Home).
  - Scenario 5: PARTIAL Outcome (Dropped Optional Stop and Advisory Diagnostics).
  - Scenario 6: INFEASIBLE Outcome (No Verified Parking Hub in Pedestrian Precinct).
  - Scenario 7: Controlled Test Principals & Owner Isolation (HTTP 403 Forbidden checks).

### Level 3: Connected Valhalla Verification Gate
- Location: `backend/tests/integration/test_route_graph_valhalla_connected_slice5.py`
- Purpose: Integration verification with a live external Valhalla routing provider instance.
- Gating: Gated by `RUN_CONNECTED_VALHALLA_TESTS=1` and `VALHALLA_ENDPOINT_URL`. Skips cleanly in 0.13s when disabled or unconfigured.
- Behavior when enabled: Runs full pipeline with trip seeding, route submission, LangGraph execution, worker solving with real Valhalla client, reconciliation, and verifies RFC 7946 GeoJSON FeatureCollection with decoded turn by turn LineString coordinates (> 50 points).

## Verified commands and evidence

Tested commit: `66eb322` (clean working tree on main)

```bash
# 1. Database schema migration verification (single child replan constraint)
cd backend && .venv/bin/alembic upgrade head
# Tested Commit: 66eb322
# Applied: 0014_enforce_single_child_replan (uq_routes_parent_route_id partial unique index)

# 2. Acceptance database safety and isolation unit test execution
cd backend && PYTHONPATH=src .venv/bin/pytest tests/unit/test_acceptance_isolation_safety.py -v
# Output: 10 passed in 25.00s on live dedicated acceptance database (user agnostic database equivalence, host alias resolution, indeterminate targets fail closed, conftest checkpointer isolation guard, worker trip filtering, and fixture cleanup ordering with 100% unrelated data preservation)

# 3. Integration checkpointer protection suite on isolated test database
cd backend && PYTHONPATH=src .venv/bin/pytest tests/integration/test_checkpoint_protection.py -v
# Output: 5 passed in 72.88s on live PostgreSQL (persisted state recovery, expired runner takeover, stale write rejection, buffer isolation, redispatch, and checkpointer guard protecting application database)

# 4. Unit test regression execution (graph state machine and closure avoidance with full line polygon proof)
cd backend && PYTHONPATH=src .venv/bin/pytest tests/unit/test_route_graph_state_machine_unit.py tests/unit/test_route_optimizer_slice4.py -v
# Output: 32 passed in 2.26s (including full line polygon intersection proof and closure route change demonstration)

# 5. Replan concurrency regression execution (simultaneous identical replay, different parent collision)
cd backend && PYTHONPATH=src .venv/bin/pytest tests/integration/test_route_replan_concurrency_slice2.py -k "test_concurrent" -v
# Output: 2 passed in 12.97s on live PostgreSQL (simultaneous identical replan returns same job, different parent shared key collision returns 409)

# 6. Level 1: Mocked Playwright UI regression suite
cd frontend && npx playwright test e2e/route-optimization-e2e.spec.ts
# Output: 8 passed in 11.0s

# 7. Level 2: Full stack browser acceptance suite on dedicated database (AC-11)
cd frontend && npx playwright test e2e/route-optimization-acceptance.spec.ts
# Output: 7 passed in 4.4m on real local frontend, backend, dedicated Aiven PostgreSQL acceptance database, runner, and worker with live pipelines, full collinear and touching segment polygon intersection, and detour route change proof

# 8. Level 3: Connected Valhalla verification gate (clean skip when unconfigured)
cd backend && PYTHONPATH=src .venv/bin/pytest tests/integration/test_route_graph_valhalla_connected_slice5.py -v
# Output: 1 skipped in 0.14s

# 9. Level 3: Connected Valhalla verification gate (live provider execution when enabled)
cd backend && RUN_CONNECTED_VALHALLA_TESTS=1 VALHALLA_ENDPOINT_URL=http://localhost:8002 PYTHONPATH=src .venv/bin/pytest tests/integration/test_route_graph_valhalla_connected_slice5.py -v
# Output: 1 passed in 46.53s
```

## Known limitations

1. Browser acceptance tests run against local Next.js and FastAPI instances connected to live cloud PostgreSQL. Because each LangGraph execution stages outbox events and cycles through graph runner and worker reconciliation over the network, full graph solving runs take approximately 50 to 60 seconds per execution cycle.
2. Connected Valhalla live tests require an active external Valhalla instance with geographic OSM tiles loaded. They are safely skipped in environments where Valhalla is not deployed.
3. The development authentication mode uses encoded bearer tokens with subject principals; production deployment requires standard asymmetric JWT verification through the identity provider.

