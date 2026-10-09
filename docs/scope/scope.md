# Scope: SWENA Travel Intelligence

Asynchronous travel planning and intelligence platform with durable workflow execution.

**Build approach:** Tracer Bullet (thin end to end slices across canonical PostgreSQL persistence).
**Workflow:** GA (requires /check verify, /check review, and regression test suites before done).

## At a glance

| # | Feature | Phase | Status |
|---|---------|-------|--------|
| 1 | Orchestrator execution engine, transactional outbox, and reconciliation | Foundation | done |

## Foundations

### 1. Orchestrator execution engine, transactional outbox, and reconciliation · done
Durable execution engine, transactional outbox relay, worker lease fencing, scheduled lease recovery, and LangGraph checkpoint reconciliation.
**Done when:** executions, tasks, outbox relay, worker leases, crash recovery, and checkpoint reconciliation run conflict free on live PostgreSQL with atomic transaction boundaries.
- [x] Design it (spec): `docs/specs/0001-orchestrator-execution-engine.md`
- [x] Build it: `/develop orchestrator execution engine`
   - [x] Canonical tables migration and idempotency reservation (AC-1..4)
   - [x] Worker claim and lease fencing slice (AC-6..8)
   - [x] Bounded transactional outbox relay slice (AC-12)
   - [x] Scheduled task recovery slice (AC-10..12) (verified and reviewed, two nits noted)
   - [x] LangGraph result application and checkpoint reconciliation slice (AC-13..14) (verified and reviewed)
   - [x] Graph runner consumers for EXECUTION_START and EXECUTION_RESUME events (verified and reviewed)
   - [x] Worker task dispatch queue polling loop and SQS event handler
   - [x] Completion receipt consumer wiring for TASK_COMPLETED reconciliation
   - [x] Continuous background daemon failure supervision and scheduler loop wiring for relay and recovery
   - [x] SAM bindings for SQS consumers, partial batch responses, DLQ redrive policies, and scheduled triggers
   - [ ] Deployed AWS infrastructure evidence (live cloud stack execution, queue redrive, and EventBridge schedules) (deferred)
- [x] Verify it: `/check verify 0001-orchestrator-execution-engine`
   - [x] Scheduled task recovery verification: [verify.md](../specs/0001-orchestrator-execution-engine/verify.md) (passed all 32 behaviors)
   - [x] Graph runner checkpoint protection verification: [2026-10-09-0001-graph-runner-verification.md](../archive/0001/2026-10-09-0001-graph-runner-verification.md) (passed all 8 behaviors)
   - [x] End to end execution trace verification (Graph Runner slice): passed ([report](../reviews/2026-10-09-0001-e2e-trace-verification.md), trace test [verify_e2e_trace.py](../../backend/tests/integration/verify_e2e_trace.py))
   - [x] End to end execution trace verification (Full Wiring): passed ([report](../reviews/2026-10-09-0001-e2e-trace-verification.md), trace test [verify_e2e_trace.py](../../backend/tests/integration/verify_e2e_trace.py))
- [x] Review it: `/check review 0001-orchestrator-execution-engine`
   - [x] Full execution engine review: [2026-10-08-0001-orchestrator-execution-engine.md](../archive/0001/2026-10-08-0001-orchestrator-execution-engine.md) (Approve with nits)
   - [x] Scheduled task recovery re review: [2026-10-08-scheduled-task-recovery-re-review.md](../archive/0001/2026-10-08-scheduled-task-recovery-re-review.md) (Approve with two nits)
   - [x] Graph runner verification review: [2026-10-09-main.md](../archive/0001/2026-10-09-main.md) (Blocked)
   - [x] Graph runner verification re-review: [2026-10-09-main-re-review.md](../archive/0001/2026-10-09-main-re-review.md) (Approve)
   - [x] Consumer layer final review: [2026-10-09-main-final-review.md](../archive/0001/2026-10-09-main-final-review.md) (Approve with nits)
   - [x] Execution engine final review: [2026-10-09-execution-engine-final-review.md](../reviews/2026-10-09-execution-engine-final-review.md) (Approve)
- [x] Test it: `/test 0001-orchestrator-execution-engine` (90 tests passing)

Spec 0001 (`docs/specs/0001-orchestrator-execution-engine.md`, Status: Accepted) · code in `backend/src/functions/orchestrator/execution/`
