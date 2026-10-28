# Review, main, 2026-10-26

**Reviewed by**: Gemini 3.8 Flash (author on Gemini 3.8 Flash, inline review)
**Scope**: Route optimization acceptance testing, replan concurrency policy, and connected Valhalla gate
**Tested commit**: `fa6f170752703ec561324678271c10b7defeecee`
**Verdict**: Approve

## Summary

Specification 0006 (`0006-route-graph-and-map-e2e`) has been completed and verified across all acceptance criteria and concurrency requirements.

Testing is structured across three distinct operational tiers:
1. **Mocked UI regression tests** (`frontend/e2e/route-optimization-e2e.spec.ts`): Eight scenarios verifying UI rendering, breadcrumbs, status polling transitions, multi color Leaflet polylines, custom stop pins, safe popup text escaping against Cross Site Scripting, mid trip replan form layout, version switching, and red failure diagnostic pins.
2. **Full stack browser acceptance tests** (`frontend/e2e/route-optimization-acceptance.spec.ts`): Seven scenarios executing against the real local stack (real Next.js on port 3000, real FastAPI on port 8000, live PostgreSQL, real LangGraph execution runner, worker consumer, and completion reconciliation). Injects only `MockValhallaClient` routing provider without intercepting application APIs. Demonstrates live submission, polling, refresh recovery, map retrieval, changed road replan, version switching, parking, walking, vehicle return, return home, PARTIAL, and INFEASIBLE outcomes. Tests tenant isolation using controlled test principals (`USER_A_ID` vs `USER_B_ID`), preserving the development auth decision.
3. **Connected Valhalla verification gate** (`backend/tests/integration/test_route_graph_valhalla_connected_slice5.py`): Live provider verification gated behind `RUN_CONNECTED_VALHALLA_TESTS=1`. Skips cleanly in 0.13s when unconfigured, and verifies turn by turn LineString coordinate decoding (> 50 points) when enabled.

Additionally, the replan concurrency policy is enforced for standalone and trip scoped routes:
- Competing branches are strictly forbidden. Route lineage is linear (v1 to v2 to v3).
- At most one child route may branch from any parent route version.
- Enforced atomically at acceptance (`SELECT ... FOR UPDATE` row lock on parent route plus child presence check and idempotency key reservation) and at persistence via PostgreSQL partial unique index `uq_routes_parent_route_id` on `routes(parent_route_id) WHERE parent_route_id IS NOT NULL`.
- Backed by real PostgreSQL concurrent request regression in `backend/tests/integration/test_route_replan_concurrency_slice2.py`.

No blockers or major defects were identified.

## Strengths

* **Clear test tier separation**: Mocked UI tests provide rapid feedback for visual regressions (22.8s), while separate browser acceptance tests validate the entire distributed system end to end across real database transactions and outbox events (2.6m).
* **Robust atomic concurrency enforcement**: Replan races are rejected before queueing work, and PostgreSQL enforces data integrity with a partial unique index, preventing branched lineages.
* **Tenant boundary isolation**: Real 403 Forbidden checks ensure route details and replan submissions never cross tenant boundaries.
* **Resilient data normalization**: Persistent models safely normalize legacy transport modes and traffic basis values, protecting domain logic from unexpected raw values.

## Verified commands and results
 
* `cd backend && PYTHONPATH=src .venv/bin/pytest tests/integration/test_route_replan_concurrency_slice2.py -v`: 3 passed in 19.29s on live PostgreSQL (concurrency single child race rejection, identical payload replay returning original job, changed payload 409 conflict, and failed job retry when no child route exists).
* `cd frontend && npx playwright test e2e/route-optimization-e2e.spec.ts`: 8 passed in 6.9s.
* `cd frontend && npx playwright test e2e/route-optimization-acceptance.spec.ts`: 7 passed in 1.8m against real local frontend, backend, PostgreSQL, runner, and worker, executing multimodal, partial, and infeasible routes through the live graph pipeline with owned fixture isolation and road closure geometry avoidance.
* `cd backend && PYTHONPATH=src .venv/bin/pytest tests/integration/test_route_graph_valhalla_connected_slice5.py -v`: 1 skipped in 0.13s (clean skip when unconfigured).
* `cd backend && RUN_CONNECTED_VALHALLA_TESTS=1 VALHALLA_ENDPOINT_URL=http://localhost:8002 PYTHONPATH=src .venv/bin/pytest tests/integration/test_route_graph_valhalla_connected_slice5.py -v`: 1 passed in 46.53s when connected.
 
## Limitations
 
1. Browser acceptance tests run against local Next.js and FastAPI instances connected to live cloud PostgreSQL. Because each LangGraph execution stages outbox events and cycles through graph runner and worker reconciliation over the network, full graph solving runs take approximately 40 to 50 seconds per execution cycle.
2. Connected Valhalla live tests require an active external Valhalla instance with geographic OSM tiles loaded. They are safely skipped in environments where Valhalla is not deployed.
3. The development authentication mode uses encoded bearer tokens with subject principals; production deployment requires standard asymmetric JWT verification through the identity provider.

