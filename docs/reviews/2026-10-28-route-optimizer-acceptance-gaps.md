# Review, main, 2026-10-28

**Reviewed by**: Senior Reviewer (inline evaluation, author confirmed Gemini 3.8 Flash)
**Scope**: 5 files, tested commit `684e68dd1dd724bbf18905eb9cd33cb215a89e15` (short: `684e68d`)
**Verdict**: Approve

## Summary

This change resolves all remaining findings from the previous assessment:
1. **Acceptance database safety**: Replaced broad owner based deletion with demonstrably fixture owned targeting. `ExecutionModel` cleanup deletes only records tied to `OWNED_TRIP_IDS` or `route_exec_ids`, preserving standalone developer executions with null trip identifiers. `ApiIdempotencyModel` cleanup deletes only designated fixture keys and canonical route keys, preserving developer idempotency records.
2. **Concurrent same key replay with different parents**: Wrapped the idempotency insertion through `session.flush()` and `commit()` in an `IntegrityError` handler. Concurrent requests using the same key across different parent routes now reliably return HTTP 409 Conflict rather than escaping as unhandled errors during early flush. Verified with a new live PostgreSQL integration test.
3. **Changed road proof**: Replaced the custom segment intersection helper with a complete 4-orientation and onSegment algorithm that accurately catches collinear overlaps and touching boundary segments. Scenario 3 proves that the road closure changes the route by verifying version history distance deltas, replan origin metadata, detour leg intermediate vertices, and coordinate deviation from the base route.
4. **Verification records**: Identified tested commit `684e68d` and recorded fresh evidence from test runs executed directly in this verification cycle.

## Strengths

- **Demonstrably fixture owned database isolation**: The cleanup queries in `acceptance_seeder.py` and `acceptance_server.py` no longer broadly wipe records by user identifier. Standalone developer executions and developer idempotency records remain untouched on shared development databases.
- **Early flush collision protection**: Handling unique constraint collisions during `session.flush()` ensures consistent 409 conflict responses for concurrent same key requests across distinct parents.
- **Comprehensive spatial intersection testing**: The updated `segmentsIntersect` helper correctly identifies collinear overlaps and touching boundary segments, and the browser test asserts that all leg segments avoid the closure polygon with zero intersections.

## Test coverage

- `backend/tests/integration/test_route_replan_concurrency_slice2.py`: 5 passed in 40.18s on live PostgreSQL, verifying single child race rejection, cached job replay, changed payload conflict, failed job retry, simultaneous identical replay, and different parent key collision 409 response.
- `backend/tests/unit/test_route_optimizer_slice4.py`: 21 passed in 2.23s, including full spatial intersection detection and detour route alteration.
- `backend/tests/unit/test_route_graph_state_machine_unit.py`: 11 passed in 1.19s, verifying route graph state machine nodes, interrupts, transitions, and checkpointer behavior.
- `frontend/e2e/route-optimization-e2e.spec.ts`: 8 passed in 6.8s, verifying mocked UI elements, progress banner, metrics, XSS popup escaping, and replan form interactions.
- `frontend/e2e/route-optimization-acceptance.spec.ts`: 7 passed in 2.5m on real local frontend, backend, PostgreSQL, runner, and worker with live pipelines, strict database fixture isolation, full collinear and touching boundary checks, and detour route change proof.

