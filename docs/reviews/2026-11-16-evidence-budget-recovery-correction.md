# Review: Budget Reconciliation Binding and Abandonment Verification Under Locks (2026-11-16)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 5 files, commit a32bace
**Verdict**: Approve

## Summary

This review assesses commit a32bace on specification 0007 Slice 8, titled "feat(orchestrator): bind budget reconciliation and verify abandonment under locks". The commit refines the evidence budget reservation and reconciliation engine across [budget.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L49-L472), [recovery.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L330-L343), and [service.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/service.py#L200-L264). The implementation enforces re-evaluation of timestamps after row lock acquisition to protect against lock wait lease expiry, strictly binds reconciliation requests to stored reservation metadata, verifies abandonment under locks so that active reservations of live tasks are never prematurely stolen by recovery sweeps, distinguishes non dispatched calls from uncertain external outcomes like timeouts or crashes, and preserves strict budget ceilings on PostgreSQL. The test suite in [test_budget_crash_recovery.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L742-L1202) provides comprehensive regression verification.

## Evaluation of Review Criteria

### 1. Lock Wait Safety via Timestamp Re-evaluation
When multiple transactions contend for row locks, a worker lease may expire while waiting in the database queue for lock acquisition. The updated logic in [budget.py:73, 89](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L73) for `reserve_budget_entry` and [budget.py:192, 207](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L192) for `reconcile_budget_entry` samples `datetime.now(UTC)` immediately after acquiring locks on `ExecutionModel` and `TaskModel`.
* In `reserve_budget_entry` at [budget.py:100-102](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L100-L102), if the task lease expired during the lock wait, reservation is rejected and returns `None`.
* In `reconcile_budget_entry` at [budget.py:225-227](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L225-L227), if the lease expired during the lock wait, reconciliation is rejected and returns `False`.

This behavior is verified by `test_lock_wait_lease_expiry_rejects_reservation` at [test_budget_crash_recovery.py:742-792](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L742-L792) and `test_lock_wait_lease_expiry_rejects_reconciliation` at [test_budget_crash_recovery.py:793-850](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L793-L850).

### 2. Strict Binding of Reconciliation to Stored Reservations
In [budget.py:230-286](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L230-L286), `reconcile_budget_entry` enforces binding between caller parameters and the stored reservation entry:
* Missing reservation identifier: Rejects reconciliation without mutating counters.
* Task identifier mismatch: Rejects reconciliation if `task_id` differs from the reservation.
* Attempt mismatch: Rejects reconciliation if the worker attempt differs from the reservation attempt.
* Lease token mismatch: Rejects reconciliation if the lease token differs from the stored token.
* Cost mismatch: Rejects reconciliation if the reported cost differs from `reserved_cost_in_entry`.
* Idempotency guarantee at [budget.py:288-297](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L288-L297): If the reservation is already settled or recovered, the function returns `True` as a no op without adjusting budget counters a second time.

This is thoroughly verified by `test_mismatched_or_missing_reservation_leaves_counters_unmodified` at [test_budget_crash_recovery.py:852-982](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L852-L982).

### 3. Verification of Abandonment Under Locks During Recovery
In [budget.py:355-472](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L355-L472), `recover_abandoned_budget_reservations` implements abandonment verification under locks rather than blindly marking active reservations as abandoned:
* Enforces parent first lock ordering: Locks `ExecutionModel` first, then locks `TaskModel`.
* Re-evaluates current time under the task lock.
* Checks whether the task is live: If the parent execution is healthy and the task is in `CLAIMED` or `RUNNING` status with an unexpired lease and matching lease token, `is_abandoned` evaluates to `False`. The reservation is preserved, preventing recovery sweeps from stealing active budget from running workers.
* Only truly abandoned reservations (unclaimed, terminal task, or expired lease) transition to `ABANDONED_UNCERTAIN`.

This is confirmed by `test_recovery_racing_active_worker_preserves_live_reservation` at [test_budget_crash_recovery.py:983-1045](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L983-L1045).

### 4. Preservation of Uncertain Spend Across Timeouts and Cancellations
In [budget.py:320-346](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L320-L346), the engine cleanly separates two unsuccessful execution outcomes:
* Not dispatched calls (`dispatched=False` or `outcome="NOT_DISPATCHED"`): The reservation was reserved but never sent to the network provider. The reservation is safely released, returning the reserved capacity to available balance without incrementing spent or uncertain counters.
* Dispatched calls with uncertain outcomes (timeouts, worker crashes, cancellations, or provider errors): The call was transmitted externally. The system enforces a fail closed policy by treating the spend as consumed. The cost is recorded in `uncertain_cost_minor` and `spent_cost_minor`, and deducted from `remaining_cost_minor`.

This invariant is verified by `test_timeout_after_dispatch_preserves_uncertain_spend` at [test_budget_crash_recovery.py:1046-1097](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L1046-L1097) and `test_budget_ceilings_enforced_across_timeouts_and_crashes` at [test_budget_crash_recovery.py:1098-1202](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L1098-L1202).

### 5. PostgreSQL Transaction Integrity, Concurrency, and Serialization
All reservation, reconciliation, and recovery operations strictly maintain parent first row locking:
* `ExecutionModel` rows are queried first with `with_for_update()`.
* `TaskModel` rows are queried second with `with_for_update()`.
* Every mutation commits within an isolated atomic transaction. Lock wait timeouts prevent deadlocks, while timestamp re-evaluations ensure stale workers cannot exploit lock queuing delays.

### 6. Comprehensive Test Coverage Across Regression Scenarios
The integration test suite in [test_budget_crash_recovery.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L1-L1202) contains 14 focused tests verifying the entire lifecycle:
* Lock wait safety: `test_lock_wait_lease_expiry_rejects_reservation` and `test_lock_wait_lease_expiry_rejects_reconciliation`.
* Reservation binding: `test_mismatched_or_missing_reservation_leaves_counters_unmodified`.
* Recovery concurrency: `test_recovery_racing_active_worker_preserves_live_reservation`.
* Uncertain spend handling: `test_timeout_after_dispatch_preserves_uncertain_spend` and `test_budget_ceilings_enforced_across_timeouts_and_crashes`.
* Worker concurrency and fencing: `test_concurrent_workers_budget_reservation_serialized_safely` and `test_stale_worker_cannot_reconcile_or_spend_after_lease_loss`.

## Strengths
* Robust lock wait safety: Prevents race conditions where worker leases expire during database lock acquisition queues.
* Strict parameter binding: Rejects invalid or mismatched reconciliation payloads with no ops, preserving counter integrity.
* Live task protection: Prevents aggressive background recovery loops from reclaiming reservations belonging to healthy active workers.
* Strict fail closed cost accounting: Preserves uncertain spend across timeouts, ensuring retried attempts respect customer budget limits.

## Test Coverage
The integration suite in [test_budget_crash_recovery.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L1-L1202) provides full end to end verification against real PostgreSQL for all budget reservation, reconciliation, timeout, and recovery branches.
