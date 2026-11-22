# Review: Valhalla Matrix Distance Rejection Recovery (0007), 2026-11-22

**Reviewer**: Senior Code Reviewer
**Scope**: 6 files across backend routing client, optimizer worker, orchestration graph, test suites, and live evaluation script
**Verdict**: Approve

## Summary

This change implements deterministic recovery when the external Valhalla routing engine rejects intercity matrix calculations with HTTP 400 and error code 154 on the `/sources_to_targets` endpoint. In intercity itineraries where distance between stops exceeds 150 kilometers, public Valhalla instances reject the matrix query. Previously, this failure would abort the workflow or fail the job without publishing the verified candidate places, weather advisories, or budget synthesis.

With this change, the rejection is classified with high precision, completed durably under worker lease fencing with degraded `ROUTING_UNAVAILABLE` status, cleanly consumed by the trip planner graph, and published as a proposed itinerary with preserved places, evidence provenance, and honest diagnostics explaining provider distance limits without Euclidean guesswork.

All database invariants are preserved: zero phantom rows in the routes table, lease tokens cleared on task completion, and idempotent discard on message redelivery.

## Detailed Verification by Criteria

### 1. Classification Precision

The classification is implemented in `backend/src/functions/route_optimizer/valhalla.py`.

* Inspection of `ValhallaRoutingError.is_matrix_distance_limit_exceeded`:
  The property strictly verifies three simultaneous conditions:
  1. `self.status_code == 400`
  2. `self.error_code == 154`
  3. `self.endpoint == "/sources_to_targets"` or ending with `"/sources_to_targets"`
* Inspection of `is_valhalla_matrix_distance_limit_error`:
  The helper function accepts `BaseException`, checks `isinstance(exc, ValhallaRoutingError)`, and delegates to the property. Any other exception type returns `False`.
* Precision checks:
  * HTTP 400 errors with other error codes (such as error code 171 for no suitable edges) return `False`.
  * Errors returned from other endpoints (such as `/route`) return `False`, even if error code 154 is returned.
  * Server errors (HTTP 500) and timeouts return `False`.
  * Safe extraction: `_send_request` attempts to parse JSON response bodies safely within `try...except`, so malformed responses or non JSON error payloads do not raise parsing crashes and default `error_code` to `None`.

### 2. Error Handling and Fast Fail Semantics

The error handling logic conforms to provider retry policies and workflow scope boundaries.

* Nonretryable client failure:
  In `ValhallaClient._send_request`, status codes in the 4xx range (`400 <= resp.status_code < 500`) log the client error and raise `ValhallaRoutingError` immediately without retry attempts. Unit test `test_valhalla_error_code_154_classified_correctly` verifies `client.call_count == 1` and `client.retry_count == 0`. Integration test `test_valhalla_matrix_rejection_degraded_recovery_lifecycle` verifies `valhalla_call_count == 1` at the HTTP transport boundary.
* Workflow boundary enforcement:
  In `RouteOptimizerWorkerService.execute_task`, the degraded recovery branch is gated by `is_travel_workflow`:
  `is_travel_workflow = ((exec_model is not None and exec_model.workflow == "TRAVEL") or effective_trip_id is not None)`
  If the workflow is a standalone route optimization without trip context, the exception is reraised untouched. This is verified by `test_optimizer_worker_reraises_for_non_travel_workflow`.
* Unrelated error propagation:
  Any other routing error, such as HTTP 500 server errors, connection timeouts, or closure calculation errors, bypasses the recovery block and reraises untouched. This is verified by `test_optimizer_worker_reraises_unrelated_valhalla_error`.

### 3. Lease Fencing and Durable Completion

The worker completes the degraded task under parent first lease fencing semantics.

* In `RouteOptimizerWorkerService.execute_task`:
  When the classified distance limit rejection occurs, the service invokes:
  `await self._fencing_service.complete_task(execution_id=claim_result.execution_id, task_id=claim_result.task_id, lease_token=claim_result.lease_token, output=degraded_output, expected_context_version=claim_result.context_version)`
* Lease protection:
  `WorkerFencingService.complete_task` atomically validates the lease token and context version against the task row. If the lease expired or was preempted by another worker, the completion is rejected.
* Stranded claim prevention:
  The completion persists `WorkerResultModel`, transitions the task state to `COMPLETED`, clears `lease_token` and `lease_expiry`, and inserts a `TASK_COMPLETED` event into the transactional outbox table. No task is left stranded in `PENDING` or `IN_PROGRESS`.

### 4. Job Timeline Observability

* Job event recording:
  Immediately after completing the task under lease, the worker records a timeline event:
  `await record_job_event(self._session, execution_id=claim_result.execution_id, event_type="ROUTE_OPTIMIZED", safe_payload={"status": "ROUTING_UNAVAILABLE", "routing_status": "ROUTING_UNAVAILABLE", "reason": safe_reason})`
* Audit trail integrity:
  The event type matches `ROUTE_OPTIMIZED`, which is the expected event name for the routing optimization phase.
  The payload contains clean diagnostic strings describing the provider matrix distance threshold without exposing raw tokens or internal system credentials.

### 5. Graph Consumption and Evidence Preservation

