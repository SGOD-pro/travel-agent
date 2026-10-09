# Review, main, 2026-10-08

**Reviewed by**: inline senior review (author on sonnet)
**Scope**: 4 files, uncommitted changes
**Verdict**: Approve with nits

## Summary

This review re-examined the scheduled task recovery and worker lease fencing slice following the remediation of previous review findings against spec 0001. All blockers and major issues have been resolved cleanly. Worker failure and recovery retry outbox events now conform strictly to the worker message schema. Expired and cancelled PENDING tasks are recovered without altering healthy active retries. Nested savepoint isolation prevents dirty session state upon fencing rejection. Database lock timeouts and per task attempt limits are properly enforced.

## Nits

- ⚪ `backend/src/functions/orchestrator/execution/fencing.py:722`, `calc_backoff` evaluates to 30 seconds for initial attempts. If callers need immediate retry in test environments, they should pass `backoff_seconds=0`.
- ⚪ `backend/src/functions/orchestrator/execution/recovery.py:144`, Candidate scanning joins `executions` and filters on `(status, deadline_at)` and `(status, lease_expiry)`. A compound partial index on `tasks (status, deadline_at)` will optimize high throughput production workloads.

## Strengths

* Nested savepoint transaction boundaries inside `complete_task`, `fail_task`, `claim_task`, and `renew_lease` ensure failed fencing checks roll back flushed worker results immediately, leaving caller sessions completely clean and reusable.
* Robust candidate selection and lock recheck logic under parent first locks accurately identifies and recovers deadlocked PENDING tasks while preserving healthy pending retries in active backoff.
* Complete alignment of retry outbox event payloads with `TaskQueueMessage`, guaranteeing seamless worker consumer deserialization across both worker failure and recovery paths.
* Zero regressions across the full PostgreSQL integration and unit test suites with comprehensive regression tests covering every review finding.

## Test coverage

40 tests in `test_worker_claim_fencing.py` and `test_task_recovery.py` passed against live PostgreSQL, including dedicated regression tests for payload deserialization, savepoint transaction isolation, PENDING task recovery, lock recheck, and attempt limit configurations. Total repository test suite of 73 tests passes cleanly.
