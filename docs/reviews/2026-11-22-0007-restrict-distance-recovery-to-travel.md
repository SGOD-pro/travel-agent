# Review, uncommitted, November 22 2026

**Reviewed by**: Senior Staff Reviewer (author on primary agent)
**Scope**: 3 files, uncommitted changes
**Verdict**: Approve

## Summary

This change correctly restricts degraded recovery for Valhalla distance rejections to TRAVEL workflows. Previously, any task payload containing a populated trip identifier could trigger degraded task completion under worker lease fencing, even when running under the standalone route optimizer graph. With this surgical modification in `RouteOptimizerWorkerService`, degraded HTTP 400 and error code 154 completion requires an active execution model with workflow set strictly to TRAVEL. Both unit and integration regression suites verify that standalone route optimizer workflows propagate the routing rejection, leave the task uncompleted under lease, write zero routes, and emit no completion events to the outbox.

## Strengths

* Precision of workflow guard: The boolean check in `RouteOptimizerWorkerService` strictly checks `exec_model is not None and exec_model.workflow == "TRAVEL"`. This eliminates the false positive where an execution of `ROUTE_OPTIMIZER_GRAPH` with a populated `trip_id` bypassed normal error propagation.
* Thorough regression verification: The unit test suite confirms that `WorkerFencingService.complete_task` is not awaited when `workflow == "ROUTE_OPTIMIZER_GRAPH"`, while the database integration lifecycle test confirms that task status remains not completed, zero routes are written to the database, and no `TASK_COMPLETED` outbox event is scheduled.
* Full preservation of travel recovery: The existing recovery path for TRAVEL workflows remains intact, allowing travel proposal synthesis to continue with partial results and honest diagnostics while preserving fail closed semantics for standalone routing.
* Surgical change discipline: Conforms strictly to AGENTS.md guidelines by altering only the target conditional in `service.py` without gratuitous refactoring or dead code.

## Test coverage

Test coverage is comprehensive across both isolation levels:
* Unit coverage: `backend/tests/unit/test_valhalla_distance_rejection.py` verifies error classification, degraded completion for TRAVEL workflows, exception re raising for non travel workflows, propagation when `ROUTE_OPTIMIZER_GRAPH` has a populated `trip_id`, and propagation for unrelated 500 errors.
* Integration coverage: `backend/tests/integration/test_valhalla_matrix_rejection_recovery_lifecycle.py` exercises the complete worker and database lifecycle, verifying both degraded recovery for TRAVEL workflows and fail closed error propagation for `ROUTE_OPTIMIZER_GRAPH` executions with an attached trip.
