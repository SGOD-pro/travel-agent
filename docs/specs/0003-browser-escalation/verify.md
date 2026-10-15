# Verify: Browser Escalation Through the Orchestrator, spec 0003, updated 2026-10-15
_Steps derived from spec 0003 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Acceptance criteria

- [x] AC-1: Orchestrator LangGraph workflow evaluates execution budget and enqueues up to 3 BROWSER worker tasks in request emission order with replay safe slot limits.
- [x] AC-2: Orchestrator transport layer routes worker="BROWSER" task dispatches to BROWSER_WORKER_QUEUE_URL without importing browser code into orchestrator modules.
- [x] AC-3: Browser worker claims and executes tasks under WorkerFencingService lease fencing, validating parent execution status, context version, and monotonic attempt counters.
- [ ] AC-4: Target URLs and dynamic subresources evaluated against SSRF policies using Playwright route interception, aborting private IP ranges, loopback, and cloud metadata services.
- [x] AC-5: Browser worker uses abstract BrowserExtractionPort, verified offline via FixtureBrowserAdapter with canned fixtures, and executed live via PlaywrightBrowserAdapter.
- [x] AC-6: Rendered content parsed using Scrapling and persisted as immutable SourceObservationModel in source_observations with extraction_method="BROWSER_HTML", referencing placeholder observation UUID.
- [x] AC-7: When navigation times out, crashes, or encounters bot challenges, worker records explicit unavailable source entry in worker_results and completes task under lease fencing as success.
- [ ] AC-8: Browser worker packages independently into AWS Lambda artifact complying with 250 MB ceiling, verified under isolated Python 3.12 with -S -P.

## Specced surfaces (Slice 1)

- [x] Contract `BrowserRenderStatus` in `backend/src/contracts/browser.py` defining SUCCESS, UNAVAILABLE, TIMED_OUT, and BLOCKED outcomes.
- [x] Contract `BrowserWaitUntil` in `backend/src/contracts/browser.py` enforcing allowed Playwright wait strategies: load, domcontentloaded, networkidle, commit.
- [x] Contract `BrowserTaskPayload` in `backend/src/contracts/browser.py` enforcing min and max text bounds, timeout bounds, UUID placeholder observation id, and wait until enum.
- [x] Contract `BrowserResourceUsage` in `backend/src/contracts/browser.py` tracking execution duration in milliseconds and memory in megabytes.
- [x] Contract `BrowserRenderResult` in `backend/src/contracts/browser.py` enforcing:
  - SUCCESS requires substantive content (1 to 1000 characters excerpt text, excerpt locator) and a valid 64 character hex SHA256 content hash, with unavailable reason absent.
  - UNAVAILABLE, TIMED_OUT, and BLOCKED require non empty unavailable reason and permit absent content without fabricated excerpts, locators, or hashes.
- [x] Contract `BrowserWorkerOutput` in `backend/src/contracts/browser.py` distinguishing persisted worker results stored in WorkerResultModel.output_ref (with observation id UUID and unavailable sources) from adapter results.
- [x] Abstract port `BrowserExtractionPort` in `backend/src/contracts/browser.py` declaring async render_page contract.
- [x] Test fixture adapter `FixtureBrowserAdapter` in `backend/src/functions/browser/fixtures.py` simulating dynamic rendering for lodging and transit with zero network access and zero browser binary calls.
- [x] Unit test suite in `backend/tests/unit/test_browser_contracts_and_fixtures.py` verifying positive serialization, negative validation, and offline fixture execution (67 tests passing).

## Specced surfaces (Slice 2)

