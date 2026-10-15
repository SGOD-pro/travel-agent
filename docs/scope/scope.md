# Scope: SWENA Travel Intelligence

Asynchronous travel planning and intelligence platform with durable workflow execution.

**Build approach:** Tracer Bullet (thin end to end slices across canonical PostgreSQL persistence).
**Workflow:** GA (requires /check verify, /check review, and regression test suites before done).

## At a glance

| # | Feature | Phase | Status |
|---|---------|-------|--------|
| 1 | Orchestrator execution engine, transactional outbox, and reconciliation | Foundation | done |
| 2 | Evidence collection with provenance and canonical place/claim verification | Phase 4: Evidence & Verification | done |
| 3 | Browser escalation worker service and orchestrator dispatch | Phase 5: Browser Escalation | in-progress |

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

## Phase 4: Evidence & Verification

### 2. Evidence collection with provenance and canonical place/claim verification · done
Collect source evidence with immutable provenance and verify canonical places and claims following Phase 4 in `docs/DEVELOPMENT.md`, depending on accepted 0001 execution infrastructure. Strictly excludes routing, media, browser automation, full itinerary generation, and live AWS deployment.
**Done when:** evidence search and permitted page extraction store immutable source observations, link claims to canonical places, evaluate deterministic verification policy states, preserve contradictory or stale assertions, and execute via the 0001 worker registry on live PostgreSQL.
- [x] Design it (spec): [0002](../specs/0002-evidence-collection-and-verification.md)
- [x] Build it: `/develop evidence collection with provenance and canonical place/claim verification`
   - [x] Offline source-backed vertical slice with fixture adapter, SQS handler, and immutable observation persistence (AC-3, AC-14, AC-15, AC-16)
   - [x] Claim and place persistence models with PostGIS extensions (AC-5, AC-8)
   - [x] Live provider adapters for SerpAPI and Scrapling with SSRF checks (AC-1, AC-2, AC-4, AC-13, AC-14, AC-18)
   - [x] Canonical place resolution service with spatial band discovery (AC-6, AC-7)
   - [x] Deterministic Policy Engine for claim verification, freshness windows, and conflict preservation (AC-9, AC-10, AC-11, AC-12)
   - [x] Standalone SAM packaging template and verification suites (AC-17)
- [x] Verify it: `/check verify 0002-evidence-collection-and-verification`
   - [x] Fixture evidence verification: [verify.md](../specs/0002-evidence-collection-and-verification/verify.md)
   - [x] Claim and place persistence verification: [verify.md](../specs/0002-evidence-collection-and-verification/verify.md) (9 behaviors verified on live PostgreSQL)
   - [x] Live provider smoke test (credentialed with SERPAPI_API_KEY)
   - [x] Canonical place resolution and annular band spatial verification: [verify.md](../specs/0002-evidence-collection-and-verification/verify.md) (19 tests, index scan proof, and live PostGIS query proof)
   - [x] Deterministic policy engine verification: [verify.md](../specs/0002-evidence-collection-and-verification/verify.md) (12 tests and live database proof for authority, conflicts, and freshness)
   - [x] SAM packaging and connected source verification: [verify.md](../specs/0002-evidence-collection-and-verification/verify.md) (AC-17, package size limits, and end to end trace)
   - [x] Production evidence dispatch and adapter wiring verification: [verify.md](../specs/0002-evidence-collection-and-verification/verify.md) (single owner claim and completion, echo stub removal, live adapter wiring, and observation persistence)
   - [ ] Deployed AWS infrastructure evidence (deferred)
- [x] Review it: `/check review 0002-evidence-collection-and-verification` (2026-10-10 Slice 1 Approve with nits · 2026-10-11 Slice 2 Approve with nits · 2026-10-14 Slice 3 Approve · 2026-10-14 Slice 4 Approve · 2026-10-14 Slice 5 Approve with nits · 2026-10-14 Slice 6 Approve with nits · 2026-10-15 Production Dispatch Wiring Approve with nits · 2026-10-15 Standalone Packaging Approve)
- [x] Test it: `/test 0002-evidence-collection-and-verification` (289 tests passing across entire system · 183 tests for spec 0002)
- [x] Document it: `/document 0002-evidence-collection-and-verification`

Spec 0002 (`docs/specs/0002-evidence-collection-and-verification.md`, Status: Accepted) · code in `backend/src/functions/evidence_collector/`, `backend/src/functions/orchestrator/places/`, and `backend/src/functions/orchestrator/evidence/`
 
## Phase 5: Browser Escalation

### 3. Browser escalation worker service and orchestrator dispatch · in-progress
Dedicated serverless browser escalation worker to render dynamic client side web pages emitted from evidence collection, execute Playwright navigation with SSRF route filtering, and persist rendered observations under orchestrator lease fencing. Strictly excludes ECS or ECR container deployments and live AWS deployment.
**Done when:** browser tasks are claimed and completed under lease fencing, target URLs navigate under SSRF protection, rendered DOM HTML is captured and parsed with Scrapling, immutable source observations are persisted with extraction_method BROWSER_HTML, and the browser worker packages independently beneath AWS Lambda size ceilings.
- [x] Design it (spec): [0003](../specs/0003-browser-escalation.md)
- [ ] Build it: `/develop browser escalation worker service and orchestrator dispatch`
   - [ ] Browser domain contracts and offline fixture adapter (AC-5)
   - [ ] Orchestrator SQS transport routing and LangGraph escalation node (AC-1, AC-2)
   - [ ] Fenced browser worker service, observation persistence, and graceful error degradation (AC-3, AC-6, AC-7)
   - [ ] Playwright driver adapter with route level private IP and metadata interception (AC-4, AC-5)
   - [ ] SQS consumer Lambda handler, SAM template with Chromium layer, and combined packaging acceptance (AC-8)
- [ ] Verify it: `/check verify 0003-browser-escalation`
- [ ] Review it (fresh model): `/check review 0003-browser-escalation`
- [ ] Test it: `/test 0003-browser-escalation`
- [ ] Document it: `/document 0003-browser-escalation`

Spec 0003 (`docs/specs/0003-browser-escalation.md`, Status: Proposed) · code in `backend/src/functions/browser/` and `backend/src/functions/orchestrator/`
