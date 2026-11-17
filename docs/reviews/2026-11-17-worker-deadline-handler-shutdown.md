# Review: Worker Deadline Handler Shutdown Correction (2026-11-17)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 6 files, commit a4fd415
**Verdict**: Approve

## Summary

This review evaluates commit a4fd415 on specification 0007 Slice 8, titled "feat(workers): bound lambda handler shutdown and event loop lifecycle (0007 Slice 8)". The change corrects the handler shutdown defect where a stubborn record or pending task that resists cancellation prevents standard `asyncio.run` shutdown from completing, causing the AWS Lambda invocation to reach zero remaining time and terminate via SIGKILL without returning its partial batch response to SQS.

The implementation replaces standard unbounded `asyncio.run` with an explicit per invocation event loop lifecycle in both SQS consumers ([evidence_collector/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L286-L408) and [browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L285-L407)). It bounds browser and playwright driver teardown in [browser/driver.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L284-L293), includes required budget support in isolated packaging modules ([evidence_collector/packaging.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/packaging.py#L210-L218) and [browser/packaging.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/packaging.py#L231-L239)), and adds four subprocess isolated integration tests in [test_worker_deadline_and_partial_batch.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L968-L1264).

## Evaluation of Review Criteria

### 1. Handler Shutdown and Event Loop Lifecycle
In standard Python `asyncio.run`, `Runner.close` invokes `_cancel_all_tasks(loop)`, which runs `loop.run_until_complete(tasks.gather(*to_cancel, return_exceptions=True))` with no timeout. If any task suppresses cancellation or has long running operations in a finally block, `asyncio.run` blocks indefinitely even after `process_batch` has returned its partial batch failures.

Commit a4fd415 resolves this by introducing explicit loop lifecycle management in `lambda_handler`:
* A fresh event loop is created for each invocation: `loop = asyncio.new_event_loop()`, and bound via `asyncio.set_event_loop(loop)`.
* Batch execution is given an outer wall clock timeout: `handler_exec_timeout_s = max(0.05, (rem_ms - 200) / 1000.0)`, reserving 200ms for loop cleanup and response return.
* Batch execution and task cancellation use `asyncio.wait` rather than `asyncio.wait_for` or `asyncio.gather`. In Python 3.11+, `asyncio.wait_for(gather(...))` awaits the gathering future when cancelled, which in turn waits for child tasks to become done. Using `asyncio.wait(set(pending), timeout=shutdown_s)` guarantees that when the shutdown timeout expires, the wait call returns immediately without waiting for stubborn tasks.
* Lingering tasks are cancelled and awaited with bounded shutdown time: `shutdown_s = max(0.01, min(0.5, (cur_rem - 50) / 1000.0))`.
* When the shutdown window elapses, any remaining tasks are abandoned locally.
* Asynchronous generators and default thread pool executors are closed with strict 50ms bounds.
* In the finally block, `loop.close()` destroys all remaining pending tasks on the loop, and `asyncio.set_event_loop(None)` unbinds the thread loop. Subsequent invocations in a reused Lambda container start with a brand new event loop and cannot inherit or resume abandoned tasks.

### 2. Browser Driver Teardown Bounding
In [browser/driver.py:284-293](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L284-L293), `PlaywrightBrowserAdapter.render_page` previously awaited `browser.close()` and `playwright.stop()` without timeouts in its outermost finally block:
* If the underlying Chromium process or Playwright driver hung on network sockets or pipes, the driver teardown blocked indefinitely.
* Commit a4fd415 wraps both calls with `asyncio.wait_for(..., timeout=1.0)`.
* Together with pre-existing 1.0s timeouts on page closure, context closure, and egress proxy stop, all browser resource teardown operations are strictly bounded.

### 3. Preserving Sibling Results and Partial Batch Responses
The bounded handler architecture guarantees that partial batch responses reach SQS:
* Fast sibling records that complete successfully before usable time expires are committed to PostgreSQL and omitted from `batchItemFailures`.
* SQS deletes the successful messages, preventing duplicate work on retry.
* Slow, timed out, or stubborn records are returned in `batchItemFailures` as `{"itemIdentifier": mid}`.
* Verified in [test_worker_deadline_and_partial_batch.py:1200-1264](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L1200-L1264), where a fast record succeeded in 10ms and was committed while a stubborn record timed out, resulting in `{"batchItemFailures": [{"itemIdentifier": "msg-stubborn-sibling"}]}`.

### 4. Fail Closed Spend and Fencing Preservation
When an abandoned task cannot complete its budget reconciliation before the handler deadline:
* The active budget reservation remains committed in PostgreSQL in `executions.budget['reservations']` with status `ACTIVE`.
* Worker task leases expire normally after `lease_seconds`.
* Background `TaskRecoveryService.recover_abandoned_budget_reservations` sweeps the expired reservation to `ABANDONED_UNCERTAIN`, incrementing `spent_cost_minor` and `uncertain_cost_minor` without refunding.
* Any late attempt by the abandoned worker to touch the database is rejected by lease fencing (`TaskLeaseActiveError` or token mismatch).
* Spend accounting remains conservative and fail closed.

### 5. Packaging and Isolated Import Conformance
In [evidence_collector/packaging.py:216](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/packaging.py#L216) and [browser/packaging.py:237](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/packaging.py#L237):
* `budget.py` is included in the permitted execution support staged for isolated Lambda artifacts.
* `test_verify_handler_import_clean_lambda_environment` and `test_verify_browser_handler_import_clean_lambda_environment` pass cleanly in an isolated Python process with only artifact directories on `PYTHONPATH`.

## Test Coverage

1. `test_worker_deadline_and_partial_batch.py`:
   * `test_evidence_collector_mixed_fast_and_slow_records_partial_batch` PASSED
   * `test_browser_worker_mixed_fast_and_slow_records_partial_batch` PASSED
   * `test_deadline_during_sequential_execution_skips_unstarted_record` PASSED
   * `test_evidence_budget_preserves_uncertain_spend_on_invocation_timeout` PASSED
   * `test_duplicate_delivery_under_fencing_discarded_without_dlq_redrive` PASSED
   * `test_pre_execution_insufficient_time_fails_all_records_immediately` PASSED
   * `test_cancellation_resistant_cleanup_bounded_before_hard_invocation_deadline` PASSED
   * `test_suppressed_cancellation_swallowed_returns_partial_batch_failure` PASSED
   * `test_blocked_reconciliation_times_out_and_preserves_budget_as_uncertain` PASSED
   * `test_lambda_handler_cooperative_cancellation_returns_before_deadline` PASSED
   * `test_lambda_handler_repeated_cancellation_suppression_bounded_and_returns` PASSED
   * `test_lambda_handler_blocked_cleanup_abandoned_and_returns_partial_batch` PASSED
   * `test_lambda_handler_successful_siblings_preserved_with_stubborn_record` PASSED
2. Subprocess isolation:
   * Tests run `lambda_handler` in isolated OS subprocesses using `subprocess.run` with parent enforced timeouts (5.0s).
   * Verified that child processes return before simulated invocation deadlines (elapsed approximately 2.44s on a 2500ms context with 1500ms reserve).
3. Regression and packaging suites:
   * `test_browser_consumer.py`: 15 passed in 1.46s.
   * `test_budget_crash_recovery.py`: 14 passed in 15.84s.
   * `test_worker_budget_and_providers.py` and `test_task_recovery.py`: 23 passed in 15.87s.
   * `test_claim_ingestion.py` and `test_trip_planner_evidence_runner_lifecycle.py`: 58 passed in 19.33s.
   * `test_sam_package_size.py`: clean lambda environment import verified.
   * `test_browser_packaging.py`: clean lambda environment import verified.
4. Static verification:
   * `ruff check src tests`: clean with zero issues.
   * `mypy src/functions/evidence_collector src/functions/browser tests/integration/test_worker_deadline_and_partial_batch.py`: clean with zero issues across 19 source files.

## Limitations and Open Gates

1. Hardware AWS Lambda execution remains marked PENDING. Verification utilizes `SimulatedLambdaContext` and isolated child subprocess execution modeling monotonic container time.
2. Unstoppable work in Python: Python cannot forcibly interrupt native C extensions or synchronous blocking calls (such as blocking DNS or C socket reads) via `task.cancel()`. By using `asyncio.wait` and closing the event loop, the Python handler returns before the container deadline, abandoning the blocked task. If such work cannot complete, AWS Lambda container retirement will reclaim system resources, while PostgreSQL fencing and recovery safely account for budget spend.
