# 0003. Browser Escalation Through the Orchestrator

**Date**: 2026-10-15  
**Status**: Proposed

## Summary

This specification defines the browser escalation worker and orchestrator workflow integration for dynamic web pages that require JavaScript rendering. When static evidence collection detects a client side application or bot verification challenge, the orchestrator inspects the resulting requests and dispatches targeted browser rendering tasks under strict concurrency and budget limits. A dedicated browser worker executes these tasks using Playwright and an external Chromium layer within an independent AWS Lambda package, ensuring the primary evidence collector stays lean and zero container infrastructure is introduced. All rendered page captures are persisted as immutable source observations linked to original placeholder records, guaranteeing graceful degradation when pages time out or block automation.

## Context

The SWENA travel intelligence platform gathers lodging, transit, and advisory evidence to verify traveler plans. In specification 0002, static evidence collection was established using SerpAPI for organic discovery and Scrapling for CSS extraction. That evidence collector runs as an isolated AWS Lambda function strictly bounded beneath 50 MB compressed and 250 MB uncompressed, deliberately omitting heavy browser automation dependencies such as Playwright and Chromium.

When Scrapling encounters web pages with empty single page application containers, noscript tags, or bot verification notices, it emits a `NEEDS_BROWSER` outcome. This outcome records a placeholder observation in PostgreSQL and attaches a list of `browser_requests` to the task output. Currently, these requests terminate as informative placeholders because no browser execution capability exists in the workflow.

Without browser escalation, dynamic web pages like modern hotel portals, train reservation systems, and interactive municipal advisories remain unextracted. Simply bundling browser engines into the existing evidence collector Lambda would violate AWS Lambda size limits, lengthen cold starts, and mix static scraping with heavy browser lifecycles. Running dedicated container services using Amazon ECS or ECR would introduce operational complexity and infrastructure overhead contrary to project constraints.

A dedicated serverless browser escalation worker is required. It must operate under the established orchestrator execution engine invariants: strict parent first database locking, monotonic attempt progression, lease fencing, transactional outbox dispatch, and write once immutable observation persistence. The orchestrator workflow graph must supervise escalation bounds, capping browser dispatches to prevent runaway costs while allowing partial results to proceed smoothly.

## Requirements

**User stories**:
- As a travel planning orchestrator, I want dynamic web pages requiring JavaScript execution to escalate to a specialized browser worker so that the system gathers rich lodging, transit, and advisory observations from client rendered sources.
- As an infrastructure operator, I want the browser worker to package independently as a serverless Lambda function without ECS or ECR container overhead so that deployment remains simple and cost effective.
- As a system observer, I want rendered page snapshots saved with immutable provenance and clear error boundaries so that failed renders degrade gracefully without failing the entire trip planning workflow.

**Acceptance criteria**:
- **AC-1**: When an evidence collection task completes with `browser_requests` records in its output, the orchestrator LangGraph workflow evaluates the remaining execution budget and enqueues up to 3 `BROWSER` worker tasks in request emission order for asynchronous dispatch.
- **AC-2**: The orchestrator transport layer routes `worker="BROWSER"` task dispatches to the dedicated browser queue URL via `BROWSER_WORKER_QUEUE_URL` environment configuration, preserving modular boundaries without importing browser code into orchestrator modules.
- **AC-3**: The browser worker claims and executes tasks under `WorkerFencingService` lease fencing, validating parent execution status, context version, and monotonic attempt counters before starting browser automation.
- **AC-4**: Target URLs and dynamic subresource requests are evaluated against SSRF security policies using self contained browser network route filtering without cross worker imports, blocking access to private network ranges, loopback addresses, and cloud metadata services (169.254.169.254).
- **AC-5**: The browser worker uses an abstract `BrowserExtractionPort` with a `PlaywrightBrowserAdapter` for live execution and a `FixtureBrowserAdapter` for offline tests, navigating with bounded page timeouts and capturing rendered DOM HTML.
- **AC-6**: Rendered content is parsed into clean entity observations using Scrapling and persisted as an immutable `SourceObservationModel` with `extraction_method="BROWSER_HTML"`, referencing the original `NEEDS_BROWSER` placeholder observation ID in `artifact_ref` without in place row mutations.
- **AC-7**: When browser navigation times out, crashes, or encounters bot verification challenges, the worker records an explicit unavailable source entry in `worker_results` and completes the task successfully under lease fencing, allowing the parent workflow to continue without failing the overall execution.
- **AC-8**: The browser worker packages independently into an isolated AWS Lambda artifact adhering to the 250 MB uncompressed ceiling, relying on an external precompiled Chromium layer in `/opt` and isolating browser dependencies from the evidence collector and orchestrator.

