# Review, main, 2026-10-25

**Reviewed by**: Gemini 3.8 Flash (author on Gemini 3.8 Flash)
**Scope**: 16 files (12 modified, 4 untracked)
**Verdict**: Approve with nits

## Summary

Slice 1 of specification 0006 (`0006-route-graph-and-map-e2e`) implements the complete vertical orchestration slice for route optimization: backend submission with atomic idempotency reservation, dedicated `RouteOptimizerGraph` state machine execution under `TransactionBoundCheckpointer`, SQS worker task dispatch, external Valhalla solver constraint execution, receipt reconciliation, graph resumption without duplicate dispatch, durable job to route resolution, and authenticated RFC 7946 GeoJSON map retrieval.

All 16 behaviors verified cleanly on live PostgreSQL in the automated integration test suite (`test_route_graph_map_slice1.py`), and existing unit tests (`test_route_optimizer_slice6.py`, 15 passed) and integration tests (`test_route_optimizer_worker_slice6.py`, 2 passed) continue to pass without regressions.

## Nits

* ⚪ `backend/src/functions/orchestrator/routing/geojson.py:108`, `leg.geometry` is assumed to contain a dictionary conforming to GeoJSON LineString. If an external provider returns an encoded polyline string or non standard shape, the assembler falls back safely to an empty LineString, but logging a debug warning for unexpected geometry shapes would aid troubleshooting during provider integration.
* ⚪ `backend/src/functions/orchestrator/execution/reconciliation.py:137`, `_resolve_graph` instantiates a new compiled workflow on demand if not pre registered in `_workflow_graphs`. While it caches the compiled graph in `self._workflow_graphs[workflow]`, pre compiling both graphs at service initialization in `consumer.py` and `completion_consumer.py` keeps cold start latency bounded.

## Strengths

* **Clean separation of graph nodes and execution runner**: `RouteOptimizerGraph` nodes emit pure pending task specifications without directly modifying database tables or staging outbox events. `GraphRunnerService` owns all transactional outbox staging, task creation, and checkpoint flushing.
* **Deterministic idempotency and replay protection**: `POST /api/v1/optimization/routes` reserves idempotency atomically under `(owner_id, "route_optimization:solve", key)` before execution creation, returning identical `job_id` responses on replays and rejecting conflicting payloads with HTTP 409 Conflict.
* **Resumption without duplicate dispatch**: The interrupt boundary before `await_solver_result` halts execution on start; on resumption after worker completion, `await_solver_result_node` explicitly clears `pending_tasks`, guaranteeing zero duplicate SQS messages.
* **Durable job to route discovery**: Resolving route references via `GET /api/v1/optimization/routes/by-job/{job_id}` and `GET /api/v1/jobs/{job_id}` `result_ref` removes brittle timeline event scraping, enabling direct client map hydration.
* **Strict tenant isolation across all surfaces**: Cross tenant queries to `/by-job/{job_id}`, `/{route_id}/map`, and `/{route_id}` return explicit HTTP 403 Forbidden responses, preventing unauthorized entity discovery.
* **Standards compliant GeoJSON FeatureCollection**: RFC 7946 GeoJSON assembling PostGIS stop coordinates and JSONB leg line strings with simplestyle properties (`marker-color`, `marker-symbol`, `stroke`, `stroke-width`) and computed 4 element `bbox` (`[min_lon, min_lat, max_lon, max_lat]`).

## Test coverage

Test signal: `configured` (pytest with uv runner). Full test coverage across unit and live database integration suites:
* `backend/tests/integration/test_route_graph_map_slice1.py`: 1 comprehensive end to end lifecycle integration test covering submission, idempotency replay, graph runner start, outbox relay routing, worker solve with injected `MockValhallaClient`, receipt reconciliation, graph resumption without duplicate dispatch, job polling with `result_ref`, by-job discovery, GeoJSON map retrieval, and cross owner 403 isolation on live PostgreSQL.
* `backend/tests/unit/test_route_optimizer_slice6.py`: 15 passed unit tests verifying worker envelope parsing, cooperative cancellation, deadlines, and SQS routing.
* `backend/tests/integration/test_route_optimizer_worker_slice6.py`: 2 passed integration tests verifying worker execution under lease fencing and explicit failure when provider is unconfigured.