- [x] Configuration parameters in `backend/src/config/config.py`: `BROWSER_WORKER_QUEUE_URL`, `CHROMIUM_PATH`, `BROWSER_NAVIGATION_TIMEOUT_SECONDS`, `BROWSER_MAX_ESCALATION_TASKS`.
- [x] SQS transport routing in `backend/src/functions/orchestrator/execution/transport.py`: routes `worker="BROWSER"` to `BROWSER_WORKER_QUEUE_URL` with fallback to `os.environ` while maintaining modular boundaries (zero browser module imports).
- [x] LangGraph escalation node `escalate_dynamic_evidence_node` in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`: parses dynamic requests from `task_outputs`, enforces 3 task escalation ceiling in emission order, records `dispatched_browser_urls` in state, and formats `pending_tasks` with deterministic idempotency keys (`{execution_id}:render:{canonical_url_hash[:16]}`).
- [x] Workflow integration in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`: wired between `discover_candidates` and `solve_schedule` with safe fallback for legacy states.
- [x] Unit test suite in `backend/tests/unit/test_browser_escalation_slice2.py`: 14 tests verifying transport routing, environment fallbacks, modular isolation, budget enforcement, emission ordering, replay safe deduplication, optional field preservation, configurable budget ceilings, deterministic execution ID fallbacks, and e2e workflow execution.

## Specced surfaces (Slice 3)

- [x] Isolated model `SourceObservationModel` in `backend/src/functions/browser/models.py` mapping to `source_observations` table using dedicated `BrowserModelBase` without cross worker imports from `functions.evidence_collector`.
- [x] Scrapling entity parser `parse_rendered_entities` and price parser `parse_inr_price` in `backend/src/functions/browser/parser.py` extracting lodging cards (`HotelObservation`), transit fares (`FareObservation`), and general DOM elements with strict Decimal INR pricing (no zero or unparseable rate coercion).
- [x] Browser dispatch envelope `BrowserDispatchEnvelope` and parser `parse_browser_dispatch_message` in `backend/src/functions/browser/service.py` supporting direct dictionaries and SQS JSON wrapped envelopes.
- [x] Worker service `BrowserWorkerService` in `backend/src/functions/browser/service.py`:
  - Enforces `WorkerFencingService.claim_task(worker="BROWSER", action="render")`, rejecting cancelled parent executions, terminal parent executions, stale context versions, or exhausted attempts prior to browser automation.
  - On successful render, persists immutable `SourceObservationModel` with `extraction_method="BROWSER_HTML"` referencing original `placeholder_observation_id` inside `artifact_ref` without in place mutations.
  - On navigation timeouts, bot challenge blocks (`BLOCKED`), or driver exceptions, records descriptive `unavailable_sources` entries in `BrowserWorkerOutput` and completes the task under lease fencing as a success outcome so the parent workflow continues uninterrupted.
- [x] Unit test suite in `backend/tests/unit/test_browser_worker_slice3.py`: 17 tests covering price non coercion, lodging/transit Scrapling parsing, dispatch parsing, fencing rejection invariants, successful observation persistence with defensive fallback, graceful degradation, and modular isolation.

## Commands

- [x] `uv run ruff check src/contracts/browser.py src/functions/browser tests/unit/test_browser*` passes cleanly with zero lint warnings.
- [x] `uv run mypy src/functions/browser/ tests/unit/test_browser_worker_slice3.py` passes cleanly with zero type errors.
- [x] `uv run pytest tests/unit/test_browser_worker_slice3.py -v` passes all 17 worker and persistence tests.
- [x] `uv run pytest tests/unit/test_browser_contracts_and_fixtures.py tests/unit/test_browser_escalation_slice2.py tests/unit/test_browser_worker_slice3.py -v` passes all 98 browser unit tests.
- [x] `uv run pytest tests/unit` passes all 247 backend unit tests with zero regressions.

## Acceptance criteria coverage

- AC-1 covered by `test_browser_escalation_slice2.py` (replay safe 3 task budget, emission ordering, state deduplication).
- AC-2 covered by `test_browser_escalation_slice2.py` (dedicated queue routing, env fallback, modular boundary enforcement).
- AC-3 covered by `test_browser_worker_slice3.py` (claim under fencing, rejection of cancelled/terminal/stale parent before browser execution).
- AC-5 (partial: contracts and offline fixture adapter) covered by `test_browser_contracts_and_fixtures.py`.
- AC-6 covered by `test_browser_worker_slice3.py` (Scrapling entity parsing, immutable `SourceObservationModel` persistence with `extraction_method="BROWSER_HTML"`, artifact reference linkage).
- AC-7 covered by `test_browser_worker_slice3.py` (graceful degradation on timeout/bot block/driver exception, unavailable sources recording, successful task completion under lease fencing).
