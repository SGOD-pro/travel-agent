# Review: Durable Evidence Budget Reservation Crash Recovery (2026-11-16)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 5 files, commit f8d6578
**Verdict**: Approve

## Summary

This review evaluates commit f8d6578 on specification 0007 Slice 8, titled "feat(orchestrator): recover abandoned evidence budget reservations after worker crash". The commit introduces durable evidence budget reservation, reconciliation, and crash recovery conforming to specification 0001, specification 0002 (acceptance criterion 14), and backend audit finding 2. The solution makes budget reservations identifiable by execution, task, attempt, and lease token, recovers abandoned reservations safely when worker leases expire or tasks terminate abnormally, preserves uncertain provider spend by treating interrupted calls as consumed budget, prevents stale workers from reconciling or reserving after lease loss, guarantees idempotency on duplicate recovery and reconciliation, enforces parent first row locking on PostgreSQL, and maintains clean architectural boundaries between orchestrator execution and collector services.

## Evaluation of Review Criteria

### 1. Identifiable Reservations by Execution, Task, and Attempt
In [budget.py:130-152](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L130-L152), `reserve_budget_entry` creates discrete reservation entries indexed by a distinct `reservation_id` within the `execution.budget["reservations"]` dictionary. Each reservation records:
* `reservation_id`: Unique identifier for the reservation.
* `task_id`: Identifier of the child task holding the reservation.
* `attempt`: Task execution attempt index.
* `lease_token`: Worker lease token under which the reservation was made.
* `cost_minor`: Cost reserved in minor currency units.
* `status`: Set initially to `BudgetReservationStatus.ACTIVE`.
* Audit timestamps: ISO strings for `created_at`, `reconciled_at`, `recovered_at`, and `settled_as`.

This structure is verified by `test_reservations_identifiable_by_execution_task_attempt_and_lease` at [test_budget_crash_recovery.py:174-218](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L174-L218).

### 2. Safe Recovery of Abandoned Reservations After Lease Expiry or Crash
In [budget.py:259-324](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L259-L324), `recover_abandoned_budget_reservations_in_session` inspects active reservations and marks abandoned entries as `ABANDONED_UNCERTAIN`. It safely decrements `reserved_tool_calls` and `reserved_cost_minor`, preventing dangling reservations from leaking capacity.

In [recovery.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py), recovery is integrated into every exit branch:
* Parent execution cancelled at [recovery.py:445-450](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L445-L450)
* Parent execution terminal at [recovery.py:490-495](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L490-L495)
* Execution or task deadline exceeded at [recovery.py:532-537](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L532-L537)
* Stale context version at [recovery.py:574-579](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L574-L579)
* Attempt limit exhaustion at [recovery.py:632-637](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L632-L637)
* Task retry after worker lease expiry at [recovery.py:673-678](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L673-L678)

This behavior is confirmed by `test_abandoned_reservation_recovery_after_worker_crash_and_lease_expiry` at [test_budget_crash_recovery.py:219-289](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L219-L289).

### 3. Fail Closed Preservation of Uncertain Provider Spend
A critical principle of external provider integration is that interrupted or crashed external calls cannot be assumed free. In [budget.py:309-317](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L309-L317), when an abandoned reservation is recovered, its cost is charged against `spent_cost_minor` and `spent_tool_calls`, registered in `uncertain_cost_minor` and `uncertain_tool_calls`, and subtracted from `remaining_cost_minor` and `remaining_tool_calls`.

This guarantees that crashed attempts do not grant free retries that breach user configured budget limits. This constraint is verified by `test_retries_cannot_exceed_configured_budget_after_crash` at [test_budget_crash_recovery.py:290-369](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L290-L369).

### 4. Prevention of Stale Workers from Reconciling or Spending After Lease Loss
Workers that encounter transient pauses or network partitions must not corrupt state upon waking up after lease loss:
* In [budget.py:88-114](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L88-L114), `reserve_budget_entry` fences worker reservations by verifying `task.lease_token == lease_token`, `task.lease_expiry > now`, `task.attempt == attempt`, and `task.status in ("CLAIMED", "RUNNING")`.
* In [budget.py:188-202](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L188-L202), `reconcile_budget_entry` acquires an exclusive lock on the task row and verifies that the worker lease is still active and unmodified before accepting reconciliation. If the lease expired or was transferred to another worker, reconciliation is rejected.

This protection is verified by `test_stale_worker_cannot_reconcile_or_spend_after_lease_loss` at [test_budget_crash_recovery.py:370-463](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L370-L463).

### 5. Idempotent Recovery and Repeated Reconciliation
Network retries and multiple recovery sweeps must never double count budget metrics:
* In [budget.py:216-225](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L216-L225), `reconcile_budget_entry` checks whether a reservation is already settled or recovered, exiting early and returning `True` without re applying counter decrements.
* In [budget.py:281-283](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L281-L283), `recover_abandoned_budget_reservations_in_session` only processes reservations in `ACTIVE` status.

Idempotency is demonstrated by `test_duplicate_recovery_is_idempotent` at [test_budget_crash_recovery.py:464-521](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L464-L521) and `test_repeated_reconciliation_is_idempotent` at [test_budget_crash_recovery.py:522-588](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L522-L588).

### 6. Real PostgreSQL Transaction Integrity, Concurrency, and Serialization
The design strictly enforces consistent parent first lock ordering:
* `ExecutionModel` is locked first with `with_for_update()`, followed by `TaskModel` with `with_for_update()`, completely eliminating lock inversion deadlocks.
* In [test_budget_crash_recovery.py:589-683](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L589-L683), `test_concurrent_workers_budget_reservation_serialized_safely` exercises three concurrent workers contending for two available tool call slots via `asyncio.gather`. Exactly two workers acquire budget reservations while the third is safely rejected, proving serialization on PostgreSQL.
* In [test_budget_crash_recovery.py:684-740](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L684-L740), `test_parent_cancelled_recovers_budget_reservations` verifies transaction rollback and budget recovery on parent cancellation.

### 7. Architectural Separation Between Orchestrator Execution and Collector Domain
The architecture maintains clear modular boundaries:
* Core budget state machines and algorithms are centralized in [functions.orchestrator.execution.budget](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/budget.py#L1-L324).
* Scheduled recovery loops and outbox event emissions remain encapsulated within [functions.orchestrator.execution.recovery](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/recovery.py#L1-L820).
* The evidence collector in [functions.evidence_collector.service](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/service.py#L163-L248) consumes these execution primitives without reimplementing locking, serialization, or reservation state management.

## Strengths
* High integrity transaction model: Parent first locking guarantees deadlock prevention across concurrent tasks and background recovery sweeps.
* Robust fail closed safety: Treats interrupted external API calls as consumed spend, protecting against cost overruns during cascading worker failures.
* Complete fencing coverage: Validates task status, lease token, lease expiry, and attempt count at both reservation and reconciliation phases.
* Comprehensive integration test suite: 8 rigorous integration tests executed against real PostgreSQL verifying concurrency, idempotency, lease fencing, and budget limits.

## Test Coverage
The integration suite in [test_budget_crash_recovery.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_budget_crash_recovery.py#L1-L740) provides end to end validation for every scenario described in specification 0001, specification 0002 acceptance criterion 14, and audit finding 2.
