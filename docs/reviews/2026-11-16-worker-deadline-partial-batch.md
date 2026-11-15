# Review: Worker Invocation Deadline and Partial Batch Handling (2026-11-16)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 5 files, commit e13e59b
**Verdict**: Approve

## Summary

This review evaluates commit e13e59b on specification 0007 Slice 8, titled "feat(workers): enforce invocation deadline and partial batch handling". The commit resolves audit finding 1 by introducing proactive AWS Lambda invocation deadline management, partial batch response handling via SQS batchItemFailures, fail closed budget spend preservation on worker cancellation, and safe discarding of duplicate or stale task messages under worker fencing. The implementation spans [evidence_collector/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L1-L193), [browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L1-L191), and [evidence_collector/service.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/service.py#L614-L676). The test suite includes comprehensive unit tests in [test_browser_consumer.py](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_browser_consumer.py#L1-L318) and integration tests against real PostgreSQL in [test_worker_deadline_and_partial_batch.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L1-L692).

## Evaluation of Review Criteria

### 1. Audit Finding 1 Resolution: Deadline Enforcement and Reserve Margin
In standard AWS Lambda execution, if execution time reaches zero, the container runtime terminates the process abruptly via SIGKILL. This prevents the worker from returning the partial batch response, which causes SQS to redrive every record in the batch including already completed work.

Commit e13e59b resolves this hazard in both worker consumers:
* Evidence collector consumer at [consumer.py:28-46](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L28-L46) defines a default reserve margin of 3000 milliseconds (`DEFAULT_INVOCATION_RESERVE_MS = 3000`).
* Browser consumer at [consumer.py:30-48](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L30-L48) defines a default reserve margin of 5000 milliseconds (`DEFAULT_INVOCATION_RESERVE_MS = 5000`), allowing sufficient time for headless browser process tree teardown and socket disposal.
* `get_remaining_usable_seconds` computes usable time by subtracting the reserve margin from `context.get_remaining_time_in_millis()`.
* If remaining time is under the reserve margin before starting a batch, the consumer immediately marks all records as failed for SQS redrive without starting database transactions or adapter calls ([consumer.py:110-117](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L110-L117)).
* For running work, `_process_one` wraps execution in `asyncio.wait_for(..., timeout=usable_seconds)` ([consumer.py:140-148](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L140-L148)). When the usable deadline expires, pending coroutines are cancelled, finally cleanup blocks run, and the timed out record is reported for retry before Lambda reaches container expiration.

### 2. SQS Partial Batch Contract
Both consumers implement the AWS SQS partial batch failure contract:
* When a record completes successfully, `_process_one` returns `None`. Successful message identifiers are omitted from the returned `failures` list. SQS deletes these records permanently from the queue.
* When a record fails, times out, or is cancelled, its message identifier is returned as `{"itemIdentifier": message_id}`.
* In [consumer.py:190-193](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L190-L193), the top level `lambda_handler` returns `{"batchItemFailures": failures}`. SQS leaves only the failing records in flight for redrive, eliminating duplicate reprocessing of successfully completed sibling tasks.

### 3. Budget Spend Preservation on Cancellation and Timeout
In [evidence_collector/service.py:614-675](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/service.py#L614-L675), the collector distinguishes whether a query call was dispatched to a remote network provider before cancellation:
* When a query is initiated, `dispatched = True` is recorded prior to calling the adapter.
* When `asyncio.wait_for` cancels execution due to invocation deadline expiration, `asyncio.CancelledError` is caught at line 646, marking `call_outcome = "CANCELLED"`.
* The `finally` block at line 665 executes `_safe_reconcile_budget` with `dispatched=True` and `outcome="CANCELLED"`.
* In `reconcile_budget_entry`, this triggers the fail closed branch: the reservation cost is deducted from `reserved_cost_minor` and `remaining_cost_minor`, and recorded in `spent_cost_minor` and `uncertain_cost_minor`. It is not refunded, ensuring worker timeouts do not permit free quota overruns.

This behavior is verified by `test_evidence_budget_preserves_uncertain_spend_on_invocation_timeout` at [test_worker_deadline_and_partial_batch.py:540-618](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L540-L618).

### 4. Fencing, Idempotency, and Benign Conflict Discarding
In both consumers ([evidence consumer:75-82](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L75-L82) and [browser consumer:76-83](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L76-L83)):
* Benign fencing exceptions (`TaskClaimConflictError`, `TaskLeaseActiveError`, `TaskAlreadyTerminalError`, `StaleQueueMessageError`) indicate that another worker or prior delivery has claimed or finished the task.
* The consumer catches these exceptions, logs them at informational level, and returns normally without appending the message identifier to `batchItemFailures`.
* SQS interprets the absence of failure as success and deletes the duplicate message, preventing harmless claim collisions from endlessly looping to the dead letter queue.
* Genuine infrastructure failures such as database disconnections raise `ExecutionEngineError` and are reported in `batchItemFailures` for proper redrive.

This is verified in unit tests at [test_browser_consumer.py:134-160](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_browser_consumer.py#L134-L160) and integration tests at [test_worker_deadline_and_partial_batch.py:622-664](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L622-L664).

### 5. Clear Distinction Between AWS Lambda Execution and Simulated Context Testing
The file documentation and test architecture cleanly articulate the operating environment:
* In production AWS Lambda, `context.get_remaining_time_in_millis()` is provided by the execution microVM environment.
* In tests, `SimulatedLambdaContext` ([test_worker_deadline_and_partial_batch.py:61-72](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L61-L72)) models that interface using monotonic wall clock time without mocking internal consumer details.
* This proves that consumers operate against the public Lambda context protocol, correctly calculating usable time and protecting container reserve windows.

### 6. Test Suite Coverage Across Mixed Records and Edge Cases
The test coverage thoroughly validates all operational permutations:
* Mixed fast and slow evidence tasks: `test_evidence_collector_mixed_fast_and_slow_records_partial_batch` at [test_worker_deadline_and_partial_batch.py:256-354](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L256-L354) confirms that the fast record commits while the slow record times out and returns in `batchItemFailures`.
* Mixed fast and slow browser renders: `test_browser_worker_mixed_fast_and_slow_records_partial_batch` at [test_worker_deadline_and_partial_batch.py:358-460](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L358-L460) confirms that the fast render commits observations while the slow render is cancelled and returned in `batchItemFailures`.
* Usable time exhaustion during sequential execution: `test_deadline_during_sequential_execution_skips_unstarted_record` at [test_worker_deadline_and_partial_batch.py:464-536](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L464-L536) proves that when an earlier record consumes usable time, subsequent unstarted records are skipped immediately without executing external searches.
* Pre execution deadline expiration: `test_pre_execution_insufficient_time_fails_all_records_immediately` at [test_worker_deadline_and_partial_batch.py:669-692](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L669-L692) verifies that insufficient initial time aborts the entire batch cleanly.
* Unit coverage: [test_browser_consumer.py](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_browser_consumer.py#L1-L318) exercises message routing, fencing error handling, execution engine error re raising, partial batch filtering, synchronous entrypoint invocations, and missing message identifier edge cases.

## Strengths
* Graceful degradation under load: Proactively cancels running work before Lambda container SIGKILL, guaranteeing partial batch responses reach SQS.
* Precise partial batch delivery: Commits successful sibling records while selectively redriving only failed or timed out items.
* Fail closed budget integrity: Preserves uncertain spend when calls are cancelled after network dispatch, preventing retries from bypassing budget caps.
* Clean queue hygiene: Acknowledges benign fencing conflicts so duplicate deliveries are deleted without poisoning dead letter queues.

## Test Coverage
The combination of unit tests in [test_browser_consumer.py](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_browser_consumer.py#L1-L318) and integration tests in [test_worker_deadline_and_partial_batch.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L1-L692) provides comprehensive end to end verification for worker deadline enforcement, partial batch handling, and budget preservation.
