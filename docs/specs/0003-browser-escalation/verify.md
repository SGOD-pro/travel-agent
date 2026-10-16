# Verify: Browser Escalation Through the Orchestrator, spec 0003, updated 2026-10-16
_Steps derived from spec 0003 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Acceptance criteria

- [x] AC-1: Orchestrator LangGraph workflow evaluates execution budget and enqueues up to 3 BROWSER worker tasks in request emission order with replay safe slot limits.
- [x] AC-2: Orchestrator transport layer routes worker="BROWSER" task dispatches to BROWSER_WORKER_QUEUE_URL without importing browser code into orchestrator modules.
- [x] AC-3: Browser worker claims and executes tasks under WorkerFencingService lease fencing, validating parent execution status, context version, and monotonic attempt counters.
- [x] AC-4: Target URLs and dynamic subresources evaluated against SSRF policies using Playwright route interception, aborting private IP ranges, loopback, and cloud metadata services.
- [x] AC-5: Browser worker uses abstract BrowserExtractionPort, verified offline via FixtureBrowserAdapter with canned fixtures, and executed live via PlaywrightBrowserAdapter.
- [x] AC-6: Rendered content parsed using Scrapling and persisted as immutable SourceObservationModel in source_observations with extraction_method="BROWSER_HTML", referencing placeholder observation UUID.
- [x] AC-7: When navigation times out, crashes, or encounters bot challenges, worker records explicit unavailable source entry in worker_results and completes task under lease fencing as success.
- [x] AC-8: Browser worker packages independently into AWS Lambda artifact complying with 250 MB ceiling, verified under isolated Python 3.12 with -S -P.

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

## Specced surfaces (Slice 4)

- [x] Network security and SSRF protection module in `backend/src/functions/browser/network.py`:
  - Address filtering via `is_ip_allowed` blocking loopback, link local, RFC 1918 private subnets, carrier grade NAT, multicast, reserved addresses, and AWS metadata endpoints (`169.254.169.254`, `fd00:ec2::254`).
  - Pre navigation address screening via `validate_navigation_url` and `resolve_and_validate_host`.
  - In flight route interception via `RouteSecurityInterceptor` resolving destination IP addresses on every request to mitigate DNS rebinding.
  - Protocol controls disabling Service Workers (`service_workers="block"`) and eliminating WebSockets across protocol filters and context init scripts.
  - HTTP redirect destination validation in `check_response` monitoring 301, 302, 303, 307, and 308 status codes, closing pages immediately upon private or metadata redirect targets.
- [x] Concrete driver adapter `PlaywrightBrowserAdapter` in `backend/src/functions/browser/driver.py`:
  - Conforms to `BrowserExtractionPort`.
  - Headless Chromium launch flags configured for serverless runtime compatibility.
  - Captures hydrated DOM, extracts substantive text excerpt up to 1000 characters, calculates SHA256 content hash, parses domain entities via Scrapling, and bounds raw HTML snippets to 50000 characters.
  - Graceful degradation cleanly mapping timeouts to `TIMED_OUT`, security blocks and anti bot challenges to `BLOCKED`, and browser crashes to `UNAVAILABLE`.
- [x] Mocked unit test suite in `backend/tests/unit/test_browser_network_and_driver_mocked.py`: 43 tests verifying pre navigation checks, route interception, DNS rebinding simulation, thread offloaded DNS resolution, background task tracking, WebSocket blocking, timeout degradation, bot challenge degradation, and crash handling.
- [x] Live Chromium execution test suite in `backend/tests/unit/test_browser_driver_live.py`: 6 tests verifying actual headless Chromium JavaScript rendering, DOM hydration, timeout enforcement, HTTP 302 redirect aborts to metadata endpoints, Service Worker blocking, and WebSocket disabling.

## Specced surfaces (Slice 5)

- [x] Dedicated SQS consumer Lambda handler `backend/src/functions/browser/consumer.py`:
  - Implements `lambda_handler`, `process_batch`, and `process_record` with partial batch failure reporting (`ReportBatchItemFailures`).
  - Protects against Lambda execution timeouts by failing records early when remaining time drops below 5000ms, enabling clean SQS redrives.
  - Discards benign fencing conflicts (`TaskClaimConflictError`, `TaskLeaseActiveError`, `TaskAlreadyTerminalError`, `StaleQueueMessageError`) without DLQ failure.
  - Re-raises fatal execution errors for DLQ routing.
