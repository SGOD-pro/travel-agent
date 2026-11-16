# Review: Worker Deadline Cancellation Boundary Correction (2026-11-16)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 5 files, commit 1692063
**Verdict**: Approve

## Summary

This review evaluates commit 1692063 on specification 0007 Slice 8, titled "feat(workers): bound cancellation cleanup and protect invocation deadline (0007 Slice 8)". The change corrects the cancellation boundary defect where a record exceeding its usable time could block or suppress cancellation during cleanup, jeopardizing the AWS Lambda invocation deadline.

The implementation introduces two layer protection in both SQS consumers ([evidence_collector/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L28-L285) and [browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L30-L284)), bounds database budget reconciliation in [evidence_collector/service.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/service.py#L220-L265), bounds browser and proxy resource teardown in [browser/driver.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L671-L697), and adds comprehensive integration tests in [test_worker_deadline_and_partial_batch.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L695-L963).

## Evaluation of Review Criteria

### 1. Cancellation Cleanup Bounding
In AWS Lambda, if cancellation cleanup blocks indefinitely, the container reaches zero remaining time and is terminated by SIGKILL, preventing the handler from returning partial batch failures to SQS.

Commit 1692063 introduces phased execution in both consumers:
* During normal execution, `await asyncio.wait_for(asyncio.shield(task), timeout=usable_seconds)` bounds the initial work while shielding the inner task from immediate cancel propagation.
* When usable time expires, `timed_out = True` is recorded, and `task.cancel()` is triggered.
* Cleanup is bounded by `await asyncio.wait_for(task, timeout=cleanup_sec)`.
* Crucially, `cleanup_sec` is dynamically calculated at cancellation start:
  `available_cleanup_s = max(0.02, (rem_ms - cleanup_margin_ms) / 1000.0)`
  where `cleanup_margin_ms` reserves safety margin for the batch response.
* If a worker's cleanup blocks or sleeps past `cleanup_sec`, a warning is logged, the cleanup is abandoned locally, and the record is returned in `batchItemFailures` before Lambda container expiration.
* This is verified by `test_cancellation_resistant_cleanup_bounded_before_hard_invocation_deadline` at [test_worker_deadline_and_partial_batch.py:698-795](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L698-L795).

### 2. Preserving Successful Siblings and Partial Batch Responses
The batch hard deadline guard wraps `asyncio.gather(*record_tasks, return_exceptions=True)` with `batch_hard_timeout_s = (total_rem_ms - safety_margin_ms) / 1000.0`.
* If the batch hard timeout triggers, already completed sibling tasks are inspected via `t.done() and not t.cancelled() and t.exception() is None`.
* If an earlier sibling task completed successfully (`t.result() is None`), it is omitted from `failures`.
* SQS deletes the successful message, preventing duplicate processing on redrive.
* Only incomplete, cancelled, or failing tasks are appended to `failures` as `{"itemIdentifier": mid}`.

### 3. Suppressed Cancellation Handling
If an inner worker operation catches and swallows `asyncio.CancelledError` (or a third party library catches `BaseException` and returns normally):
* The consumer relies on the explicit `timed_out = True` flag recorded when `usable_seconds` expired.
* Regardless of what the inner coroutine returns after cancellation, the consumer ignores any swallowed return value and returns `{"itemIdentifier": message_id}` in `batchItemFailures`.
* This prevents message loss where a timed out message would otherwise be treated as successful.
* This is verified by `test_suppressed_cancellation_swallowed_returns_partial_batch_failure` at [test_worker_deadline_and_partial_batch.py:800-846](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L800-L846).

### 4. Blocked Reconciliation and Fail Closed Budget Accounting
In [evidence_collector/service.py:220-265](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/service.py#L220-L265), `_safe_reconcile_budget` executes with `asyncio.wait_for(asyncio.shield(...), timeout=timeout_seconds)` (default 1.5s):
* If the database hangs or row locks block during cancellation cleanup, reconciliation times out cleanly without hanging the process.
* The reservation remains in status `ACTIVE` under `executions.budget['reservations']`.
* When worker lease expiry passes, `TaskRecoveryService.recover_abandoned_budget_reservations` transitions the reservation to `ABANDONED_UNCERTAIN`, incrementing `spent_cost_minor` and `uncertain_cost_minor` and decrementing `remaining_cost_minor`.
* The budget fails closed conservatively without allowing duplicate spends or quota bypass.
* This is verified by `test_blocked_reconciliation_times_out_and_preserves_budget_as_uncertain` at [test_worker_deadline_and_partial_batch.py:851-963](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L851-L963).

### 5. Browser Driver Teardown Bounding
In [browser/driver.py:671-697](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L671-L697), `PlaywrightBrowserAdapter` bounds each teardown action:
* Background tasks: `asyncio.wait_for(asyncio.gather(*background_tasks, return_exceptions=True), timeout=1.0)`
* Page closure: `asyncio.wait_for(page.close(), timeout=1.0)`
* Context closure: `asyncio.wait_for(context.close(), timeout=1.0)`
* Egress proxy stop: `asyncio.wait_for(proxy.stop(), timeout=1.0)`
* Unresponsive browser instances or hung network sockets cannot prevent consumer cleanup completion.

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
2. Full suite: 110 passed across unit and integration suites in 64.96s.
3. Static typing and linting: `mypy` passed with 0 errors across 19 source files; `ruff` passed with all checks clean.

## Limitations and Open Gates

1. Hardware AWS Lambda execution remains marked PENDING. Verification utilizes `SimulatedLambdaContext` modeling monotonic clock progression.
2. Unstoppable local work: Work that cannot be safely terminated locally (e.g. hung remote provider call or hung database connection) is abandoned by the worker, leaving its reservation active until lease expiry where it is safely swept to `ABANDONED_UNCERTAIN`. Spend is never assumed to be free.
