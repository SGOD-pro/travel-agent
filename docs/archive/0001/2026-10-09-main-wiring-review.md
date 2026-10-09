# Review, main (uncommitted), 2026-10-09

**Reviewed by**: Claude Sonnet 4.6 (Thinking) (author on Gemini)
**Scope**: 62 files, uncommitted
**Verdict**: Changes requested

## Summary

This change implements the full runtime integration layer for the orchestrator execution engine: three SQS Lambda consumers (graph runner, worker, completion receipt), three bounded EventBridge-scheduled handlers (relay, recovery, reconciliation), a `BackgroundDaemon` supervisor for local continuous operation, a `TransactionBoundCheckpointer` for atomic LangGraph state persistence, and the SAM infrastructure template. The design is genuinely strong: parent-first lock ordering, transactional outbox, idempotent checkpoint markers, and `ReportBatchItemFailures` partial batches are all correctly implemented. Tests are thorough and pass against real PostgreSQL. Three issues need fixing before this is merge-ready; none are data-loss blockers in the test suite, but two are silent failure modes that will be invisible in production.

## Major

### 🟠 `worker_consumer.py` uses a stub worker in production, `worker_consumer.py:25-28`

**Problem**: `simulate_worker_execution` is the only worker implementation wired into the Lambda handler path. It sleeps 100ms and returns a mock result. When `WorkerTaskConsumerFunction` runs in production it will claim tasks, call this stub, and complete them with fake output silently.

**Why it matters**: Tasks across all worker types and actions are silently marked `COMPLETED` with `{"status": "mock_success"}`. Graph resumption proceeds on garbage data. No error is raised and no log indicates it is fake — this is an invisible correctness failure in production.

**Suggested fix**: Replace `simulate_worker_execution` with a real worker dispatch registry keyed on `(worker_role, action)`, or make the mock explicitly raise `NotImplementedError` when `ENVIRONMENT != "test"` to prevent silent fake completions from reaching production. The comment on line 56 acknowledges this is intentional for "this trace" but it is currently wired as the live Lambda handler.

---

### 🟠 `SqsTransportAdapter` local fallback is a crash loop, `daemon.py:98-104`, `scheduled.py:59-60`

**Problem**: When `boto3` is not importable, `BackgroundDaemon` and the scheduled handlers construct `SqsTransportAdapter(queue_url="dummy-local", sqs_client=None)`. The adapter raises `RuntimeError("SQS client is not configured")` on every `publish()` call at `transport.py:97`. In the daemon relay loop this is caught per-message as a delivery failure, logged as an error, and triggers exponential backoff — an infinite retry loop that burns CPU and fills logs. In the scheduled handlers the error propagates and fails the Lambda invocation.

**Why it matters**: A developer running locally without boto3 gets a cascading error loop rather than a clean "local mode, SQS disabled" path. The intent was a local no-op transport but the implementation is a crash-loop.

**Suggested fix**: Add a `NullTransportAdapter` (logs a warning, returns immediately without error) and use it as the local fallback. `SqsTransportAdapter` should always require a real client and raise at construction time if `sqs_client is None`, not at publish time.

---

### �� `checkpointer.py` `flush()` can silently discard pending writes, `checkpointer.py:464-472`

**Problem**: In `flush()`, `_pending_flushes.pop(thread_id, [])` removes the pending items regardless of whether they are subsequently written. If any write fails mid-loop (e.g. a DB error on item 3 of 10), items 1-2 are already written, items 3-10 are lost (popped from the buffer and never retried), and the method raises — but the buffer is already empty so a retry of `flush()` writes nothing. Additionally, when `_session_factory is None` and `session.bind` is also `None` (lines 459-462), the flush silently no-ops: pending items are popped and discarded.

**Why it matters**: Partial flush failure silently loses LangGraph checkpoint writes. On the next invocation the graph reads a stale checkpoint and may re-execute nodes or miss task outputs entirely — a silent state divergence rather than a recoverable error.

**Suggested fix**: Pop pending items into a local variable only after all writes succeed, or keep the buffer intact on exception and re-raise so the transaction rolls back and the buffer is retried. Add an explicit guard: if `_session_factory is None` and `pending` is non-empty, raise `RuntimeError` to make misconfiguration visible.

## Minor

### 🟡 Stalled-receipt recovery hardcodes `TASK_COMPLETED`, `reconciliation.py:607`

**Problem**: `reconcile_stalled_receipts` reconstructs every stalled receipt as `event_type="TASK_COMPLETED"` regardless of the original event type. A stalled `TASK_FAILED` receipt replays as a completion.