## Options considered

### Option 1: Dedicated serverless browser worker Lambda with Chromium layer (Chosen)

A separate Lambda function running in `backend/src/functions/browser/` that consumes messages from a dedicated SQS queue. It uses Playwright Python with an external precompiled Chromium layer (such as Sparticuz Chromium located in `/opt`), keeping function code lean and packaging independent.

**Pros**:
- Preserves independent Lambda deployment and lean packaging for both evidence collector and browser worker.
- Eliminates container management overhead, ECS clusters, and ECR image registries.
- Reuses 0001 lease fencing, outbox relay, and completion reconciliation without structural changes.
- Supports clean offline fixture testing with zero binary dependencies.

**Cons**:
- Lambda memory must be provisioned at 1536 MB to support headless Chromium comfortably.
- Cold starts with browser initialization are higher than lightweight static scraping.

### Option 2: Monolithic bundling of Playwright and Chromium into evidence collector Lambda

Bundle Playwright and headless browser binaries directly into the existing `evidence_collector` package so that dynamic pages render in place during the initial collection task.

**Pros**:
- Eliminates the need for a secondary task dispatch and queue.

**Cons**:
- Exceeds the 250 MB uncompressed AWS Lambda package ceiling when combined with Scrapling and parsers.
- Forces all evidence tasks to pay high memory and cold start penalties even when only static scraping is needed.
- Violates architectural modularity conventions established in `backend/AGENTS.md`.

### Option 3: Dedicated long running ECS or ECR container service

Deploy browser automation as a containerized worker service running on Amazon ECS Fargate, pulling Docker images from Amazon ECR.

**Pros**:
- Unrestricted disk space and memory without Lambda size ceilings.
- Persistent browser processes can reduce repeated browser launch overhead.

**Cons**:
- Violates explicit user instruction forbidding ECS and ECR infrastructure.
- High fixed baseline cost for idle container tasks.
- Substantially more complex deployment, networking, and monitoring topology.

## Decision

**Chosen option**: Option 1: Dedicated serverless browser worker Lambda with Chromium layer.

The browser escalation worker will be built as an independent AWS Lambda consumer using Playwright Python and an abstract driver port, routing through dedicated SQS queues and utilizing an external precompiled Chromium layer.

## Rationale

Option 1 provides the optimal balance of architectural isolation, cost efficiency, and operational simplicity. By separating dynamic browser execution into its own worker domain, the primary evidence collector Lambda remains extremely fast, lightweight (under 35 MB compressed), and resilient. Browser execution occurs only when genuinely needed, consuming higher memory allocations only for dynamic targets.

Reusing the established 0001 execution engine infrastructure guarantees consistency across the entire backend. The browser worker claims tasks via `WorkerFencingService`, respects monotonic attempt counters, updates execution budgets, and emits completion events through the transactional outbox. Reusing `source_observations` with `extraction_method="BROWSER_HTML"` maintains write once immutable provenance while allowing downstream canonical place resolution and deterministic claim verification to operate without awareness of whether evidence was harvested statically or dynamically.

## Feature design

**Data model sketch**:
Zero schema migrations are required. The feature completely reuses existing canonical PostgreSQL tables:

1. `tasks`:
   - `worker`: `'BROWSER'`
   - `action`: `'render'`
   - `payload_ref`: JSONB holding `url`, `canonical_url`, `category`, `reason`, `wait_until`, `timeout_seconds`, `selector_target`, and `placeholder_observation_id`
   - `idempotency_key`: `f"{execution_id}:render:{canonical_url_hash}"`
   - `status`: `PENDING` -> `CLAIMED` -> `RUNNING` -> `COMPLETED` or `FAILED`
2. `worker_results`:
   - `output_ref`: JSONB holding `status`, `canonical_url`, `effective_url`, `observation_id`, `render_duration_ms`, `rendered_html_bytes`, `unavailable_reason`, and `usage`
3. `source_observations`:
   - `extraction_method`: `'BROWSER_HTML'`
   - `artifact_ref`: JSONB holding `supersedes_placeholder_id`, `render_duration_ms`, `viewport`, `wait_until`, and `rendered_html_snippet`
   - `excerpt_text`: substantive rendered text up to 1000 characters
   - `content_hash`: SHA256 of rendered text
   - Unique constraint: `(execution_id, canonical_url, content_hash)`
4. `outbox_events`:
   - Emits `TASK_DISPATCH` (`worker="BROWSER"`, `action="render"`) and `TASK_COMPLETED`

**State transitions**:
The browser escalation flow executes across eight distinct phases:

```
[Evidence Collector Task]
         |
         v (detects dynamic page)
[Placeholder Observation (NEEDS_BROWSER)]
         |
         v (task complete with browser_requests)
[Outbox Relay: TASK_COMPLETED]
         |
         v
[Completion Reconciliation Service]
         |
         v (LangGraph resume)
[Dynamic Escalation Node in Graph]
         | (evaluates budget, enqueues up to 3 tasks)
[Pending BROWSER Tasks in DB]
         |
         v (Outbox Relay: TASK_DISPATCH)
[BROWSER SQS Queue]
         |
         v
[Browser Worker Consumer]
         | (claims task under lease fencing)
[Playwright Chromium Navigation (with SSRF Route Interception)]
         |
         v
[Hydrated DOM HTML Captured]
         |
         v
[Scrapling Entity Extraction]
         |
         v
[Save Observation (BROWSER_HTML) & Complete Task Atomically]
         |
         v (Outbox Relay: TASK_COMPLETED)
[Orchestrator Graph Resumes with Rendered Observations]
```

**API surface**:
The browser worker exposes zero public HTTP endpoints. It operates entirely as an asynchronous SQS consumer with partial batch failure reporting:

| Component | Interface | Inputs | Outputs | Auth / Scope | Key Errors |
|---|---|---|---|---|---|
| Browser Consumer | SQS Event Handler | SQS Batch (`Records` with `TASK_DISPATCH` payload) | `{"batchItemFailures": [{"itemIdentifier": id}]}` | IAM Lambda SQS execution role | Malformed JSON, unparseable envelope |
| Browser Service | `execute_render_task` | `TaskDispatchEnvelope`, `session`, `driver` | `tuple[UUID, UUID | None]` (`result_id`, `observation_id`) | Worker lease fencing token | LeaseFencingError, ParentCancelledError |
| Escalation Node | LangGraph Node | `TripPlanningState` with `task_outputs` | `{"pending_tasks": [task_specs], "current_step": "browser_escalated"}` | Internal workflow execution | BudgetExhausted, InvalidState |

**Value sourcing**:
Every value required by the acceptance criteria and worker tasks is explicitly sourced:

| Action | Value produced / displayed | Source |
|---|---|---|
| Graph Escalation | `browser_task.url` | `evidence_task.output_ref["browser_requests"][i]["url"]` |
| Graph Escalation | `browser_task.canonical_url` | `evidence_task.output_ref["browser_requests"][i]["canonical_url"]` |
| Graph Escalation | `browser_task.category` | `evidence_task.output_ref["browser_requests"][i]["category"]` |
| Graph Escalation | `browser_task.reason` | `evidence_task.output_ref["browser_requests"][i]["reason"]` |
| Graph Escalation | `browser_task.wait_until` | Configured strategy (defaults to `"networkidle"`) |
| Graph Escalation | `browser_task.timeout_seconds` | Derived from remaining execution deadline or fixed 20 seconds |
| Graph Escalation | `browser_task.placeholder_id` | Matched from `evidence_task.output_ref["observations"]` where `extraction_method="NEEDS_BROWSER"` |
| Browser Worker | `source_observations.excerpt_text` | Clean text extracted from hydrated DOM body (up to 1000 characters) |
| Browser Worker | `source_observations.content_hash` | SHA256 hex digest of clean rendered excerpt text |
| Browser Worker | `source_observations.artifact_ref` | Dict containing `supersedes_placeholder_id`, `render_duration_ms`, and render metadata |
| Browser Worker | `worker_results.output_ref` | Dict containing `status`, `observation_id`, `rendered_html_bytes`, and parsed domain entities |

**Key invariants**:
- **Strict Budget Ceiling**: A single execution cannot spawn more than 3 browser escalation tasks total.
- **Write Once Immutability**: Existing `NEEDS_BROWSER` placeholder observations in PostgreSQL are never mutated in place or deleted. Rendered observations are inserted as new rows with `extraction_method="BROWSER_HTML"`.
- **SSRF Safety**: Pre navigation DNS checks and Playwright route filtering abort any requests targeting private IP subnets, loopback interfaces, or cloud metadata IP addresses.
- **Fail Safe Graceful Degradation**: Page render timeouts, navigation aborts, or bot challenge blocks record an explicit unavailable source record and complete the task successfully under lease fencing without failing the parent execution.
- **Independent Packaging**: The browser worker artifact contains only browser consumer code and permitted shared contracts. It never imports optimizer, API, or orchestrator graph code, and evidence collector never imports browser code.

**Security model**:
- **Network Boundaries**: Pre navigation URL validation using `validate_safe_url`. During page execution, Playwright route interception inspects every HTTP request URL and aborts any subresource targeting forbidden hostnames or non public IP ranges.
- **Fenced Execution**: Task claims require valid parent execution status (`RUNNING` or `WAITING_TASKS`), matching context versions, and unexpired lease tokens.
- **Zero Secrets in Code**: Environment configuration manages queue URLs and browser binary paths.

**Configuration required**:
- `BROWSER_WORKER_QUEUE_URL`: Amazon SQS queue URL for browser task dispatch.
- `CHROMIUM_PATH`: Path to Chromium executable (defaults to `/opt/bin/chromium` in Lambda, or local system binary in development).
- `BROWSER_NAVIGATION_TIMEOUT_SECONDS`: Maximum seconds allowed for page navigation (default: 20).
- `BROWSER_MAX_ESCALATION_TASKS`: Maximum browser tasks permitted per trip execution (default: 3).

**Critical test scenarios**:
- Happy path: Evidence task outputs dynamic page request, orchestrator dispatches `BROWSER` task, browser worker captures hydrated DOM HTML, parses lodging entities, and records immutable observation, verifies **AC-1**, **AC-2**, **AC-3**, **AC-5**, **AC-6**.
- SSRF route blocking: Target page or dynamic redirect attempts navigation to cloud metadata address (`http://169.254.169.254`), request is aborted by route filter and flagged as unsafe, verifies **AC-4**.
- Navigation timeout and anti bot challenge: Target page hangs or triggers Cloudflare verification, worker catches timeout, records unavailable source entry with descriptive reason, completes task, and workflow continues, verifies **AC-7**.
- Packaging boundary verification: Clean environment subprocess check with `-S -P` verifies browser worker imports cleanly, meets package size limits, and raises `ModuleNotFoundError` for forbidden modules (`functions.optimizer`, `functions.api`, `functions.orchestrator.graphs`), verifies **AC-8**.

