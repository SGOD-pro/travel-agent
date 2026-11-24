# Review, main, 2026-11-25

**Reviewed by**: review subagent
**Scope**: 1 file, uncommitted
**Verdict**: Approve with nits

## Summary
This change tightens the Spec 0007 Valhalla matrix rejection integration tests in `backend/tests/integration/test_valhalla_matrix_rejection_recovery_lifecycle.py` so the mock provider returns a controlled HTTP 400 error_code 154 response on `/route` instead of an empty HTTP 200 payload, and verifies exact endpoint counts, durable fallback reservation settlement, specific failure diagnostics, incomplete budget lines, and strict standalone graph isolation. All assertions align accurately with the production paths in `service.py`, `valhalla.py`, `budget.py`, `graph.py`, `consumer.py`, and `trips.py`. Two small assertion gaps remain around fabricated road cost absence and post replay unexpected endpoint checks, neither of which blocks merge.

## Minor
### 🟡 Missing negative check for fabricated fuel or toll lines, `backend/tests/integration/test_valhalla_matrix_rejection_recovery_lifecycle.py:589-595`
**Problem**: The budget assertions confirm that `budget_summary["is_complete"]` is `False` and that `budget_lines` contains the explicit unknown `intercity_transport` entry, but they do not check that road fuel and toll lines are absent when `legs` is empty.
**Why it matters**: If a regression in `compute_budget_node` ever emitted a zeroed or guessed fuel or toll line alongside the `intercity_transport` unknown entry, this test would still pass.
**Suggested fix**: Assert that no entry in `budget_lines` has a fuel or toll basis when `routing_status` is `ROUTING_UNAVAILABLE` and `legs` is empty.

### 🟡 Post replay check omits `unexpected_requests`, `backend/tests/integration/test_valhalla_matrix_rejection_recovery_lifecycle.py:612`
**Problem**: After replaying `dispatch_payload` through `process_record`, line 612 rechecks `calls_by_endpoint == {"/sources_to_targets": 1, "/route": 1}` without rechecking `assert not unexpected_requests`.
**Why it matters**: If a duplicate delivery regression triggered a call to a different Valhalla endpoint such as `/status`, `calls_by_endpoint` would remain unchanged and the extra request would go unnoticed.
**Suggested fix**: Reassert `not unexpected_requests` immediately after the replay `process_record` call alongside the `calls_by_endpoint` check.

## Nits
- ⚪ `backend/tests/integration/test_valhalla_matrix_rejection_recovery_lifecycle.py:448`, `assert len(reservations) <= 3` is redundant immediately after `assert len(reservations) == 1`.

## Strengths
- Replacing the empty HTTP 200 fallback response on `/route` with an explicit HTTP 400 error_code 154 (1500000 meters) response forces the real `ValhallaClient` and `RouteOptimizerWorkerService` fallback, budget reservation, and reconciliation code paths to execute end to end.
- Checking `ExecutionModel.budget["route_fallback"]` (`spent_attempts == 1`, `reserved_attempts == 0`, `remaining_attempts == 2`, `status == "RECONCILED"`, `settled_as == "HTTP_400"`) alongside `WorkerResultModel.output_ref["error"]` confirms both durable cap accounting and honest failure attribution.
- Verifying `ExecutionModel.status == "SUCCEEDED"` separately from `itinerary["status"] == "partial"`, `route_id is None`, `map_geojson is None`, empty `legs`, preserved candidates, and `intercity_transport` unknown budget line covers the core Spec 0007 lifecycle invariants cleanly.
- Expanding `test_valhalla_matrix_rejection_propagates_for_route_optimizer_graph_with_trip_id` to assert exact endpoint call counts (`{"/sources_to_targets": 1, "/route": 0}`), absence of `route_fallback` budget state, zero `TASK_COMPLETED` outbox rows, and untouched trip planning state verifies strict isolation even when `trip_id` is populated.

## Test coverage
`TESTS = configured`: the configured integration suite (`test_valhalla_matrix_rejection_recovery_lifecycle.py` and `test_valhalla_fallback_durable_cap.py`, 6/6 tests) along with `ruff check` and `mypy` were reported passing. The updated tests directly cover matrix 400/154 rejection followed by bounded `/route` 400/154 fallback, durable cap settlement, checkpoint resume, HTTP itinerary retrieval, duplicate dispatch replay, and standalone `ROUTE_OPTIMIZER_GRAPH` propagation with a populated `trip_id`.