- [x] SAM serverless template `backend/src/functions/browser/template.yaml`:
  - Declares `BrowserEscalationFunction` provisioned with Python 3.12, 1536 MB memory, 60s timeout, and x86_64 architecture.
  - Declares `BrowserEscalationQueue` and `BrowserEscalationDLQ` with redrive policy (`maxReceiveCount: 5`).
  - Binds external precompiled Chromium Lambda layer parameter `ChromiumLayerArn`.
  - Configures SQS trigger with batch size 10 and `ReportBatchItemFailures`.
- [x] Isolated packaging and verification module `backend/src/functions/browser/packaging.py`:
  - Dependency caching with hash verification.
  - Clean environment verification under Python 3.12 using `-S -P` with `PYTHONNOUSERSITE=1` and virtualenv paths stripped.
  - Asserts function package uncompressed size <= 80 MB (measured ~69 MB).
  - Asserts function package compressed ZIP size <= 50 MB (measured ~21 MB).
  - Asserts combined function package plus 130 MB Chromium layer <= 250 MB ceiling (measured ~199 MB, satisfying 210 MB target).
  - Asserts strict module isolation with forbidden modules (`functions.optimizer`, `functions.api`, `functions.evidence_collector`, `functions.orchestrator.graphs`, `ortools`, `langgraph`) raising `ModuleNotFoundError`.
- [x] Unit test suites:
  - `backend/tests/unit/test_browser_consumer.py`: 9 tests verifying record routing, non browser message filtering, fencing conflict discarding, error redrives, deadline timeouts, partial batch failures, and synchronous Lambda handler execution.
  - `backend/tests/unit/test_browser_packaging.py`: 6 tests verifying SAM template validity and settings alignment, lean requirements dependencies, dependency inclusive size limits, clean environment import under `-S -P`, cached rebuild speed, and missing Python 3.12 runtime detection.

## Commands

- [x] `uv run ruff check src/contracts/browser.py src/functions/browser tests/unit/test_browser*` passes cleanly with zero lint warnings.
- [x] `uv run mypy src/functions/browser/ tests/unit/test_browser*` passes cleanly with zero type errors across all 16 source files.
- [x] `uv run pytest tests/unit/test_browser_consumer.py tests/unit/test_browser_packaging.py -v` passes all 15 Slice 5 tests.
- [x] `uv run pytest tests/unit/test_browser* -v` passes all 165 browser unit and live tests.
- [x] `uv run pytest tests/unit` passes all 314 backend unit tests with zero regressions.

## Acceptance criteria coverage

- AC-1 covered by `test_browser_escalation_slice2.py` (replay safe 3 task budget, emission ordering, state deduplication).
- AC-2 covered by `test_browser_escalation_slice2.py` (dedicated queue routing, env fallback, modular boundary enforcement).
- AC-3 covered by `test_browser_worker_slice3.py` (claim under fencing, rejection of cancelled/terminal/stale parent before browser execution).
- AC-4 covered by `test_browser_network_and_driver_mocked.py` and `test_browser_driver_live.py` (pre navigation address screening, in flight route interception, DNS rebinding simulation, live redirect blocking to 169.254.169.254, Service Worker blocking, and WebSocket elimination).
- AC-5 covered offline by `test_browser_contracts_and_fixtures.py` and live by `test_browser_network_and_driver_mocked.py` and `test_browser_driver_live.py` (PlaywrightBrowserAdapter live execution, DOM capture, and Scrapling entity parsing).
- AC-6 covered by `test_browser_worker_slice3.py` (Scrapling entity parsing, immutable `SourceObservationModel` persistence with `extraction_method="BROWSER_HTML"`, artifact reference linkage).
- AC-7 covered by `test_browser_worker_slice3.py` and `test_browser_network_and_driver_mocked.py` (graceful degradation on timeout/bot block/driver exception, unavailable sources recording, successful task completion under lease fencing).
- AC-8 covered by `test_browser_packaging.py` and `packaging.py` (unpacked function size 68.95 MB <= 80 MB, compressed ZIP 20.56 MB <= 50 MB, combined unpacked size 198.95 MB <= 210 MB target and 250 MB ceiling, and clean Python 3.12 isolation under `-S -P`).
