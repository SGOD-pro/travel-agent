# Review, main, 2026-10-26

**Reviewed by**: Gemini 3.8 Flash (author on Gemini 3.8 Flash)
**Scope**: 17 files (13 modified, 4 untracked)
**Verdict**: Approve with nits

## Summary

Slice 2 of specification 0006 (`0006-route-graph-and-map-e2e`) implements the mid trip replanning engine, parent lineage tracking, version comparison deltas, and the version history endpoint. The implementation enforces optimistic concurrency checks during replan submission, links previous routes via `parent_route_id`, executes replans under `ROUTE_OPTIMIZER_GRAPH` with live traveler coordinates, persists updated routes with incremented version numbers, and exposes version lineage trees through `GET /api/v1/optimization/routes/{route_id}/versions`.

All 18 lifecycle assertions verified cleanly on live PostgreSQL in the automated integration test suite (`test_route_replan_lineage_slice2.py`), and all unit tests (`test_route_optimizer_slice6.py`, `test_route_optimizer_slice5.py`, 24 passed) and Slice 1 integration tests (`test_route_graph_map_slice1.py`, 1 passed) pass without regressions.

## Nits

* ⚪ `backend/src/functions/orchestrator/routing/repository.py:383-408`: Standalone route lineage resolution uses iterative BFS queries per node (`child_stmt` inside `while queue`). For routes with multiple replans, this executes multiple round trips to the database. Using a PostgreSQL recursive CTE (`WITH RECURSIVE`) would fetch the entire ancestor and descendant tree in a single query.
* ⚪ `backend/src/functions/api/routes/optimization.py:307-316`: In `get_route_versions`, dropped stop IDs are identified by comparing stop names between parent and current versions (`dropped_names = parent_active_names - current_active_names`). If a stop was omitted entirely from the new version without a skipped record in `v.stops`, filtering `[s.id for s in (v.stops or []) if s.name in dropped_names]` returns empty because the omitted stop only exists on `parent_v.stops`. Pulling the ID from `parent_v.stops` when the stop is omitted from `v.stops` ensures client consumers can always trace which parent stop was dropped.

## Strengths

* **Strict optimistic concurrency check**: `POST /api/v1/optimization/routes/{route_id}/replans` compares `request.expected_previous_version` against `existing_route.route_version` and returns HTTP 409 Conflict with informative detail, preventing stale or conflicting updates when multiple replans occur concurrently.
* **Deterministic parent route lineage**: In `RouteReplanService.replan_and_persist`, `parent_route_id` is assigned and persisted to PostgreSQL, establishing an immutable chain linking child versions back to their root ancestors.
* **Graph orchestration parity**: The LangGraph state machine handles replan tasks via `initialize_route_node` and `RouteReplanRequest` validation, ensuring that mid trip replans reuse the same transactional checkpointing, interrupt boundaries, and reconciliation flow as initial route solves.
* **Bi directional lineage discovery**: Querying `GET /api/v1/optimization/routes/{route_id}/versions` from either the root version or any child version resolves the full version history tree with accurate distance and travel duration deltas.
* **Strict tenant isolation across all surfaces**: Cross tenant queries to both `POST /{route_id}/replans` and `GET /{route_id}/versions` return explicit HTTP 403 Forbidden responses, preventing unauthorized entity discovery or unauthorized route manipulation.

## Test coverage

Test signal: `configured` (pytest with uv runner). Full test coverage across unit and live database integration suites:
* `backend/tests/integration/test_route_replan_lineage_slice2.py`: 1 comprehensive end to end lifecycle integration test covering initial route submission, graph execution, worker solve, replan submission, optimistic concurrency conflict rejection, replan graph execution, worker replan solve, reconciliation resume, job discovery, versions lineage retrieval, and cross owner 403 isolation on live PostgreSQL (all 18 lifecycle assertions passed in 83.03s).
* `backend/tests/unit/test_route_optimizer_slice6.py`: 15 passed unit tests verifying worker envelope parsing, cooperative cancellation, deadlines, and SQS routing.
* `backend/tests/unit/test_route_optimizer_slice5.py`: 9 passed unit tests verifying replanning logic, optimistic concurrency conflicts, SSRF coordinate validation, and optional stop drops.
* `backend/tests/integration/test_route_graph_map_slice1.py`: 1 passed end to end integration test verifying the core vertical slice on live PostgreSQL.
