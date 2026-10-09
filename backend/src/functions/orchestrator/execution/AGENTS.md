# Orchestrator Execution Engine

## Overview

The orchestrator execution engine handles the durable execution, outbox relay, fenced task distribution, lease recovery, and LangGraph checkpoint reconciliation for SWENA asynchronous workflows. It ensures at-least-once queue delivery and exactly-once state machine progression using PostgreSQL as the single source of truth.

## Key files

| File | Owns |
|---|---|
| `models.py` | Canonical SQLAlchemy models for executions, tasks, worker results, outbox events, and completion receipts. |
| `daemon.py` | Continuous background process that manages queue polling and scheduler loops. |
| `consumer.py` / `completion_consumer.py` / `worker_consumer.py` | SQS message consumers using `asyncio.gather` for concurrent processing and partial batch responses. |
| `checkpointer.py` | Transaction-bound checkpointer that buffers writes and flushes atomically with the SQLAlchemy session. |
| `reconciliation.py` | Deduplicates completion events, resumes LangGraph execution safely using accepted result markers, and maintains execution state. |
| `fencing.py` | Fenced worker claim and lease renewals utilizing skip-locked patterns. |

## Commands

```bash
# Verify End-to-End trace
uv run python tests/integration/verify_e2e_trace.py
```

## Conventions

- **Transaction Boundaries**: Each execution phase (job acceptance, task dispatch, worker claim, worker result, relay, and graph resume) must execute within precisely defined, isolated database transactions. No transaction is held open while awaiting network calls or long-running computations.
- **Worker Fencing**: Task claiming and completion are protected by monotonic attempt counters and unique `lease_token`s. Stale workers whose leases have expired can never overwrite newer results or advance task state.
- **Checkpoint Rollback**: The `TransactionBoundCheckpointer` buffers checkpoint writes and clears them only after successful DB execution. A failure mid-flush retains pending writes for a clean retry.
- **SQS Partial Batch Responses**: Message handlers appropriately return `{ "itemIdentifier": message_id }` for individual message failures inside an `asyncio.gather` loop to leverage AWS Lambda's partial batch failure contract.
- **Stalled Receipts**: Recovery relies on `event_type` from `CompletionReceiptModel` instead of hardcoded strings to accurately identify the stalled event type (e.g., `TASK_COMPLETED`).
- **Orphan Tasks Cleanup**: The daemon uses a bounded timeout (`asyncio.wait(..., timeout=2.0)`) during shutdown cancellation to prevent zombie asyncio tasks while avoiding indefinite hangs.
