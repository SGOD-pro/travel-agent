# Review, main, 2026-10-28

**Reviewed by**: Senior Reviewer (inline evaluation, author confirmed Gemini 3.8 Flash)
**Scope**: 8 files, tested commit `c84b7bb9222c1592c300fbfd740c02c676ec8007` (short: `c84b7bb`)
**Verdict**: Approve

## Summary

This change resolves all remaining acceptance environment findings identified in the review of commit `5162eb4`:

1. **Acceptance database safety and isolation**:
   - Configured dedicated PostgreSQL database `acceptance_db` on the live Aiven PostgreSQL cluster, migrated through Alembic head revision `0014_enforce_single_child_replan`.
   - Created `backend/src/acceptance_database.py` providing URL normalization across connection formats, schemes (`postgres://` versus `postgresql+asyncpg://`), default port 5432 omissions, and query parameter variations.
   - Removed all fallback from `ACCEPTANCE_DATABASE_URL` to `DATABASE_URL`. The acceptance server, fixture seeder, background worker, and fixture reset endpoints now fail closed with `RuntimeError` or `HTTPException` if `ACCEPTANCE_DATABASE_URL` is omitted, blank, or resolves to the same database as `DATABASE_URL`.
   - Applied FastAPI dependency override `app.dependency_overrides[get_session]` so that every acceptance HTTP endpoint runs against the dedicated acceptance database.
   - Added trip identifier filtering in `LocalAcceptanceWorker` (`ExecutionModel.trip_id.in_(self._allowed_trip_ids)`), guaranteeing the worker never consumes unrelated developer executions sharing the default user identifier.

2. **Fixture cleanup ordering and data preservation**:
   - Reordered cleanup in `acceptance_seeder.py` and `acceptance_server.py:reset_replan_fixtures` to collect `route_exec_ids` prior to deleting `RouteModel` records.
   - Deletes associated `JobEventModel`, `OutboxEventModel`, `TaskModel`, `WorkerResultModel`, and `ExecutionModel` rows.
   - Restricted cleanup strictly to demonstrably fixture owned records (`OWNED_TRIP_IDS`, `CANONICAL_ROUTE_IDS`, child replans, fixture idempotency keys, and fixture places/claims).
   - Created focused test `backend/tests/unit/test_acceptance_isolation_safety.py` proving that fixture records are cleaned up while unrelated executions, tasks, outbox events, results, job events, and idempotency keys remain completely intact across repeated setup and cleanup passes.

3. **Clarified changed road evidence**:
   - Retained the complete 4-orientation line segment intersection algorithm including touching and collinear segment checks.
   - Maintained unit test `test_changed_road_closure_full_intersection_and_route_change` proving that with identical origin and destination coordinates, road closure exclusion alters route geometry, increases travel distance, and eliminates closure polygon intersections.
   - Updated browser acceptance documentation and comments in `frontend/e2e/route-optimization-acceptance.spec.ts` to distinguish mid trip origin shifts from the controlled same-input unit test proof.

4. **Reconciled evidence**:
   - Preserved both the concurrent same parent replay regression (`test_concurrent_same_key_replan_returns_same_job`) and different parent key collision regression (`test_concurrent_same_key_different_parent_replan_conflicts`).
   - Documented actual tested commit `c84b7bb` with every independently executed check and command recorded directly from the current test environment.

## Strengths

- **Strict fail closed isolation**: The validation logic catches equivalent database targets even when drivers or query parameters differ, preventing accidental reuse of the main application database during acceptance testing.
- **Ordered cascade cleanup**: Collecting execution identifiers before removing route rows resolves the previous ordering bug and guarantees clean removal of child executions.
- **Tenant and trip fenced worker**: Restricting outbox polling to allowed trip identifiers prevents worker crosstalk with other tests or developer sessions on shared user accounts.

## Test coverage

- `backend/tests/unit/test_acceptance_isolation_safety.py`: 6 passed in 44.82s, proving missing configuration failure, database reuse rejection, worker trip filtering, and fixture cleanup ordering without touching unrelated data.
- `backend/tests/unit/test_route_optimizer_slice4.py`: 21 passed in 2.23s, including coordinate validation, Valhalla client retries, and controlled same-input closure avoidance.
- `backend/tests/unit/test_route_graph_state_machine_unit.py`: 11 passed in 1.19s, verifying route graph state machine nodes, interrupts, transitions, and checkpointer behavior.
- `backend/tests/integration/test_route_replan_concurrency_slice2.py`: 2 passed in 79.31s on live PostgreSQL, verifying concurrent same parent replay returns identical jobs and different parent collisions return HTTP 409 Conflict.
- `frontend/e2e/route-optimization-acceptance.spec.ts`: 7 passed in 4.4m on live Next.js, FastAPI, dedicated Aiven PostgreSQL `acceptance_db`, and injected MockValhallaClient.
- `frontend/e2e/route-optimization-e2e.spec.ts`: 8 passed in 9.7s verifying UI elements, progress banners, map rendering, and version switching.