## Build plan

The build plan follows the Tracer Bullet approach, standing up a thin vertical thread through contracts, transport, worker claiming, browser rendering, and packaging verification:

1. **Slice 1: Browser Domain Contracts and Port Abstraction**
   - Create `backend/src/contracts/browser.py` defining `BrowserTaskPayload`, `BrowserRenderResult`, and `BrowserExtractionPort`.
   - Implement self contained SSRF URL validation in `backend/src/functions/browser/ssrf.py` without importing evidence collector code.
   - Implement `FixtureBrowserAdapter` in `backend/src/functions/browser/fixtures.py` simulating dynamic page rendering with canned HTML fixtures for offline tests.
   - Satisfies **AC-4**, **AC-5**.

2. **Slice 2: Orchestrator Transport Routing and LangGraph Escalation Node**
   - Update `SqsTransportAdapter` in `functions/orchestrator/execution/transport.py` to route `worker="BROWSER"` to `BROWSER_WORKER_QUEUE_URL`.
   - Implement `escalate_dynamic_evidence_node` in `functions/orchestrator/graphs/trip_planner/graph.py` to inspect completed evidence outputs, enforce the 3 task ceiling in emission order, and enqueue pending `BROWSER` tasks.
   - Satisfies **AC-1**, **AC-2**.

3. **Slice 3: Fenced Browser Worker Service and Observation Persistence**
   - Implement `BrowserWorkerService` in `backend/src/functions/browser/service.py` to claim tasks under `WorkerFencingService`, execute rendering via the driver port, parse rendered content with Scrapling, and persist `SourceObservationModel` records with `extraction_method="BROWSER_HTML"`.
   - Implement error handling for navigation timeouts and bot challenges, recording unavailable source records and completing tasks gracefully as success.
   - Satisfies **AC-3**, **AC-6**, **AC-7**.

4. **Slice 4: Playwright Driver Adapter and SSRF Route Interception**
   - Implement `PlaywrightBrowserAdapter` in `backend/src/functions/browser/driver.py` using Playwright async API with route filtering against private IPs and metadata endpoints.
   - Configure headless launch options for Lambda environment compatibility (disabling sandbox, dev shm usage, and gpu).
   - Satisfies **AC-4**, **AC-5**.

5. **Slice 5: SQS Consumer Handler and Standalone SAM Packaging**
   - Implement `lambda_handler`, `process_batch`, and `process_record` in `backend/src/functions/browser/consumer.py` with partial batch failure reporting.
   - Create `backend/src/functions/browser/template.yaml` declaring the browser worker Lambda function, SQS event trigger, DLQ, and Chromium layer binding.
   - Create `backend/src/functions/browser/packaging.py` and `tests/unit/test_browser_packaging.py` validating package sizes beneath 250 MB uncompressed and verifying strict isolation under `-S -P`.
   - Satisfies **AC-8**.

## Consequences

**Positive**:
- Dynamic web pages requiring JavaScript execution are extracted seamlessly without altering static scraping performance.
- Zero ECS or ECR infrastructure overhead; the entire system remains strictly serverless and Lambda first.
- Strict isolation prevents heavy browser dependencies from polluting the orchestrator or evidence collector packages.
- Provenance and auditability are preserved through write once immutable observation records in PostgreSQL.

**Negative / tradeoffs**:
- Browser worker Lambda functions require 1536 MB of memory and experience higher cold start latency than static scraping Lambdas.
- Complex single page applications with aggressive anti bot protection may still fail to render, requiring reliance on graceful degradation.

**Neutral**:
- Requires deployment of a precompiled Chromium Lambda Layer (such as Sparticuz Chromium) when running in live AWS environments.

## Follow-up

- [ ] Enroll feature 3 in `docs/scope/scope.md` linking this specification.
- [ ] Maintain precompiled Chromium Lambda Layer ARN configuration in SAM deployment parameters for live AWS verification.