**Why it matters**: Failed tasks that stall before their receipt is applied will be incorrectly treated as successful, potentially triggering `EXECUTION_RESUME` instead of `EXECUTION_FAILED`. Rare in practice but wrong when it happens.

**Suggested fix**: Add an `event_type` column to `completion_receipts` and use it during stalled-receipt recovery.

---

### 🟡 `process_batch` processes records sequentially, `consumer.py:83-103`

**Problem**: All three consumers iterate records in a `for` loop, awaiting each `process_record` serially. With `BatchSize: 10` and LangGraph invocations + multiple DB roundtrips per record, tail latency multiplies by 10x.

**Why it matters**: Lambda timeout is 60s. Ten sequential LangGraph invocations at 5-6s each saturate the budget on a full batch. The per-record budget check only fires at record boundaries so the last records get the full remaining budget regardless.

**Suggested fix**: Use `asyncio.gather(*[process_record(r, runner) for r in records], return_exceptions=True)` and map exceptions back to `batchItemFailures`.

---

### 🟡 Daemon monitor raises with potentially un-cancelled orphan tasks, `daemon.py:269-281`

**Problem**: When a worker reaches `FAILED` state, `_supervision_monitor` raises `DaemonSupervisorError`. If the monitor just created a new worker task via `asyncio.create_task` (line 269) for a different worker in the same iteration before detecting the FAILED state, that task may escape `stop()`'s cancel sweep depending on timing.

**Why it matters**: Resource leak in local daemon mode. Task runs after the daemon nominally stopped.

**Suggested fix**: In `stop()`, add a bounded `asyncio.wait` with timeout after cancellation before the final gather, to ensure all tasks are actually done.

## Nits

- ⚪ `daemon.py:88`: hardcoded fallback SQS URL `https://sqs.us-east-1.amazonaws.com/123456789012/swena-outbox.fifo` is a placeholder that will silently succeed at construction. Move to a required env var with no default, or raise immediately if the URL is clearly a placeholder.
- ⚪ `daemon.py:313`, `consumer.py:29,35,64`, `completion_consumer.py:30,35`, `worker_consumer.py:37,42`: f-strings in `logger.error(f"...")` calls; prefer `logger.error("...", var)` with %-formatting (ruff I001 also flags some of these).
- ⚪ `scheduled.py:44-60` and `daemon.py:82-104`: boto3 client construction is duplicated. Extract to a shared `_get_sqs_transport(context)` helper in `transport.py`.
- ⚪ `checkpointer.py:104-105`: `max(checkpoints.keys())` assumes lexicographically sortable checkpoint IDs. LangGraph currently uses ULIDs which satisfy this, but the assumption is undocumented. Add a comment.
- ⚪ `daemon.py:313`: loop variable `name` unused (`for name, task in ...`). Rename to `_name` (ruff B007).
- ⚪ `worker_consumer.py:543` (task_id): `attempt=0` in `TaskModel` during dispatch but workers see `attempt=1` after `claim_task` increments. No bug (fencing handles it), but the `0` is confusing without a comment explaining it is the pre-claim sentinel value.

## Strengths

- Parent-first lock ordering (executions → tasks) is consistently applied across reconciliation and the runner with no exceptions found. The `SKIP LOCKED` relay and lease fencing predicates combine correctly: no dual-write window exists.
- `TransactionBoundCheckpointer` is a genuinely elegant design: buffering LangGraph writes in-memory and flushing atomically with the outbox/task commit eliminates the entire class of checkpoint-divergence crash scenarios. The `discard()` method correctly purges buffered state on all rollback paths.
- `ReportBatchItemFailures` is correctly wired in all three consumers; malformed messages return precise `itemIdentifier` entries so SQS redrive policies engage on the failing record only, not the whole batch.
- Integration test coverage is well above the usual bar: concurrent race tests, stale-context rejection, deadline and cancellation barriers, crash-before and crash-after checkpoint injection, and the connected 6-step E2E trace all exercise real PostgreSQL behavior.

## Test coverage

Test signal: `TESTS = none-yet` (no `test-preferences.json`, but 64 integration tests pass).

Gaps: the three Lambda consumer `process_batch` functions have no direct unit tests for their error paths (malformed JSON, missing fields, Lambda timeout budget cutoff). The stub worker in `worker_consumer.py` is untested against a real dispatch path since none exists yet. Recommend adding these before the next deployment gate.
