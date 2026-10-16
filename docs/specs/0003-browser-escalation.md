# 0003. Browser Escalation Through the Orchestrator

**Date**: 2026-10-15  
**Status**: Accepted

## Summary

This specification defines the browser escalation worker and orchestrator workflow integration for dynamic web pages that require JavaScript rendering. When static evidence collection detects a client side application or bot verification challenge, the orchestrator inspects the resulting requests and dispatches targeted browser rendering tasks under strict replay safe concurrency and budget limits. A dedicated browser worker executes these tasks using Playwright and an external Chromium layer within an independent AWS Lambda package, ensuring the primary evidence collector stays lean and zero container infrastructure is introduced. All rendered page captures are persisted as immutable source observations linked to original placeholder records, guaranteeing graceful degradation when pages time out or block automation.

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
- **AC-1**: When an evidence collection task completes with `browser_requests` records in its output, the orchestrator LangGraph workflow evaluates the remaining execution budget and enqueues up to 3 `BROWSER` worker tasks in request emission order for asynchronous dispatch with replay safe slot limits.
- **AC-2**: The orchestrator transport layer routes `worker="BROWSER"` task dispatches to the dedicated browser queue URL via `BROWSER_WORKER_QUEUE_URL` environment configuration, preserving modular boundaries without importing browser code into orchestrator modules.
- **AC-3**: The browser worker claims and executes tasks under `WorkerFencingService` lease fencing, validating parent execution status, context version, and monotonic attempt counters before starting browser automation.
- **AC-4**: Target URLs and all dynamic subresource requests are evaluated against SSRF security policies using live Playwright route level interception in the driver adapter, aborting navigation to private IP ranges, loopback addresses, and cloud metadata services (169.254.169.254).
- **AC-5**: The browser worker uses an abstract `BrowserExtractionPort`, verified offline via `FixtureBrowserAdapter` with canned HTML fixtures, and executed live via `PlaywrightBrowserAdapter` with bounded page timeouts and rendered DOM capture.
- **AC-6**: Rendered content is parsed into clean entity observations using Scrapling and persisted as an immutable `SourceObservationModel` in `source_observations` with `extraction_method="BROWSER_HTML"`, referencing the original `NEEDS_BROWSER` placeholder observation ID in `artifact_ref` without in place row mutations.
- **AC-7**: When browser navigation times out, crashes, or encounters bot verification challenges, the worker records an explicit unavailable source entry in `worker_results`, completes the task under lease fencing as a success outcome, and allows the parent workflow to continue without failing the overall execution.
- **AC-8**: The browser worker packages independently into an isolated AWS Lambda artifact where the combined unpacked size of the function package plus external Chromium layer strictly complies with the 250 MB AWS Lambda ceiling, verified under isolated Python 3.12 with `-S -P`.

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

### Concrete domain contracts

All browser data types are strictly typed Pydantic models in `backend/src/contracts/browser.py`:

```python
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.evidence import FareObservation, HotelObservation

_SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")


class BrowserRenderStatus(StrEnum):
    SUCCESS = "SUCCESS"
    UNAVAILABLE = "UNAVAILABLE"
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"


class BrowserWaitUntil(StrEnum):
    LOAD = "load"
    DOMCONTENTLOADED = "domcontentloaded"
    NETWORKIDLE = "networkidle"
    COMMIT = "commit"


class BrowserTaskPayload(BaseModel):
    """Input payload dispatched to the BROWSER worker."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(..., min_length=1, max_length=2048, description="Target web page URL to render")
    canonical_url: str = Field(..., min_length=1, max_length=2048, description="Normalized canonical URL")
    category: str = Field(..., min_length=1, max_length=64, description="Evidence category, e.g. lodging, transit, advisory")
    reason: str = Field(..., min_length=1, max_length=255, description="Dynamic detection reason, e.g. NOSCRIPT_JAVASCRIPT_REQUIRED")
    wait_until: BrowserWaitUntil = Field(default=BrowserWaitUntil.NETWORKIDLE, description="Navigation completion strategy")
    timeout_seconds: int = Field(default=20, ge=1, le=60, description="Maximum navigation timeout in seconds")
    selector_target: str | None = Field(default=None, max_length=255, description="Optional CSS selector required before DOM capture")
    placeholder_observation_id: UUID | None = Field(default=None, description="Original NEEDS_BROWSER observation UUID")


class BrowserResourceUsage(BaseModel):
    """Resource consumption metrics for browser page execution."""

    model_config = ConfigDict(frozen=True)

    duration_ms: int = Field(..., ge=0, description="Elapsed execution duration in milliseconds")
    memory_mb: int | None = Field(default=None, ge=0, description="Estimated memory consumption in megabytes")


class BrowserRenderResult(BaseModel):
    """Normalized output produced by the browser extraction port.

    Distinguished from persisted worker output: this model captures the immediate
    outcome of browser page rendering and extraction before database persistence.
    """

    model_config = ConfigDict(frozen=True)

    status: BrowserRenderStatus = Field(default=BrowserRenderStatus.SUCCESS)
    canonical_url: str = Field(..., min_length=1, max_length=2048)
    effective_url: str = Field(..., min_length=1, max_length=2048)
    content_hash: str | None = Field(default=None, description="SHA256 hex digest of clean rendered text")
    excerpt_text: str | None = Field(default=None, max_length=1000, description="Clean substantive text excerpt up to 1000 characters")
    excerpt_locator: str | None = Field(default=None, max_length=256, description="CSS selector or locator for extracted content")
    rendered_html_snippet: str | None = Field(default=None, max_length=50000, description="Bounded excerpt of raw rendered HTML")
    render_duration_ms: int = Field(..., ge=0)
    unavailable_reason: str | None = Field(default=None, max_length=500)
    usage: BrowserResourceUsage
    parsed_lodging_observations: list[HotelObservation] = Field(default_factory=list)
    parsed_transit_observations: list[FareObservation] = Field(default_factory=list)

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, v: str | None) -> str | None:
        if v is not None and not _SHA256_HEX_REGEX.match(v.lower()):
            raise ValueError("content_hash must be a valid 64 character lowercase hex SHA256 digest")
        return v.lower() if v is not None else None

    @model_validator(mode="after")
    def validate_status_contract(self) -> BrowserRenderResult:
        if self.status == BrowserRenderStatus.SUCCESS:
            if not self.content_hash:
                raise ValueError("SUCCESS status requires a valid SHA256 content_hash")
            if not self.excerpt_text or not self.excerpt_text.strip():
                raise ValueError("SUCCESS status requires substantive non empty excerpt_text")
            if not self.excerpt_locator or not self.excerpt_locator.strip():
                raise ValueError("SUCCESS status requires non empty excerpt_locator")
            if self.unavailable_reason is not None:
                raise ValueError("SUCCESS status must not have an unavailable_reason")
        else:
            if not self.unavailable_reason or not self.unavailable_reason.strip():
                raise ValueError(f"{self.status} status requires a descriptive non empty unavailable_reason")
            if self.content_hash is not None or self.excerpt_text is not None or self.excerpt_locator is not None:
                raise ValueError(
                    f"{self.status} status permits absent content and must never fabricate excerpts, hashes, or locators"
                )
        return self


class BrowserWorkerOutput(BaseModel):
    """Persisted worker output stored in WorkerResultModel.output_ref upon task completion.

    Distinguished from adapter results: this model captures the persisted database
    observation_id UUID generated for successful renders and normalized unavailable source records.
    """

    model_config = ConfigDict(frozen=True)

    status: BrowserRenderStatus = Field(default=BrowserRenderStatus.SUCCESS)
    canonical_url: str = Field(..., min_length=1, max_length=2048)
    effective_url: str = Field(..., min_length=1, max_length=2048)
    observation_id: UUID | None = Field(default=None, description="Persisted source observation UUID")
    content_hash: str | None = Field(default=None, description="SHA256 hex digest of clean rendered text")
    rendered_html_bytes: int | None = Field(default=None, ge=0)
    render_duration_ms: int = Field(..., ge=0)
    unavailable_sources: list[dict[str, Any]] = Field(default_factory=list)
    parsed_observations: list[dict[str, Any]] = Field(default_factory=list)
    usage: BrowserResourceUsage

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, v: str | None) -> str | None:
        if v is not None and not _SHA256_HEX_REGEX.match(v.lower()):
            raise ValueError("content_hash must be a valid 64 character lowercase hex SHA256 digest")
        return v.lower() if v is not None else None

    @model_validator(mode="after")
    def validate_worker_output(self) -> BrowserWorkerOutput:
        if self.status == BrowserRenderStatus.SUCCESS:
            if self.observation_id is None:
                raise ValueError("SUCCESS worker output requires a persisted observation_id UUID")
            if not self.content_hash:
                raise ValueError("SUCCESS worker output requires a valid SHA256 content_hash")
        else:
            if not self.unavailable_sources:
                raise ValueError(f"{self.status} worker output requires non empty unavailable_sources records")
            if self.observation_id is not None or self.content_hash is not None:
                raise ValueError(
                    f"{self.status} worker output permits absent content and must not fabricate observation_id or content_hash"
                )
        return self


class BrowserExtractionPort(ABC):
    """Abstract port for browser page rendering and dynamic entity extraction."""

    @abstractmethod
    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Renders dynamic page content and returns normalized render outcome."""
        ...
```

