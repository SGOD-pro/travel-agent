# Review, main, 2026-10-28

**Reviewed by**: Senior Reviewer (inline evaluation, author confirmed Gemini 3.8 Flash)
**Scope**: 7 files, tested commit `f2b1a44` (focused 0006 fixes: database safety, concurrent same-key replay, and full line-polygon closure avoidance)
**Verdict**: Approve

## Summary

This change closes all remaining lifecycle, concurrency, and acceptance gaps in specification 0006:
1. **Acceptance database safety**: Added `ACCEPTANCE_DATABASE_URL` configuration to isolate test databases, guarded startup seeding to fail closed in production, and strictly restricted fixture cleanup to owned user identifiers (`OWNED_USER_IDS`), protecting shared development databases.
2. **Concurrent same-key replay**: Re-checks idempotency under the parent route row lock and catches concurrent integrity collisions, guaranteeing that simultaneous identical replan requests return the exact same job.
3. **Changed-road proof**: Implemented full line-polygon intersection assertions testing both vertices and line segments against the closure polygon exterior ring, and demonstrated that the closure changes route geometry and distance.
4. **Verification records**: Identified the exact tested commit SHA (`f2b1a44`) and clean working tree state for every verification result.

All regression and acceptance suites pass on live PostgreSQL and local browser environments.

## Strengths

- **Acceptance database safety**: Startup seeding and reset endpoints fail closed in production, and all route/execution/idempotency cleanup queries filter strictly by `RouteModel.owner_id.in_(OWNED_USER_IDS)`, preventing accidental deletion of shared development data.
- **Concurrent same-key replay**: Re-checking idempotency under the `RouteModel` row lock guarantees that racing identical requests resolve to the same execution record rather than returning 409 conflict or duplicate key errors.
- **Full line-polygon spatial intersection verification**: Acceptance scenario 3 verifies that every line segment of every leg avoids intersecting the closure polygon exterior ring, with a companion unit test confirming that an intersecting direct route is rejected and detoured.
- **True pipeline generation in acceptance seeder**: Driven via API → LangGraph state machine → OR-Tools worker → completion reconciliation on live PostgreSQL.

## Test coverage

- `backend/tests/integration/test_route_replan_concurrency_slice2.py`: 4 passed in 26.27s on live PostgreSQL, verifying concurrent replan rejection (winner 202, loser 409), identical replay and changed payload conflict, failed job retry when no child route exists, and simultaneous identical replan requests returning the same job.
- `backend/tests/unit/test_route_optimizer_slice4.py`: 21 passed in 2.87s, verifying full line-polygon spatial intersection detection, direct route rejection, and closure route detour demonstration.
- `backend/tests/unit/test_route_graph_state_machine_unit.py`: 11 passed in 1.19s, verifying route graph state machine nodes, interrupts, transitions, and checkpointer behavior.
- `frontend/e2e/route-optimization-e2e.spec.ts`: 8 passed in 7.2s, verifying mocked UI elements, progress banner, metrics, XSS popup escaping, and replan form interactions.
- `frontend/e2e/route-optimization-acceptance.spec.ts`: 7 passed in 1.9m, exercising the real local stack across multimodal, partial, infeasible, database safety, and full line-polygon closure avoidance scenarios.

