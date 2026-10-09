# Review: 0001-orchestrator-execution-engine · Approve with nits

Date: 2026-10-09
Reviewer: Claude Sonnet 4.6 (cross model; author was Gemini)
Scope: uncommitted changes on main, 28 files across the orchestrator execution layer

---

## Verdict: Approve with nits

No blockers. Two major findings (one confirmed resolved), one minor, one nit.
The execution engine core is sound.

---

## Findings

### Major: Lambda time budget guard confirmed present in all consumers

`consumer.py`, `completion_consumer.py`, and `worker_consumer.py` all check
`context.get_remaining_time_in_millis() < 2000` before each record and return
the unprocessed message IDs in `batchItemFailures`. The guard sits outside the
per-record session scope, so an in-flight session is always closed by the
context manager before the loop exits. Confirmed resolved by code inspection.

### Major: Daemon loop failure handling (Corrected Analysis)

The earlier review suggested changing `asyncio.gather(self._relay_loop(), self._recovery_loop())`
to `asyncio.gather(..., return_exceptions=True)`. That suggestion is flawed:
Since `_relay_loop` and `_recovery_loop` are intended to run indefinitely, passing
`return_exceptions=True` means if one loop terminates with an unhandled exception,
`gather` does NOT return because the sibling loop never finishes. The failed loop remains dead,
unlogged, and unrecovered, while the daemon continues running in a degraded state.
Neither unhandled `gather` nor `gather(..., return_exceptions=True)` is acceptable.

**Resolution**: Implement an explicit `DaemonSupervisor` that monitors running child tasks,
detects unexpected termination, provides configurable restart with backoff, surfaces
health status (`healthy`, `restarts`, `last_error`), and triggers graceful shutdown if
consecutive failures exceed a defined crash-loop threshold.

### Minor: SQS partial batch failure vs DLQ redrive (Corrected Analysis)

The earlier review claimed that deserialization/malformed errors should be omitted from
`batchItemFailures` to "trust the DLQ-on-maxReceiveCount policy". That claim is incorrect:
In AWS Lambda with SQS event source mapping and `ReportBatchItemFailures`:
- Any record ID omitted from `batchItemFailures` is treated by SQS as **acknowledged and successful**,
  causing SQS to immediately delete the message from the queue.
- A deleted message will **never** increment `ApproximateReceiveCount` and will **never** route to the DLQ.
- To route a malformed message to the DLQ via SQS redrive policy, it **must** fail (i.e. returned in
  `batchItemFailures`) until `ApproximateReceiveCount >= maxReceiveCount`.

**Resolution**: Preserve malformed-message inclusion in `batchItemFailures` so that SQS redrive
to DLQ functions as intended, unless an explicit application-level durable quarantine
table/queue is synchronously written.

### Nit: Inline `import uuid` inside function body (Resolved)

Resolved: Moved `import uuid` to module level in `completion_consumer.py`.

---

## Strengths

The execution engine design is correct and complete against the spec:

- `WorkerFencingService.claim_task()` is called with a well-formed
  `TaskQueueMessage`. Lease races and stale dispatches are discarded gracefully
  without polluting the batch failure list.
- `CompletionReconciliationService` receives properly typed `uuid.UUID` values.
  The earlier bug where string IDs produced `ExecutionMismatchError` with
  identical-looking UUID strings is fixed.
- `OutboxRelayService` and `TaskRecoveryService` are wired correctly in
  `daemon.py` with per-iteration lease tokens and appropriate sleep intervals.
- Transaction boundaries are correct across all consumers. Each consumer opens
  its own session and commits before returning. Fencing uses atomic SQL with
  `RETURNING id`, so a missed commit cannot silently succeed.
- The `SqsTransportAdapter` in `daemon.py` reads queue URL and region from
  environment variables, which is the correct pattern for Lambda and ECS.
- All eight checkpoint protection behaviors pass against live PostgreSQL.
- The e2e trace (`verify_e2e_trace.py`) drives the full stack: HTTP acceptance,
  outbox creation, EXECUTION_START consumption, graph execution, TASK_DISPATCH
  claim, TASK_COMPLETED reconciliation.

---

## Summary

0 blockers · 2 major (1 confirmed resolved, 1 latent daemon risk) · 1 minor
· 1 nit · reviewed by Claude Sonnet over 28 files.

Full spec: `docs/specs/0001-orchestrator-execution-engine.md`
Scope: ticked `Review it` box in `docs/scope/scope.md`.
