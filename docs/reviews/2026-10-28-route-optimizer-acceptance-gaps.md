# Review, main, 2026-10-28

**Reviewed by**: Senior Reviewer (inline evaluation, author confirmed Gemini 3.8 Flash)
**Scope**: 16 files, commit d0e98b0 (reconciled 0006 acceptance and replan lifecycle gaps)
**Verdict**: Approve

## Summary

This change closes the remaining lifecycle and acceptance gaps in specification 0006. It restores standard owner, operation, and idempotency key replay semantics, allows new replan attempts after terminal failures when no child route exists, generates realistic multimodal, partial, and infeasible outcomes through the real execution pipeline, and isolates test data to owned fixtures. Both review nits (depot return name sanitization and production environment safety check on the reset endpoint) have been resolved. All regression suites pass on live PostgreSQL and local browser environments.

## Resolved Nits

- ⚪ `backend/src/functions/route_optimizer/replan.py:238`: Cleaned home return stop naming so sequential replans strip any pre existing `Return to` prefix.
- ⚪ `backend/src/acceptance_server.py:123`: Added an explicit production environment guard returning HTTP 403 Forbidden to guarantee the test helper route can never run against production configurations.

## Strengths

- **Accurate idempotency replay and conflict detection**: The replan endpoint correctly inspects the combination of owner, operation, and idempotency key. Identical replays return the cached job response with HTTP 202 even after the child route has been committed, while conflicting payloads return HTTP 409 Conflict.
- **Resilient failure retry**: Single child lineage protection remains intact, but travelers are no longer permanently locked out if a solver task fails terminally or gets cancelled before committing a child route.
- **True pipeline generation in acceptance seeder**: Instead of inserting pre fabricated route records, the seeder now drives input places, verified parking hubs, and road closure claims through the actual API submission, state machine runner, worker solving, and completion reconciliation.
- **Acceptance data isolation**: Outbox event polling in the local acceptance worker explicitly filters by allowed owner identifiers and the route optimizer workflow. Database wide updates and un scoped acknowledgements have been eliminated.
- **Road closure avoidance with return preservation**: Browser acceptance scenario 3 and scenario 4 verify that replanned geometries route around the closure polygon, preserve completed stops, keep pedestrian walking legs to the sanctuary, and return safely to the parking hub and home depot.

## Test coverage

- `backend/tests/integration/test_route_replan_concurrency_slice2.py`: 3 passed in 24.65s on live PostgreSQL, verifying concurrent replan rejection (winner 202, loser 409), identical replay and changed payload conflict, and failed job retry when no child route exists.
- `backend/tests/unit/test_route_graph_state_machine_unit.py`: 11 passed in 1.96s, verifying route graph state machine nodes, interrupts, transitions, and checkpointer behavior.
- `frontend/e2e/route-optimization-e2e.spec.ts`: 8 passed in 8.0s, verifying mocked UI elements, progress banner, metrics, XSS popup escaping, and replan form interactions.
- `frontend/e2e/route-optimization-acceptance.spec.ts`: 7 passed in 1.9m, exercising the real local stack across multimodal, partial, infeasible, and tenant isolation scenarios.
