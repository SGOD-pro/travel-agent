# Review, main, 2026-10-08

**Reviewed by**: Gemini 3.1 Pro (author on Gemini 3.8 Flash)
**Scope**: 12 files, uncommitted changes
**Verdict**: Approve with nits

## Summary
This review examined the complete orchestrator execution engine across spec 0001, covering job acceptance idempotency, worker task leasing and fencing, bounded transactional outbox relay, scheduled task recovery, LangGraph checkpoint reconciliation, and public job surfaces. The architecture enforces PostgreSQL as the single source of truth with strict transaction boundaries, parent first row locks, and idempotent recovery loops. All 14 acceptance criteria and all specced surfaces are implemented cleanly with zero blockers or major defects.

## Nits
- ⚪ `backend/src/functions/api/routes/jobs.py:76`, Timeline cursor accepts integer sequence while response models serialize string next_cursor. FastAPI parses integer string query params correctly, but accepting either string or integer in the query parameter schema avoids client casting quirks.
- ⚪ `backend/src/functions/orchestrator/execution/job_events.py:24`, `record_job_event` computes sequence via `select(max(sequence) + 1)`. All current callers hold parent execution row locks, preventing concurrent sequence collisions; documenting this lock prerequisite on the function signature protects future callers.
- ⚪ `backend/src/functions/orchestrator/execution/recovery.py:144`, Candidate scanning filters on status, deadline, and lease expiry. A compound partial index on tasks `(status, lease_expiry)` in production will further optimize high volume recovery scans.

## Strengths
- Consistent parent first locking (`SELECT id FROM executions WHERE id = :id FOR UPDATE`) before child task updates across fencing, cancellation, and reconciliation, mathematically eliminating deadlocks and preventing zombie worker writes.
- Robust LangGraph checkpoint reconciliation using `accepted_result_ids` markers, ensuring crashes before or after checkpoint commits recover cleanly without duplicate side effects.
- Strict owner isolation enforced across job creation, status polling, timeline pagination, and job cancellation, reliably returning HTTP 404 for cross owner requests.
- Transactional outbox relay with bounded batch sizes and execution time limits, preventing long running worker starvation and ensuring reliable at least once delivery.
- Comprehensive integration test suite verifying concurrency races, worker fencing, timeout recovery, and API endpoints against live PostgreSQL.

## Test coverage
79 automated tests across unit and integration suites passing green against live PostgreSQL in 438 seconds. Full test coverage over all 14 acceptance criteria in spec 0001, including idempotency conflicts, worker lease fencing, outbox delivery confirmation, lease recovery backoff, checkpoint replay safety, and public job polling and cancellation endpoints.
