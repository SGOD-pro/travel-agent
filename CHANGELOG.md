# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Core execution engine components for asynchronous travel planning workflows (see spec 0001). This includes PostgreSQL persistence migrations for executions, tasks, outbox events, worker results, completion receipts, job events, and API idempotency records.
- API job acceptance with idempotency reservations, owner isolated job timeline polling, and job cancellation endpoints (see spec 0001).
- Worker lease fencing service with optimistic concurrency controls, preventing stale worker claims and duplicate completions on live PostgreSQL (see spec 0001).
- Transactional outbox relay service that claims due events with skip locked semantics and publishes events using bounded batch limits (see spec 0001).
- Scheduled task recovery service that reclaims expired worker leases, applies exponential backoff, and marks exhausted attempts as failed (see spec 0001).
- Completion reconciliation service that deduplicates completion receipts, serializes execution row updates, and merges outputs into LangGraph checkpoint state (see spec 0001).
- Graph runner consumers for `EXECUTION_START` and `EXECUTION_RESUME` events, driving durable LangGraph workflow execution and continuation (see spec 0001).
- SQS consumer handlers and continuous background daemon supervision with crash detection, exponential backoff, and concurrent partial batch processing (see spec 0001).
- Worker task queue consumer and completion reconciliation consumer with explicit executor registry dispatching (see spec 0001).
- Transaction bound LangGraph checkpointer with atomic rollback of buffered writes when enclosing database transactions abort (see spec 0001).
- Connected end to end execution trace verifying the complete flow from HTTP acceptance through worker execution, reconciliation, graph continuation, and terminal status polling (see spec 0001). This connected test uses an injected test executor to verify execution engine wiring rather than real evidence collection.
- AWS SAM template defining SQS queues, dead letter queues with redrive policies, partial batch failure reporting, and EventBridge schedules (see spec 0001). Live deployed AWS behavior remains deferred until cloud rollout.
- Full test suite expanded to 90 passing test cases covering all isolation boundaries, regression scenarios, crash recovery loops, and checkpoint fencing on live PostgreSQL (see spec 0001).