The graph consumption node in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` cleanly absorbs degraded worker output.

* In `consume_route_result_node`:
  The condition evaluates:
  `is_failed_worker = (worker_output_dict.get("status") in ("FAILED", "CANCELLED") or worker_output_dict.get("routing_status") == "ROUTING_UNAVAILABLE" or "error" in worker_output_dict)`
  When `route_id` is missing or `None`, it constructs an empty schedule:
  `empty_schedule = OptimizedSchedule(stops=[], legs=[], total_travel_minutes=0, is_optimal=False)`
  It outputs:
  * `route_id`: `None`
  * `route_status`: `"ROUTING_UNAVAILABLE"`
  * `routing_status`: `"ROUTING_UNAVAILABLE"`
  * `route_diagnostics`: worker diagnostics combined with the standard diagnostic notice
  * `optimized_schedule`: serialized empty schedule
* Evidence and stop preservation:
  The return dictionary does not overwrite or clear `discovered_places`, `validated_candidates`, `eligible_stops`, `editorial_recommendations`, or `observations`. All candidate evidence and places remain intact in graph state.
* Downstream execution:
  * `fetch_weather_node` falls back cleanly to sampling coordinates from eligible stops or brief destinations without fabricating arrival or departure times.
  * `compute_budget_node` iterates over legs (empty in this case), skipping road fuel and toll calculation while preserving accommodation, food, and entrance lines.
  * `synthesize_proposal_node` compiles the proposal with `route_id=None` and `routing_status="ROUTING_UNAVAILABLE"`.
  * `publish_proposal_node` publishes the proposal under pessimistic locks and runner lease fencing, updating `trips.current_route_id = None`, `trips.itinerary_json = proposal`, and `trips.state = "proposed"`.

### 6. Database Invariants and Replay Safety

* Zero phantom routes:
  In degraded completion mode, `_route_repo.save_route` is never called. Both the integration test and the real live backend journey confirm `SELECT count(*) FROM routes WHERE execution_id = :eid` equals zero.
* No stranded lease claim:
  The task status transitions to `COMPLETED` with nullified lease token and expiry.
* Idempotent replay safety:
  When a duplicate dispatch envelope is received by `route_optimizer_consumer.process_record`, the task claim is rejected because the task is already in `COMPLETED` status. No duplicate deltas or duplicate job events are created, and trip version remains stable.

### 7. Live Backend Journey Verification

`backend/scripts/run_live_backend_journey.py` executes the entire application against real external providers (Bedrock, SerpAPI, Scrapling, public Valhalla, Open Meteo, Obscura, and PostgreSQL).

* In `live_journey_result.json`, the live journey on the Bengaluru to Mysuru to Madikeri corridor (over 250 kilometers intercity distance) encountered the public Valhalla matrix distance limit rejection.
* The outbox relay and route optimizer consumer handled the rejection via the recovery path.
* The job succeeded with terminal status `SUCCEEDED`.
* The authenticated API endpoint `GET /api/v1/trips/{trip_id}/itinerary` returned HTTP 200 with `state: "proposed"`, `routing_status: "ROUTING_UNAVAILABLE"`, `route_id: null`, zero routes in the database, and preserved candidate places.

## File by File Findings

### 1. `backend/src/functions/route_optimizer/valhalla.py`
* Clean extension of `ValhallaRoutingError` with status code, error code, endpoint, and response body attributes.
* Precise property `is_matrix_distance_limit_exceeded` and standalone classifier function.
* Fast fail on 4xx responses without wasteful retry attempts.

### 2. `backend/src/functions/route_optimizer/service.py`
* Accurate classification check in `execute_task` limited strictly to travel workflows.
* Atomic completion under lease fencing with `complete_task`.
* Clean `ROUTE_OPTIMIZED` job event recording.
* Correct return signature `tuple[uuid.UUID | None, RouteOptimizerWorkerOutput | dict[str, Any]]`.

### 3. `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`
* `consume_route_result_node` correctly handles `route_id=None`.
* Preserves diagnostics and candidate places in graph state.
* Downstream nodes proceed to publication without crashing on empty driving legs.

### 4. `backend/tests/unit/test_valhalla_distance_rejection.py`
* 6 focused unit tests covering classification, non classification of other 400 errors, non classification of `/route` endpoint, worker degraded recovery under lease, reraising for non travel workflows, and reraising for unrelated provider errors.
* All 6 tests pass.

### 5. `backend/tests/integration/test_valhalla_matrix_rejection_recovery_lifecycle.py`
* Comprehensive end to end lifecycle test covering dispatch, HTTP boundary mock transport, consumer execution, lease clearing, zero route invariant, reconciliation, runner resume, authenticated HTTP API assertion, and idempotent redelivery.
* Passes with isolated test database configuration.

### 6. `backend/scripts/run_live_backend_journey.py`
* Validates real end to end behavior against live public Valhalla, proving that intercity trip publication is rescued in real world environments.

## Minor Observation (Non Blocking)

In `test_valhalla_distance_rejection.py`, line 157 sets `mock_session = AsyncMock()`. During execution, `record_job_event` invokes `session.add(event)`. In SQLAlchemy, `session.add` is synchronous. Because `mock_session` is an `AsyncMock`, calling `add` returns an unawaited coroutine, producing a benign Python `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited`. Setting `mock_session.add = MagicMock()` in the test setup would eliminate this warning.

## Verdict

Approve. The implementation is surgical, robust, preserves all core database and concurrency invariants, and successfully rescues trip publication when intercity distances exceed provider matrix thresholds.