The persisted worker task output stored in `WorkerResultModel.output_ref` adheres to `BrowserWorkerOutput`. On successful renders, it captures the persisted `observation_id` UUID, rendered HTML byte size, and parsed entities:

```json
{
  "status": "SUCCESS",
  "canonical_url": "https://example.com/hotels/resort",
  "effective_url": "https://example.com/hotels/resort",
  "observation_id": "550e8400-e29b-41d4-a716-446655440000",
  "content_hash": "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0",
  "rendered_html_bytes": 48200,
  "render_duration_ms": 1420,
  "unavailable_sources": [],
  "parsed_observations": [
    {
      "property_name": "Grand Palace Resort",
      "price_per_night": "4500.00",
      "currency": "INR"
    }
  ],
  "usage": {
    "duration_ms": 1420,
    "memory_mb": 512
  }
}
```

On failed or unavailable renders (`UNAVAILABLE`, `TIMED_OUT`, `BLOCKED`), the worker task output records the failure within `unavailable_sources`, leaving `observation_id` and `content_hash` absent without fabricating synthetic identifiers:

```json
{
  "status": "TIMED_OUT",
  "canonical_url": "https://example.com/hotels/resort",
  "effective_url": "https://example.com/hotels/resort",
  "observation_id": null,
  "content_hash": null,
  "rendered_html_bytes": 0,
  "render_duration_ms": 20000,
  "unavailable_sources": [
    {
      "url": "https://example.com/hotels/resort",
      "reason": "Navigation timeout of 20 seconds exceeded"
    }
  ],
  "parsed_observations": [],
  "usage": {
    "duration_ms": 20000,
    "memory_mb": 512
  }
}
```

### Durable escalation integration and state transitions

Escalation integrates directly into the orchestrator execution engine across eight distinct transactional steps:

1. **Evidence Task Emits Dynamic Requests**: `EvidenceCollectorService` completes an evidence task where Scrapling detected a single page application or noscript tag. It stages a placeholder `SourceObservationModel` (`extraction_method="NEEDS_BROWSER"`) and commits `complete_task(..., output={"browser_requests": [...], "observations": [...]})`.
2. **Completion Reconciliation and Graph Continuation**: `CompletionReconciliationService` processes `TASK_COMPLETED`. It updates the LangGraph checkpoint with `task_outputs[str(task_id)] = output_payload`. When all initial evidence tasks reach completion, the reconciler transitions `executions.status = "RUNNING"` and emits `EXECUTION_RESUME`.
3. **Escalation Node Evaluates Replay Safe Slot Limits**: `escalate_dynamic_evidence_node` executes in `trip_planner/graph.py`. It inspects `state["task_outputs"]` for emitted `browser_requests`. It computes available escalation slots:
   `already_dispatched = set(state.get("dispatched_browser_urls", []))`
   `available_slots = max(0, 3 - len(already_dispatched))`
   If `available_slots > 0`, it takes up to `available_slots` unhandled requests in emission order and creates `pending_tasks` specifications:
   `{"worker": "BROWSER", "action": "render", "payload": BrowserTaskPayload(...).model_dump(mode="json"), "idempotency_key": f"{execution_id}:render:{canonical_url_hash[:16]}", "deadline_seconds": 60, "max_attempts": 3}`.
   State records `dispatched_browser_urls = list(already_dispatched | new_urls)`.
