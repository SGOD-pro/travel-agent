# Review, main, 2026-10-08

**Reviewed by**: inline senior review (author on sonnet)
**Scope**: 4 files, uncommitted changes
**Verdict**: Blocked

## Summary

This review examined scheduled task recovery and its integration with worker lease fencing against spec 0001. The implementation demonstrates excellent parent first lock hierarchy design, clean single task transaction boundaries, starvation prevention, and verified atomic rollback. However, one blocker in worker failure retry dispatch and two major issues in pending task recovery and uncommitted session dirty states need remediation before this slice is ready to merge.

## Blockers

### 🔴 Missing execution id and dispatch fields in worker failure retry outbox payload, `backend/src/functions/orchestrator/execution/fencing.py:749`

**Problem**: When a worker reports a retry eligible task failure in `fail_task`, the service emits a retry outbox event of kind `TASK_DISPATCH`. The current payload contains only `task_id`, `result_id`, and `attempt`. It omits `execution_id`, `worker`, `action`, `context_version`, and task input payload.

**Why it matters**: When the transactional outbox relay publishes this event to the queue, worker consumers construct a `TaskQueueMessage` object or pass the dictionary to `claim_task`. Because `execution_id` is a mandatory field with no default, message parsing fails with a validation or key error. This completely breaks retry dispatch for worker reported failures. In contrast, `TaskRecoveryService` populates all necessary fields in its `TASK_DISPATCH` events.

**Suggested fix**: In `fail_task`, when creating the `TASK_DISPATCH` outbox event for retry eligible failures, populate `execution_id`, `worker`, `action`, `context_version`, and `payload` matching the contract produced by `TaskRecoveryService`.

## Major

### 🟠 Unscanned pending tasks with expired deadlines or cancelled parents, `backend/src/functions/orchestrator/execution/recovery.py:108`

**Problem**: The candidate selection query in `recover_expired_leases` filters strictly on `TaskModel.status.in_(["CLAIMED", "RUNNING"])` and `lease_expiry <= now`. It does not scan tasks that are in status `PENDING` when execution or task deadlines expire or when parent executions are cancelled during retry backoff.

**Why it matters**: If a task enters retry backoff in status `PENDING` and its deadline passes before a worker claims it, the task remains `PENDING` in the database forever. When a worker subsequently attempts to claim it, `claim_task` detects the expired deadline or cancellation and raises an exception with a transaction rollback. Because recovery ignores `PENDING` tasks, no `TASK_FAILED` or `TASK_CANCELLED` event is ever emitted, and the task status is never updated to `FAILED`. Orchestrator joins waiting on task completion can stall indefinitely.

**Suggested fix**: Add a recovery scan for tasks where `status = 'PENDING'` whose `deadline_at` has expired or whose parent execution is in a terminal or cancelled state, and emit terminal outbox events to unblock joins.

### 🟠 Dirty session state on stolen or expired lease fencing rejection, `backend/src/functions/orchestrator/execution/fencing.py:556`

**Problem**: In both `complete_task` and `fail_task`, the service creates and flushes a `WorkerResultModel` row to the database before executing the fenced update query on the `tasks` table. If the fenced update matches zero rows because the lease token expired or was stolen, the method raises `LeaseFencingError` without rolling back the flushed session.

**Why it matters**: The flushed worker result row remains in the session transaction. If the caller catches `LeaseFencingError` and reuses or commits that session, an orphan result row for an unverified attempt is permanently committed to PostgreSQL.

**Suggested fix**: Catch fencing update failures, execute `await self._session.rollback()` to clean up the flushed worker result row, and then raise `LeaseFencingError`.

## Minor

### 🟡 Missing execution id inside complete task outbox event payload, `backend/src/functions/orchestrator/execution/fencing.py:569`

**Problem**: In `complete_task`, the `TASK_COMPLETED` outbox event populates `payload_ref` with `task_id` and `result_id` but omits `execution_id`.

**Why it matters**: While `execution_id` is stored on the table column, event consumers that read event payload dictionaries directly (such as `CompletionReconciliationService.reconcile_task_event`) parse `event["execution_id"]` and raise a key error if it is absent.

**Suggested fix**: Include `"execution_id": str(execution_id)` in `payload_ref` and `payload` for `TASK_COMPLETED` events.

### 🟡 Direct lease recovery queries lack database lock and statement timeouts, `backend/src/functions/orchestrator/execution/recovery.py:88`

**Problem**: `TaskRecoveryService.recover_expired_leases` runs row lock queries without setting database statement or lock timeouts. While `run_recovery_loop` bounds total elapsed time with `asyncio.wait_for`, direct calls to `recover_expired_leases` could block if another transaction holds an unreleased row lock.

**Why it matters**: A serverless invocation running `recover_expired_leases` directly could be held until the Lambda runtime hard kills the process.

**Suggested fix**: Set a local lock timeout on the session or ensure all callers run recovery through `run_recovery_loop`.

## Nits

* ⚪ `backend/src/functions/orchestrator/execution/recovery.py:305`, `max_attempts_limit = min(task.max_attempts, self._max_attempts)` caps custom task attempt limits at the service default of 3 unless the service is explicitly initialized with a higher value.

## Strengths

* Consistent parent first lock ordering (`executions` then `tasks`) across all recovery and worker mutations, mathematically preventing deadlock cycles with concurrent cancellations.
* Single task discrete transactions in recovery ensure that lock contention or errors on one task do not abort the recovery of other tasks in the batch.
* Candidate starvation prevention through `exclude_task_ids` ensures skipped tasks do not block processing of subsequent expired leases.
* Multi layered retry backoff fencing on both task status and lease expiry timestamps protects against premature claims and stale queue replays.

## Test coverage

30 comprehensive integration tests in `test_task_recovery.py` and `test_worker_claim_fencing.py` verify parent first lock ordering, concurrent recovery synchronization, zombie worker rejection, retry backoff enforcement, and atomic rollbacks against live PostgreSQL.