4. **Outbox Dispatch Creation**: `GraphRunnerService._apply_graph_outcome` sees `pending_tasks`. In a single atomic database transaction, it inserts `TaskModel(worker="BROWSER", action="render", ...)` rows with unique idempotency keys, inserts `OutboxEventModel(kind="TASK_DISPATCH", ...)` records, and transitions `executions.status = "WAITING_TASKS"`.
5. **Dedicated SQS Transport Routing**: `OutboxRelayService` relays outbox events. `SqsTransportAdapter` routes messages with `worker="BROWSER"` directly to `BROWSER_WORKER_QUEUE_URL` using configured environment variables without reverse importing browser code.
6. **Browser Worker Claims Task Under Fencing**: `BrowserWorkerConsumer` receives the SQS message and calls `BrowserWorkerService.execute_render_task`. The service invokes `WorkerFencingService.claim_task(worker="BROWSER", action="render")` under parent first row locking, acquiring an exclusive `lease_token`.
7. **Render Execution and Atomic Observation Persistence**: The worker executes navigation via `BrowserExtractionPort`. On render success, Scrapling extracts clean text and domain entities. In a single savepoint transaction, the worker inserts a new `SourceObservationModel` (`extraction_method="BROWSER_HTML"`), links `placeholder_observation_id` inside `artifact_ref`, and calls `WorkerFencingService.complete_task`, committing observations and emitting `TASK_COMPLETED` outbox event atomically.
8. **Reconciliation and Workflow Completion**: `CompletionReconciliationService` applies the browser task output into `state["task_outputs"]`. When all browser tasks finish, the reconciler emits `EXECUTION_RESUME`, allowing the planning graph to resume and compute itineraries with full rendered evidence.

### Persistence ownership

To preserve the strict modular boundary convention ("Do not import worker implementations across different function folders"), persistence ownership is defined as follows:

1. **Shared PostgreSQL Storage Table**: `source_observations` in PostgreSQL is the single canonical source of truth for all observations, both static and dynamic.
2. **Self Contained Browser Model Definition**: Rather than importing `functions.evidence_collector.models` across function directories, `backend/src/functions/browser/models.py` defines `SourceObservationModel` mapped to the existing `source_observations` table using the shared `Base` from `config.base_model`. This ensures the browser package remains fully self contained and isolated during standalone packaging.
3. **Atomic Worker Commits**: `BrowserWorkerService` writes observations directly in its active database session alongside `WorkerFencingService.complete_task`. If any error occurs before completion, the transaction rolls back cleanly, leaving zero orphaned observation records.

### Replay safe limits

To prevent task multiplication during workflow resumes, crash recovery sweeps, or SQS redeliveries, the 3 task escalation ceiling is enforced with dual layer protection:

1. **State Level Deduplication**: LangGraph checkpoint state maintains `dispatched_browser_urls: list[str]`. When `escalate_dynamic_evidence_node` executes, it only considers URLs not already present in this list. If the workflow resumes or replays, existing URLs are skipped immediately.
2. **Database Level Idempotency**: Each browser task receives a deterministic idempotency key:
   `idempotency_key = f"{execution_id}:render:{hashlib.sha256(canonical_url.encode()).hexdigest()[:16]}"`
   When `GraphRunnerService` inserts tasks, it checks `existing_keys = select(TaskModel.idempotency_key).where(TaskModel.execution_id == execution_id)`. Any duplicate task specification generated during re evaluation is discarded, guaranteeing that no more than 3 browser tasks can ever exist for an execution.

### Browser network controls

Offline tests and unit fixtures do not make real network requests and therefore cannot prove network security against live SSRF vulnerabilities. Live browser network security is enforced in the driver adapter layer during live page execution:

1. **Pre Navigation Address Validation**: Before launching page navigation, `PlaywrightBrowserAdapter` parses the target URL scheme and resolves the destination hostname via system DNS. It rejects non HTTP(S) schemes and aborts immediately if the IP address falls within private, loopback, or link local subnets (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, and IPv6 equivalents).
2. **In Flight Playwright Route Interception**: Attackers can bypass initial URL validation using HTTP 301/302 redirects, dynamic JavaScript `fetch` calls, iframes, or DNS rebinding. To eliminate this vulnerability, `PlaywrightBrowserAdapter` registers a route interception handler on the browser context:
   `await context.route("**/*", handle_route)`
   The route handler inspects the target URL of EVERY outgoing subresource request. It resolves the destination IP address and immediately calls `route.abort("blockedbyclient")` if the target points to AWS metadata services (`169.254.169.254`), internal container endpoints, or private networks.
3. **Bounded Page Lifecycle**: Navigation enforces strict timeouts: `page.goto(url, timeout=timeout_ms, wait_until=wait_until)`. If navigation exceeds 20 seconds, the browser halts navigation cleanly and records a timeout outcome.

### Combined function plus layer packaging acceptance

In AWS Lambda, the 250 MB uncompressed limit applies to the combined sum of the function code package PLUS all uncompressed Lambda Layers. Packaging acceptance is budgeted and verified as follows:

1. **Function Package Budget**:
   - Compressed ZIP size: strictly beneath 50 MB (target: beneath 35 MB).
   - Uncompressed directory size: strictly beneath 80 MB.
2. **Chromium Layer Budget**:
   - Uncompressed Chromium layer (Sparticuz Chromium in `/opt`): approximately 130 MB.
3. **Combined Packaging Acceptance Gate**:
   - `Total Unpacked Size = size(Function Unpacked) + size(Chromium Layer Unpacked) <= 250 MB (262,144,000 bytes)`.
   - Budget allocation: 80 MB function + 130 MB layer = 210 MB, providing 40 MB safety margin below the AWS Lambda ceiling.
4. **Packaging Isolation Verification**:
   - Subprocess execution using `-S -P` with `PYTHONNOUSERSITE=1` and `VIRTUAL_ENV` stripped.
   - Asserts `lambda_handler`, `process_batch`, and `process_record` import cleanly.
   - Asserts zero site packages directories on `sys.path`.
   - Asserts forbidden modules (`ortools`, `functions.optimizer`, `functions.api`, `functions.orchestrator.graphs`, `functions.evidence_collector`) raise `ModuleNotFoundError`.
   - Fails explicitly if Python 3.12 is missing.

### Key invariants

- **Strict Budget Ceiling**: A single trip execution cannot spawn more than 3 browser escalation tasks total across its entire lifecycle.
- **Strict Evidence Scope Boundary**: Browser escalation is strictly limited to dynamic page rendering and evidence observation extraction. Booking, payment, checkout, or transaction automation are explicitly out of scope and forbidden.
- **Write Once Immutability**: Existing `NEEDS_BROWSER` placeholder observations in PostgreSQL are never mutated in place or deleted. Rendered observations are inserted as new rows with `extraction_method="BROWSER_HTML"`.
- **Live Route Level Network Security**: All live network requests initiated by headless Chromium are intercepted and filtered against private IP ranges and cloud metadata endpoints.
- **Fail Safe Graceful Degradation**: Page render timeouts, navigation aborts, or bot challenge blocks record an explicit unavailable source record and complete the task successfully under lease fencing without failing the parent execution.
- **Independent Packaging**: The browser worker artifact contains only browser consumer code and permitted shared contracts. It never imports optimizer, API, or orchestrator graph code, and evidence collector never imports browser code.

### Security model

- **Network Boundaries**: Pre navigation DNS resolution checks combined with Playwright route level interception aborting private IP and metadata requests.
- **Fenced Execution**: Task claims require valid parent execution status (`RUNNING` or `WAITING_TASKS`), matching context versions, and unexpired lease tokens.
- **Zero Secrets in Code**: Environment configuration manages queue URLs and browser binary paths.

### Configuration required

- `BROWSER_WORKER_QUEUE_URL`: Amazon SQS queue URL for browser task dispatch.
- `CHROMIUM_PATH`: Path to Chromium executable (defaults to `/opt/bin/chromium` in Lambda, or local system binary in development).
- `BROWSER_NAVIGATION_TIMEOUT_SECONDS`: Maximum seconds allowed for page navigation (default: 20).
- `BROWSER_MAX_ESCALATION_TASKS`: Maximum browser tasks permitted per trip execution (default: 3).

### Critical test scenarios

- Happy path: Evidence task outputs dynamic page request, orchestrator dispatches `BROWSER` task, browser worker captures hydrated DOM HTML, parses lodging entities, and records immutable observation, verifies **AC-1**, **AC-2**, **AC-3**, **AC-5**, **AC-6**.
- Live route level SSRF blocking: Target page or dynamic redirect attempts subresource request to cloud metadata address (`http://169.254.169.254`), request is aborted by Playwright route filter and flagged as unsafe, verifies **AC-4**.
- Replay safe limit enforcement: Re executing the escalation node on a replayed checkpoint does not spawn duplicate tasks and strictly honors the 3 task maximum, verifies **AC-1**.
- Navigation timeout and anti bot challenge: Target page hangs or triggers bot verification, worker catches timeout, records unavailable source entry with descriptive reason, completes task successfully, and workflow continues, verifies **AC-7**.
- Combined packaging verification: Clean environment subprocess check with `-S -P` verifies browser worker imports cleanly, combined function plus layer size stays beneath 250 MB uncompressed, and raises `ModuleNotFoundError` for forbidden modules, verifies **AC-8**.

## Build plan

The build plan follows the Tracer Bullet approach, standing up a thin vertical thread through contracts, transport, worker claiming, browser rendering, and packaging verification:

1. **Slice 1: Browser Domain Contracts and Offline Fixture Adapter**
   - [x] Create `backend/src/contracts/browser.py` defining `BrowserRenderStatus`, `BrowserTaskPayload`, `BrowserResourceUsage`, `BrowserRenderResult`, and abstract `BrowserExtractionPort`.
   - [x] Implement `FixtureBrowserAdapter` in `backend/src/functions/browser/fixtures.py` simulating dynamic page rendering with canned HTML fixtures for offline tests.
   - [x] Add unit tests for contract serialization, immutability, and fixture adapter execution without live network calls.
   - Satisfies **AC-5**.

2. **Slice 2: Orchestrator Transport Routing and LangGraph Escalation Node**
   - [x] Update `SqsTransportAdapter` in `functions/orchestrator/execution/transport.py` to route `worker="BROWSER"` to `BROWSER_WORKER_QUEUE_URL`.
   - [x] Implement `escalate_dynamic_evidence_node` in `functions/orchestrator/graphs/trip_planner/graph.py` to inspect completed evidence outputs, enforce the 3 task ceiling in emission order, track `dispatched_browser_urls`, and enqueue pending `BROWSER` tasks with deterministic idempotency keys.
   - Satisfies **AC-1**, **AC-2**.

3. **Slice 3: Fenced Browser Worker Service, Observation Persistence, and Graceful Degradation**
   - [x] Implement `SourceObservationModel` mapping in `backend/src/functions/browser/models.py` referencing `source_observations` table without cross worker imports.
   - [x] Implement `BrowserWorkerService` in `backend/src/functions/browser/service.py` to claim tasks under `WorkerFencingService`, execute rendering via the driver port, parse rendered content with Scrapling, and persist `SourceObservationModel` records with `extraction_method="BROWSER_HTML"`.
   - [x] Implement error handling for navigation timeouts and bot challenges, recording unavailable source records and completing tasks gracefully as success.
   - Satisfies **AC-3**, **AC-6**, **AC-7**.

4. **Slice 4: Playwright Driver Adapter and Live Browser Network Controls**
   - [x] Implement `PlaywrightBrowserAdapter` in `backend/src/functions/browser/driver.py` using Playwright async API with pre navigation DNS validation and route level interception aborting private IP and metadata requests.
   - [x] Configure headless launch options for Lambda environment compatibility (disabling sandbox, dev shm usage, and gpu).
   - [x] Add integration tests verifying live route interception and redirect aborts against cloud metadata endpoints.
   - Satisfies **AC-4**, **AC-5**.

5. **Slice 5: SQS Consumer Handler, SAM Template, and Combined Packaging Acceptance**
   - [x] Implement `lambda_handler`, `process_batch`, and `process_record` in `backend/src/functions/browser/consumer.py` with partial batch failure reporting.
   - [x] Create `backend/src/functions/browser/template.yaml` declaring the browser worker Lambda function, SQS event trigger, DLQ, and Chromium layer binding.
   - [x] Create `backend/src/functions/browser/packaging.py` and `tests/unit/test_browser_packaging.py` verifying function package size, combined function plus layer size under 250 MB uncompressed, and strict isolation under `-S -P`.
   - Satisfies **AC-8**.

## Consequences

**Positive**:
- Dynamic web pages requiring JavaScript execution are extracted seamlessly without altering static scraping performance.
- Zero ECS or ECR infrastructure overhead; the entire system remains strictly serverless and Lambda first.
- Strict isolation prevents heavy browser dependencies from polluting the orchestrator or evidence collector packages.
- Provenance and auditability are preserved through write once immutable observation records in PostgreSQL.
- Replay safe limits guarantee predictable cost and execution bounds during workflow retries and crash recovery.

**Negative / tradeoffs**:
- Browser worker Lambda functions require 1536 MB of memory and experience higher cold start latency than static scraping Lambdas.
- Complex single page applications with aggressive anti bot protection may still fail to render, requiring reliance on graceful degradation.

**Neutral**:
- Requires deployment of a precompiled Chromium Lambda Layer (such as Sparticuz Chromium) when running in live AWS environments.

## Follow-up

- [ ] Maintain precompiled Chromium Lambda Layer ARN configuration in SAM deployment parameters for live AWS verification.
- [ ] Measure live CloudWatch memory and latency metrics during initial cloud deployment to fine tune memory allocations.
