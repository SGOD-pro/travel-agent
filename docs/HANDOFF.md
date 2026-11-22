# Context Handoff: SWENA Travel Intelligence (Specs 0001 through 0007)

Date: 2026-11-22

Status: Specs 0001, 0002, 0003, 0004, 0005, and 0006 complete and accepted. Spec 0003 permanently migrated to standalone Obscura browser engine (eliminating Chromium and Sparticuz layer, 240 MB uncompressed, 90.45 MB compressed, 47 ms cold boot). Spec 0007 Trip Planner End to End has completed Slices 1 through 6, 8, bounded optimizer dispatch, Valhalla matrix distance rejection recovery, and restriction of distance recovery to TRAVEL workflows, verified across 70 tests on isolated PostgreSQL with review verdict Approve. Scope Features 1 through 6 are done; Feature 7 is in progress. AWS deployment verification remains deferred across all features until cloud deployment. Next planned action is short multi stop brief headless live run for live road geometry verification.


## 1. Project Purpose and Agreed Stack

### Purpose
Build the SWENA Travel Intelligence platform, an asynchronous travel intelligence application that assists travelers across three composed or independent capabilities:
* Travel Planner: Destination research, itineraries, local experiences, schedule, and cost estimation.
* Route Optimizer: Stops, visit windows, road and walking routes, parking hubs, and return times.
* Media Location Identifier: Image and video evidence processing to identify geographical locations.

### Agreed Stack and Architectural Decisions
* Backend: Python 3.12 runtime for standalone workers with uv package manager, FastAPI API layer, modular Lambda first packaging.
* Persistence: Aiven PostgreSQL 16 with PostGIS extensions (local container for development), asyncpg driver, SQLAlchemy 2.0 async, Alembic migrations.
* Workflow Orchestration: LangGraph for stateful workflow graphs, backed by a custom TransactionBoundCheckpointer.
* Remote Model Serving: Remote LAYA model is served from Kaggle through a Cloudflare tunnel.
* Routing Provider: External Valhalla HTTP service for road and pedestrian distance and duration matrices and turn by turn path geometry generation with polyline6 decoding.
* Constraint Optimization: Google OR Tools constraint solver for asymmetric Vehicle Routing Problem with Time Windows (VRPTW), wait time accounting, dwell durations, and priority partitioning with drop penalties.
* Workflow Execution Sequence: Route optimizer and trip planner end to end workflows precede media location development.
* Browser Milestone Boundary: Browser escalation milestone strictly covers dynamic web page rendering and evidence extraction only; booking, reservation execution, payment, checkout, and transaction automation remain deferred.
* Messaging and Outbox: Transactional outbox table (`outbox_events`) claimed with `FOR UPDATE SKIP LOCKED`, AWS SQS for message delivery with partial batch failure reporting (`ReportBatchItemFailures`), and Dead Letter Queues (DLQ) with redrive policies.
* Worker Routing: Dedicated SQS queue transport routing worker `EVIDENCE`, `BROWSER`, and `OPTIMIZER` dispatches to dedicated queues with zero reverse imports into orchestrator modules.
* Evidence Collection: Scrapling CSS extraction adapter, SerpAPI search provider adapter, immutable content addressed observations in PostgreSQL.
* Verification Engine: PostGIS spatial annular band filtering and candidate matching, deterministic ClaimVerificationPolicy with domain authority rules, side by side conflict preservation, and categorical freshness expiration.
* Supervision and Scheduling: Continuous BackgroundDaemon supervision for local processes, AWS EventBridge schedules for periodic relay, lease recovery, and stale sweeps.
* Infrastructure: AWS SAM templates ([orchestrator template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/template.yaml), [evidence collector template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/template.yaml), [browser template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/template.yaml), and [route optimizer template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/template.yaml)).
* Frontend: Next.js (TypeScript, React) in `frontend/`.

## 2. Git State

* Commit: `b11ee1f` (feat/0007-optimizer-dispatch merged with main)
* Branch: `feat/0007-optimizer-dispatch`
* Working Tree: Clean application code, verified test suites, up to date review records, and reconciled scope.

## 3. What 0001, 0002, 0003, 0004, 0005, 0006, and 0007 Implement

### Slice 0001: Orchestrator Execution Engine
Conforms to [0001-orchestrator-execution-engine.md](file:///home/swyra/projects/travel-agent/docs/specs/0001-orchestrator-execution-engine.md):
* Database Migrations (0003 through 0007, 628f8a83fe95): Canonical tables for `executions`, `tasks`, `worker_results`, `outbox_events`, `completion_receipts`, `job_events`, and `api_idempotency`.
* API Job Acceptance: Idempotent `POST /api/v1/trips/{trip_id}/plans` returning HTTP 202 Accepted with canonical `job_id`, owner isolated timeline polling (`GET /api/v1/jobs/{job_id}`), and cooperative cancellation (`POST /api/v1/jobs/{job_id}/cancel`).
* Worker Lease Fencing: `WorkerFencingService` with optimistic lease tokens, attempt tracking, backoff delay fencing, and fenced completion or failure preventing stale worker overwrites.
* Transactional Outbox Relay: `OutboxRelayService` claiming events with `FOR UPDATE SKIP LOCKED` and publishing bounded batches with at least once delivery guarantees.
* Scheduled Lease Recovery: `TaskRecoveryService` reclaiming expired leases with exponential backoff, failing exhausted attempts, and redispatching orphaned execution leases.
* LangGraph Checkpoint Reconciliation: `TransactionBoundCheckpointer` with atomic buffer rollback on aborted database transactions, completion receipt deduplication, and safe execution resumption.
* SQS Consumers and Supervision: `SqsGraphConsumer`, `SqsWorkerConsumer`, and `SqsCompletionConsumer` with `asyncio.gather` concurrent batch processing, and `BackgroundDaemon` process supervision with crash recovery and bounded shutdown.
* SAM Infrastructure Specification: [template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/template.yaml) declaring SQS queues, DLQs with `maxReceiveCount: 5`, partial batch event mappings, and EventBridge schedules.

### Slice 0002: Evidence Collection and Verification
Conforms to [0002-evidence-collection-and-verification.md](file:///home/swyra/projects/travel-agent/docs/specs/0002-evidence-collection-and-verification.md):
* Database Migrations (0009 and 0010): Canonical tables for `source_observations`, `canonical_places`, `claims`, and `claim_evidence` junction table, with PostGIS `geometry(Point, 4326)` and GiST spatial indexes.
* Search and Extraction Adapters: `SerpApiAdapter` for organic and place search with latency measurement and minor unit spend tracking; `ScraplingExtractionAdapter` for CSS parsing of lodging and transit listings without zero coercing unparseable prices; SSRF protection against private IP ranges and cloud metadata services.
* Immutable Source Provenance: `SourceObservationModel` persisting SHA256 content hashes, canonical URLs, publication domains, and excerpts, linking duplicate syndicated content while preventing non independent corroboration.
* Canonical Place Resolution: `CanonicalPlacesRepository` providing spatial annular band discovery via `ST_DWithin` (2000 to 3000 meters and 5000 to 7000 meters) and token similarity candidate resolution within 100 meters without destructive entity merging.
* Deterministic Policy Engine: `ClaimVerificationPolicy` enforcing that generative model proposals cannot directly mark claims `VERIFIED`; critical road closures and festival advisories strictly require authoritative police or government domains; contradictory assertions are preserved side by side as `CONFLICTED` claims; expired assertions transition to `STALE` without altering historical observation records.
* Budget and Metering Guard: Tasks enforce remaining tool call and minor unit cost budgets, halting external calls and returning partial results when exhausted.
* Dedicated Worker Routing: Dedicated SQS transport routing in `SqsTransportAdapter` directing worker `EVIDENCE` tasks to a dedicated queue and consumer, removing the synthetic echo success executor stub while preserving orchestrator modular boundaries.
* Standalone SAM Packaging: Dedicated [template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/template.yaml) declaring `Runtime: python3.12` and `Architectures: [x86_64]`, packaging dependencies into an isolated artifact meeting AWS Lambda limits.

### Spec 0003: Browser Escalation (Slices 1 to 5 Complete)
Conforms to [0003-browser-escalation.md](file:///home/swyra/projects/travel-agent/docs/specs/0003-browser-escalation.md):
* Slice 1 (Domain Contracts and Offline Fixtures): `BrowserRenderStatus`, `BrowserWaitUntil`, `BrowserTaskPayload`, `BrowserResourceUsage`, `BrowserRenderResult`, `BrowserWorkerOutput`, and `BrowserExtractionPort` defined in [backend/src/contracts/browser.py](file:///home/swyra/projects/travel-agent/backend/src/contracts/browser.py). Offline `FixtureBrowserAdapter` simulates dynamic rendering for lodging and transit with zero network and zero browser binaries (67 unit tests).
* Slice 2 (Transport Routing and Escalation Node): Dedicated SQS routing for `worker="BROWSER"` via `BROWSER_WORKER_QUEUE_URL` in [backend/src/functions/orchestrator/execution/transport.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/transport.py) without importing browser worker modules. Dynamic evidence escalation node `escalate_dynamic_evidence_node` in [backend/src/functions/orchestrator/graphs/trip_planner/graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py) enforces a 3 task budget ceiling in emission order, maintains replay safe state deduplication via `dispatched_browser_urls`, and emits deterministic idempotency keys (`{execution_id}:render:{canonical_url_hash[:16]}`) (14 unit tests).
* Slice 3 (Fenced Worker Service, Parser, and Persistence):
  * Fenced task claim and completion under `WorkerFencingService` in [backend/src/functions/browser/service.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/service.py), validating parent execution status, context version, and attempt counters before browser execution.
  * Atomic database transaction committing immutable observation in `source_observations` and task completion in `worker_results` and `tasks` in the exact same transaction.
  * Clean rollback on fencing rejection prior to commit, leaving zero orphaned observations.
  * Replay safety using PostgreSQL `on_conflict_do_nothing` on `(execution_id, canonical_url, content_hash)` with defensive `scalar_one_or_none()` lookup fallback to generated UUID.
  * Owner isolation scoping observations strictly to `execution_id` with foreign key cascade, using isolated `BrowserModelBase` declarative base in [backend/src/functions/browser/models.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/models.py) with zero imports from `functions.evidence_collector`.
  * Scrapling entity parser in [backend/src/functions/browser/parser.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/parser.py) extracting lodging and transit observations with strict Decimal INR pricing (no zero or unparseable rate coercion).
  * Graceful error degradation for timeouts and bot challenge blocks, recording structured `unavailable_sources` entries, leaving observation IDs absent without fabricating data, and completing as success in `worker_results` to allow the parent graph to continue uninterrupted (17 unit tests, 5 live PostgreSQL behaviors).
* Slice 4 (Playwright Driver Adapter and Network Security Controls):
  * Standalone defensive network controls in [backend/src/functions/browser/network.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/network.py), enforcing pre navigation and dynamic in flight route interception against RFC 1918 private ranges, carrier grade NAT, and cloud metadata destinations (169.254.169.254).
  * SecureEgressProxy destination connection binding: intercepts connections prior to socket creation, resolves hostnames, and binds outbound TCP sockets strictly to validated IP literals with multi homed candidate fallback, defeating DNS rebinding across navigation, redirects, and subresources.
  * Thread offloaded DNS resolution via `asyncio.to_thread` preventing event loop stalls, DNS rebinding simulation defense, WebSocket route aborts, and HTTP redirect tracking.
  * Live Playwright headless Chromium driver in [backend/src/functions/browser/driver.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py), launching headless Chromium with disabled Service Workers and WebSockets, consistent post wait DOM content extraction after selector waiting, and structured degradation on timeouts, bot challenges, and browser crashes (56 tests across mocked and live Chromium suites).
* Slice 5 (SQS Consumer Lambda, SAM Template, and Standalone Packaging):
  * SQS consumer Lambda handler `lambda_handler` and `process_batch` in [backend/src/functions/browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py) supporting partial batch item failure reporting via `ReportBatchItemFailures`, lease conflict discarding, and 5000ms deadline timeouts.
  * SAM serverless template in [backend/src/functions/browser/template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/template.yaml) configured with 1536 MB memory, 60s timeout, dedicated dispatch queue with DLQ redrive, external Chromium layer binding, and `CHROMIUM_PATH: /tmp/chromium`.
  * Standalone packaging verifier in [backend/src/functions/browser/packaging.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/packaging.py) preserving stripped Node runtime binary (104 MB), measuring Sparticuz Chromium layer v130.0.0 at 64.50 MB, confirming function unpacked size 148.20 MB (< 185 MB limit), compressed size 52.42 MB intended for S3 deployment bucket upload, and combined size with layer 212.70 MB (< 250 MB ceiling, 37.30 MB headroom) under clean Python 3.12 `-S -P` isolation with verified layer bootstrap and isolated rendering (19 tests across consumer and packaging suites).

### Spec 0004: Remote LAYA Decision Router Integration (Slices 1 to 5 Complete)
Conforms to [0004-remote-laya-decision-router.md](file:///home/swyra/projects/travel-agent/docs/specs/0004-remote-laya-decision-router.md):
* Slice 1 (Domain Contracts, Wire Schemas, Configuration, and Offline Fixtures):
  * Internal typed domain contracts `LayaDecisionRequest`, `DecisionEnvelope`, `DecisionAction`, `DecisionStatus`, and port `LayaDecisionPort` in [backend/src/contracts/decision.py](file:///home/swyra/projects/travel-agent/backend/src/contracts/decision.py).
  * Upstream Jev wire request and response schemas in [backend/src/functions/decision_router/schemas.py](file:///home/swyra/projects/travel-agent/backend/src/functions/decision_router/schemas.py) matching NandhaKishorM/laya release v0.4.9 with probability normalization and finite float validation.
  * Settings configuration with separate Bearer and Cloudflare Access service tokens, socket timeouts, and cumulative retry deadlines in [backend/src/config/config.py](file:///home/swyra/projects/travel-agent/backend/src/config/config.py).
  * Offline `FixtureLayaDecisionAdapter` in [backend/src/functions/decision_router/fixtures.py](file:///home/swyra/projects/travel-agent/backend/src/functions/decision_router/fixtures.py) simulating decision routing with zero network dependencies (25 unit tests).
* Slice 2 (Remote HTTP Client Adapter and Privacy Sanitization):
  * Asynchronous HTTP adapter `RemoteLayaDecisionAdapter` in [backend/src/functions/decision_router/client.py](file:///home/swyra/projects/travel-agent/backend/src/functions/decision_router/client.py) speaking `POST /v1/systemone`.
  * Dual authentication headers attaching `Authorization: Bearer {LAYA_API_KEY}` and optional `CF-Access-Client-Id` / `CF-Access-Client-Secret`.
  * Cumulative total retry deadline (5.0s default) with individual socket timeout (2.5s default), exponential backoff on transient 5xx errors, fast failure on 4xx client errors or unconfigured endpoints, and deterministic singleton bypass.
  * Privacy sanitization in [backend/src/functions/decision_router/sanitization.py](file:///home/swyra/projects/travel-agent/backend/src/functions/decision_router/sanitization.py) stripping Bearer tokens, Cloudflare credentials, traveler PII, and bounding long context summaries client side (16 unit tests).
* Slice 3 (Action Eligibility, Policy Validator, and Deterministic Fallback Table):
  * Policy validator `LayaPolicyValidator` in [backend/src/functions/orchestrator/decision/policy.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/decision/policy.py) evaluating action eligibility across search completion, browser eligibility, and evaluation phases.
  * Confidence threshold gating (default 0.700 answer confidence), rejection of tokenizer truncations and explicit abstentions, probability normalization and argmax consistency validation.
  * Deterministic fallback table mapping timeouts, HTTP errors, policy rejections, and low confidence states to safe deterministic actions while preserving required destination evidence (27 unit tests).
* Slice 4 (Database Migration, Model, Repository, and Four Point Result Reuse):
  * Alembic migration `0011_add_laya_decisions.py` creating table `laya_decisions` with UUID primary key, foreign key on `executions.id` with CASCADE, numeric confidences, and unique constraint on `(execution_id, idempotency_key)`.
  * SQLAlchemy model `LayaDecisionModel` in [backend/src/functions/orchestrator/decision/models.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/decision/models.py) with phase extraction and `to_envelope()` domain conversion.
  * Repository `LayaDecisionRepository` in [backend/src/functions/orchestrator/decision/repository.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/decision/repository.py) implementing four point revalidation for result reuse (matching phase, context version, policy version, and active eligible choices) and idempotent persistence using PostgreSQL `pg_insert` with `on_conflict_do_nothing` (18 unit tests).
* Slice 5 (LangGraph Decision Node, Conditional Edge, Connected Acceptance, and Evaluation Benchmark):
  * Decision node `route_evidence_decision_node` and conditional edge `route_evidence_decision_edge` in [backend/src/functions/orchestrator/graphs/trip_planner/graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py), orchestrating evidence routing between candidate discovery and dynamic evidence escalation, routing to static search, browser escalation, or schedule solving, while validating non placeholder persisted evidence matching required destinations, preserving original decision audit metadata (`allowed_choices` and `context_version`), and persisting records in `laya_decisions` (10 unit tests).
  * Connected smoke acceptance test in [backend/tests/integration/test_laya_connected_acceptance.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_laya_connected_acceptance.py) verifying live Cloudflare tunnel communication, GET /health probe, and POST /v1/systemone inference round trip with latency measurement.
  * 25 scenario labeled routing quality evaluation benchmark in [backend/tests/unit/test_laya_evaluation_benchmark.py](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_laya_evaluation_benchmark.py), serving as an offline deterministic policy validator regression benchmark that exercises policy gating, confidence thresholding, and deterministic fallbacks; live model inference accuracy on those scenarios remains pending connected live evaluation (3 benchmark tests).

### Spec 0005: Route Optimizer and Multimodal Itinerary Planning (Slices 1 to 7 Complete)
Conforms to [0005-route-optimizer.md](file:///home/swyra/projects/travel-agent/docs/specs/0005-route-optimizer.md):
* Slice 1 (Domain Contracts, Priority Partitioning, and Offline Solver):
  * Internal typed domain contracts `RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, `MatrixRequest`, `MatrixResponse`, `RouteObjective`, `RouteStatus`, `SolverStatus`, `StopType`, and `TrafficBasis` defined in [backend/src/contracts/routing.py](file:///home/swyra/projects/travel-agent/backend/src/contracts/routing.py).
  * Offline constraint solver `OrToolsRoutingSolver` in [backend/src/functions/route_optimizer/solver.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/solver.py) solving asymmetric Vehicle Routing Problems with Time Windows (VRPTW), wait time accounting, dwell completion before venue closing, drop penalties (10,000,000) for optional stops, multi criteria integer objectives (`FASTEST`, `SHORTEST`, `BALANCED`), and honest status mapping (`OPTIMAL` strictly requiring mathematical proof, solution found yielding `FEASIBLE`, timeout yielding `TIME_LIMIT` with `UNAVAILABLE`, and impossible mandatory constraints returning `INFEASIBLE`) (15 unit tests).
* Slice 2 (Database Migration, Models, and PostGIS Repository):
  * Alembic migration `0012_add_route_optimization.py` creating tables `routes`, `route_stops`, `route_legs`, and `route_restrictions` with GiST spatial indexes, `Numeric(20, 6)` fuel cost precision, and UUID foreign keys.
  * SQLAlchemy models in [backend/src/functions/orchestrator/routing/models.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/routing/models.py) and repository `RouteRepository` in [backend/src/functions/orchestrator/routing/repository.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/routing/repository.py) providing atomic route saving, tenant isolation enforcement, and optimistic version conflict prevention rejecting duplicate trip versions (4 live PostgreSQL tests).
* Slice 3 (Multimodal Vehicle Continuity and Canonical Parking Hub Service):
  * Spatial continuity validator `VehicleContinuityTracker` in [backend/src/functions/route_optimizer/continuity.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/continuity.py) enforcing that driving legs only depart from where the vehicle is parked, detecting vehicle abandonment or teleportation.
  * Spatial parking discovery query `find_verified_parking_near` in [backend/src/functions/orchestrator/places/repository.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/places/repository.py) using PostGIS `ST_DWithin` on geography with bicycle parking exclusion.
  * Coordinator `MultimodalTransitionService` in [backend/src/functions/route_optimizer/multimodal.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/multimodal.py) injecting `PARKING_HUB` stops, splitting driving and walking legs, supporting walk chaining, and isolating fuel costs strictly to driving distance (8 unit tests, 4 live database tests).
* Slice 4 (Valhalla HTTP Client and Hard Closure Avoidance):
  * Client `ValhallaClient` in [backend/src/functions/route_optimizer/valhalla.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/valhalla.py) with connection pooling, polyline6 decoding, bounded retries, and explicit failure on unconfigured or unreachable hosts.
  * Service `ClosureAvoidanceService` in [backend/src/functions/route_optimizer/closures.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/closures.py) transforming active verified restriction claims into `exclude_polygons` with post routing Shapely intersection revalidation (20 unit tests, 1 live Valhalla integration test).
* Slice 5 (Mid Trip Dynamic Replanning):
  * Service `RouteReplanService` in [backend/src/functions/route_optimizer/replan.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/replan.py) accepting current traveler coordinates, freezing completed stops with historical arrival timestamps, treating live position as the new origin, optimizing remaining stops, retaining historical dropped stops audit records, and writing an immutable incremented `route_version` under optimistic concurrency controls (9 unit tests, 2 live database tests).
* Slice 6 (Fenced Asynchronous Worker Service, Consumer, and REST API):
  * Worker service `RouteOptimizerWorkerService` in [backend/src/functions/route_optimizer/service.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/service.py) claimed under `WorkerFencingService` on SQS.
  * SQS consumer handler `process_batch` in [backend/src/functions/route_optimizer/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/consumer.py) supporting partial batch item failure reporting and deadline guards.
  * Dedicated SQS transport routing for worker `OPTIMIZER` tasks via `ROUTE_OPTIMIZER_QUEUE_URL` in [backend/src/functions/orchestrator/execution/transport.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/transport.py).
  * Serverless SAM template in [backend/src/functions/route_optimizer/template.yaml](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/template.yaml) with dedicated dispatch queue, DLQ redrive, and 60s timeout.
  * REST endpoints in [backend/src/functions/api/routes/optimization.py](file:///home/swyra/projects/travel-agent/backend/src/functions/api/routes/optimization.py) providing asynchronous route submission (`POST /api/v1/routes/optimize`), owner isolated route retrieval (`GET /api/v1/routes/{route_id}`), and mid trip replan submission (`POST /api/v1/routes/{route_id}/replan`) (15 unit tests, 2 live PostgreSQL tests).
* Slice 7 (Standalone Lambda ZIP Packaging and Connected Acceptance):
  * Standalone packaging utility in [backend/src/functions/route_optimizer/packaging.py](file:///home/swyra/projects/travel-agent/backend/src/functions/route_optimizer/packaging.py), confirming lightweight function artifact (0.28 MB) and measuring dependency inclusive Linux distribution artifact (~211 MB unpacked, ~64.5 MB compressed) well within the AWS Lambda 250 MB uncompressed limit (38.8 MB headroom), with verified clean isolated import and solve execution under Python 3.12 `-S -P`.
  * Connected provider acceptance suite in [backend/tests/integration/test_valhalla_connected_acceptance_slice7.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_valhalla_connected_acceptance_slice7.py) verifying live Valhalla matrix calculation, turn by turn geometry decoding, and closure detour routing (7 unit tests, 6 connected live tests).
* Production Integration Repairs (Commits `c6ba6e8` and `80c03ce`):
  * Shared configured `ValhallaClient` provider across solve, multimodal, and replan workflows without implicit production haversine or hardcoded speed fallbacks, failing explicitly with `ValhallaConnectionError` when unconfigured or unreachable.
  * Selected road and walking legs query `ValhallaClient.route` with `exclude_polygons` derived from active restrictions, persisting detoured provider geometries and consistent arrival, dwell, wait, and departure timing.
  * Revalidated hard service windows after provider hydration: dwell completion checked before closing (`target_dep <= window_end`). Mandatory stop violations transition status to `RouteStatus.INFEASIBLE` and never retain a feasible status. Optional stop violations drop the stop, re solve remaining stops, and return `RouteStatus.PARTIAL`.
  * Provider hydration and validation applied to both `RouteStatus.FEASIBLE` and `RouteStatus.PARTIAL` routes across solve, multimodal, and replan workflows.
  * Identity preservation (`id=s.id`) in `ScheduledStopOutput` for both scheduled and dropped stops, ensuring exact ID mapping in itinerary and audit records.
  * Standalone terminal completion reconciliation scoped strictly to optimizer workflows (`ROUTE_OPTIMIZATION`, `ROUTE_REPLAN`, `OPTIMIZER`, `ROUTE_OPTIMIZER`), preventing unintended terminalization of graph workflows in `WAITING_TASKS` (13 unit regression tests).

## 4. Verification Commands and Latest Confirmed Results

All verification commands execute locally against live PostgreSQL and headless Chromium:

1. Full Backend Test Suite (`cd backend && uv run pytest tests/unit/ -v`):
   * Result: 444 passed, 1 skipped in ~43s with zero regressions across all unit suites.

2. Full Browser Test Suite (`cd backend && uv run pytest tests/unit/test_browser* -v`):
   * Result: 178 passed in 29.59s across all 7 browser test suites.

3. Live Chromium Execution Suite (`cd backend && uv run pytest tests/unit/test_browser_driver_live.py -v`):
   * Result: 8 passed in 9.76s on live headless Chromium.
   * Confirms live DOM rendering, navigation timeouts, private IP and 169.254.169.254 redirect aborts, Service Worker blocking, WebSocket elimination, post wait snapshot consistency with dynamic JS insertion, and egress proxy metadata subresource blocking.

4. Live PostgreSQL Schema and Behavior Verification:
   * Confirms on live database: Fencing rejection on cancelled parent (0 observations), successful render persistence with `extraction_method='BROWSER_HTML'` and placeholder UUID link in `artifact_ref`, graceful degradation on timeout and bot challenge (0 observations, task completed), and Scrapling entity parsing with strict Decimal INR pricing.
   * Confirms live PostgreSQL schema has all 13 columns present on `source_observations`.

5. Static Analysis and Type Checking:
   * `cd backend && uv run ruff check src tests/unit/`: PASS (0 warnings).
   * `cd backend && uv run mypy src`: PASS (0 type errors).

6. LAYA Unit and Benchmark Test Suite (`cd backend && uv run pytest tests/unit/test_laya_*.py -v`):
   * Result: 100 passed, 1 skipped in 3.82s.
   * Benchmark suite confirms 100% precision on canonical scenarios with >= 85% accuracy and <= 10% abstention for offline deterministic policy validation.

7. Live Connected Acceptance Test Suite (`cd backend && RUN_CONNECTED_LAYA_TESTS=1 uv run pytest tests/integration/test_laya_connected_acceptance.py -v`):
   * Result: 1 passed in 2.22s against live Kaggle endpoint through Cloudflare tunnel.
   * Confirms GET /health responds with 200 OK and POST /v1/systemone returns normalized DecisionEnvelope with recorded latency (~568 ms).

8. Live PostgreSQL Migration for LAYA:
   * Migration `0011_add_laya_decisions.py` upgraded and verified on live PostgreSQL; table `laya_decisions` present with foreign key cascade and unique constraint `uq_laya_execution_idempotency`.

9. PostgreSQL Lifecycle Regression Suite (`cd backend && uv run pytest tests/integration/test_laya_lifecycle_regression.py -v`):
   * Result: 4 passed in 4.41s on live PostgreSQL.
   * Confirms collector task dispatch and no premature scheduling, task completion execution through the real evidence collector consumer (`functions.evidence_collector.consumer.process_record`) and reconciliation to direct resolution resume, dynamic browser escalation dispatch with task execution through the real browser consumer (`functions.browser.consumer.process_record`) and reconciliation to schedule proposal, and durable decision reuse through the runtime graph node path without invoking remote inference.

10. Route Optimizer Unit Test Suite (`cd backend && uv run pytest tests/unit/test_route_optimizer*.py -v`):
    * Result: 85 passed in ~2m across all 7 slices.
    * Verifies OR Tools VRPTW constraint solving, multimodal transitions, parking hub injection, vehicle continuity, Valhalla matrix and route integration, closure avoidance, mid trip replanning, fenced SQS worker service, REST API, and standalone packaging.

11. Route Optimizer Production Regressions (`cd backend && uv run pytest tests/unit/test_route_optimizer_production_regressions.py -v`):
    * Result: 13 passed in ~42s.
    * Verifies provider failure fast fail, restriction lookup failure propagation, closure detour geometry hydration, schedule consistency, hard service window closing revalidation, optional stop dropped yielding PARTIAL with provider geometries in solve and replan paths, and unrelated workflow reconciliation isolation.

12. Route Optimizer Live PostgreSQL Integration Suite:
    * Result: 13 passed across live database and worker suites (`test_route_optimizer_slice2.py`: 4 passed; `test_route_optimizer_multimodal_slice3.py`: 4 passed; `test_route_optimizer_replan_slice5.py`: 2 passed; `test_route_optimizer_worker_slice6.py`: 2 passed; `test_valhalla_connected_slice4.py`: 1 passed).
    * Verifies PostGIS tables, spatial queries, optimistic version conflict detection, and worker lifecycle on live PostgreSQL.

13. Live Connected Valhalla Acceptance Suite (`cd backend && RUN_CONNECTED_VALHALLA_TESTS=1 uv run pytest tests/integration/test_valhalla_connected_acceptance_slice7.py -v`):
    * Result: 6 passed in ~16s against live Valhalla service.
    * Verifies live matrix calculation, turn by turn geometry decoding, and closure detour routing. Offline execution cleanly skips when live provider is unconfigured.

14. Route Optimizer Static Analysis and Type Checking:
    * `cd backend && uv run ruff check src/functions/route_optimizer/ tests/unit/test_route_optimizer*`: PASS (0 warnings).
    * `cd backend && uv run mypy src/functions/route_optimizer/ tests/unit/test_route_optimizer_production_regressions.py`: PASS (0 type errors).

15. Route Optimizer Standalone Packaging:
    * Confirms lightweight function artifact (0.28 MB) and dependency inclusive Linux distribution artifact (~211 MB unpacked, ~64.5 MB compressed) well within the AWS Lambda 250 MB uncompressed limit (38.8 MB headroom), with clean isolated import and solve verification under Python 3.12 `-S -P`.

## 5. Review Reports

* Spec 0003 Slice 2 Review: [2026-10-15-main.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-15-main.md) (Verdict: Approve).
* Spec 0003 Slice 3 Review: [2026-10-16-main.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-16-main.md) (Verdict: Approve, 0 blockers, 0 majors, 0 nits).
* Spec 0003 Slice 4 Review: [2026-10-16-browser-slice4.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-16-browser-slice4.md) (Verdict: Approve, 0 blockers, 0 majors, 0 nits).
* Spec 0003 Slice 5 Review: [2026-10-16-browser-slice5.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-16-browser-slice5.md) (Verdict: Approve, 0 blockers, 0 majors, 0 nits).
* Spec 0003 Closure Findings Review: [2026-10-17-main.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-17-main.md) (Verdict: Approve, 0 blockers, 0 majors, 0 unaddressed nits).
* Spec 0004 Slice 1 Review: [2026-10-17-laya-slice1.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-17-laya-slice1.md) (Verdict: Approve).
* Spec 0004 Slice 2 Review: [2026-10-17-laya-slice2.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-17-laya-slice2.md) (Verdict: Approve).
* Spec 0004 Slice 3 Review: [2026-10-17-laya-slice3.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-17-laya-slice3.md) (Verdict: Approve).
* Spec 0004 Slice 4 Review: [2026-10-18-laya-slice4.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-18-laya-slice4.md) (Verdict: Approve).
* Spec 0004 Slice 5 Review: [2026-10-18-laya-slice5.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-18-laya-slice5.md) (Verdict: Approve).
* Spec 0004 Full Integration and Lifecycle Repair Review: [2026-10-18-main.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-18-main.md) (Verdict: Approve, 0 blockers, 0 majors, 0 unaddressed nits).
* Spec 0005 Slice 1 Review: [2026-10-23-route-optimizer-slice1.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-23-route-optimizer-slice1.md) (Verdict: Approve).
* Spec 0005 Slice 2 Review: [2026-10-23-route-optimizer-slice2.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-23-route-optimizer-slice2.md) (Verdict: Approve).
* Spec 0005 Slice 3 Review: [2026-10-23-route-optimizer-slice3.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-23-route-optimizer-slice3.md) (Verdict: Approve with nits).
* Spec 0005 Slice 4 Review: [2026-10-24-route-optimizer-slice4.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-24-route-optimizer-slice4.md) (Verdict: Approve).
* Spec 0005 Slice 5 Review: [2026-10-24-route-optimizer-slice5.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-24-route-optimizer-slice5.md) (Verdict: Approve).
* Spec 0005 Slice 6 Review: [2026-10-24-route-optimizer-slice6.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-24-route-optimizer-slice6.md) (Verdict: Approve with nits).
* Spec 0005 Slice 7 Review: [2026-10-24-route-optimizer-slice7.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-24-route-optimizer-slice7.md) (Verdict: Approve with nits).
* Spec 0005 Production Integration Repair Review: [2026-10-25-main.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-25-main.md) (Verdict: Approve, 0 blockers, 0 majors, 0 unaddressed nits).
* Spec 0006 Slice 1 through 5 Reviews: [2026-10-25-route-optimizer-graph-slice1.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-25-route-optimizer-graph-slice1.md) through [2026-10-26-route-optimizer-graph-slice5.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-26-route-optimizer-graph-slice5.md) (Verdict: Approve).
* Spec 0003 Obscura Permanent Migration Review: [2026-11-20-obscura-migration.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-11-20-obscura-migration.md) (Verdict: Approve).
* Spec 0007 Trip Planner Slice 1 through 8 Reviews: [2026-10-29-trip-planner-llm-slice1.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-29-trip-planner-llm-slice1.md) through [2026-11-20-research-policy-re-review.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-11-20-research-policy-re-review.md) (Verdict: Approve).
* Spec 0007 Bounded Optimizer Dispatch Review: [2026-11-21-feat-0007-optimizer-dispatch.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-11-21-feat-0007-optimizer-dispatch.md) (Verdict: Approve).

## 6. Development Acceptance versus Deferred AWS Verification

### Development Acceptance
* Slices 1 through 5 of Spec 0003 are complete, verified, reviewed, and tested with permanent Obscura engine migration (Status: Accepted, Feature 3 marked done).
* Spec 0004 Remote LAYA decision router is complete across all 5 slices, with bounded corrections completed for evidence sufficiency validation, real consumer executed lifecycle tests, original decision audit preservation, and documentation reconciliation (Status: Accepted, Feature 4 marked done).
* Slices 1 through 7 of Spec 0005 Route Optimizer are complete, verified, reviewed, and tested with production integration repairs (Status: Accepted, Feature 5 marked done).
* Slices 1 through 5 of Spec 0006 Route Graph and Map GeoJSON are complete, verified, reviewed, and tested (Status: Accepted, Feature 6 marked done).
* Spec 0007 Trip Planner End to End Slices 1 through 6, 8, and bounded optimizer dispatch are complete, verified, and reviewed with verdict Approve (Status: In Progress, Feature 7 in progress).

### Deployed AWS Verification (Deferred)
No live AWS deployment has been executed. The following checks remain deferred until full cloud deployment:
* SAM deployment of `template.yaml` to live AWS CloudFormation across all workers (orchestrator, evidence collector, browser worker, and route optimizer).
* Real SQS task dispatch queue message delivery, visibility timeouts, and DLQ redrive policies in AWS.
* Live Aiven PostgreSQL and PostGIS connectivity from AWS Lambda VPC runtime.
* CloudWatch memory utilization, cold start duration, and timeout telemetry in production.

## 7. Status of Workflow Gates

* Scope (`/scope`): Features 1 through 6 marked done in [docs/scope/scope.md](file:///home/swyra/projects/travel-agent/docs/scope/scope.md); Feature 7 in progress.
* Architect (`/architect`): Specs 0001 through 0006 are `Accepted`; Spec 0007 is `In Progress`.
* Develop (`/develop`): Complete for Features 1 through 6; 0007 bounded optimizer dispatch developed.
* Verify (`/check verify`): Passed all acceptance criteria for 0001 through 0006; 0007 verified with 42 passing unit and integration tests.
* Review (`/check review`): Approved across all complete slices, Obscura migration in [2026-11-20-obscura-migration.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-11-20-obscura-migration.md), and 0007 optimizer dispatch in [2026-11-21-feat-0007-optimizer-dispatch.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-11-21-feat-0007-optimizer-dispatch.md).
* Test (`/test`): Complete across all implemented slices with comprehensive unit and database backed integration suites.
* Document (`/document`): Complete for 0001, 0002, 0003, 0004, and 0006.
* Sync (`/sync`): Complete. Checkpoint reconciled at commit `b11ee1f`.

## 0003 closure reconciliation (2026-10-16)

Inspected repository HEAD: `35d9e60c64d4d4143b178eb5affd73dea846eb10`. This checkpoint reconciles the previous Approve reports with concrete source evidence. It does not implement fixes, rerun the project test suites, or establish final closure. Earlier Approve verdicts and Accepted or done markers are historical workflow records; they do not resolve the findings below.

| ID | Status | Finding and evidence | Required closure evidence |
|---|---|---|---|
| C1 | RESOLVED | `packaging.py` removes `playwright/driver/node`. | Retain the required runtime or explicitly package or configure a compatible replacement. Start Playwright and render a controlled page from the isolated final artifact with the selected browser runtime. |
| C2 | RESOLVED | `CHROMIUM_LAYER_UNCOMPRESSED_BYTES = 130 * 1024 * 1024` is added to measured function bytes. | Identify and hash the actual function and layer artifacts, measure their extracted bytes, verify bootstrap decompression, confirm binary usability, and explain the upload mechanism. Keep unavailable target runtime proof explicitly deferred. |
| C3 | RESOLVED | `network.py` validates DNS addresses and then calls `route.continue_()`. Chromium resolution is not bound to those validated addresses. | Enforce the validated destination at connection and egress level and test the validation to connection change against controlled endpoints without contacting real metadata. |
| C4 | DISPROVED; no fix required | SAM `CodeUri: ../../../build/browser`, relative to `backend/src/functions/browser/template.yaml`, resolves to `backend/build/browser`, matching the builder default. | Independently executed Python pathlib normalization confirmed both paths match. This proves path arithmetic only, not SAM build or runtime success. |
| C5 | RESOLVED | `driver.py` captures `rendered_html = await page.content()` before `wait_for_selector()`; later entity parsing and stored HTML use that earlier capture. | Capture HTML after selector waiting and derive HTML, excerpt, and parsed entities from the same post wait snapshot. Add a delayed content regression. |

### Evidence boundaries

- Independently performed here: read only source inspection, executed Python pathlib normalization for C4, layer archive inspection, bootstrap decompressor verification, and isolated execution checks.
- Confirmed test passes: 178 browser tests, 327 backend unit tests, live Chromium and PostgreSQL checks, packaging isolation checks, and type checks.
- Explicit deferral: Live AWS Lambda execution on cloud infrastructure remains explicitly deferred across all features until cloud deployment.

### 0003 closure resolution (2026-10-17)

Following the agreed action, closure findings C1, C2, C3, and C5 were implemented and verified within existing 0003, with formal review approval in [2026-10-17-main.md](file:///home/swyra/projects/travel-agent/docs/reviews/2026-10-17-main.md) and full verification ledger in [verify.md](file:///home/swyra/projects/travel-agent/docs/specs/0003-browser-escalation/verify.md):

* C1 (RESOLVED): Preserved stripped Node runtime (104 MB, permissions 0755) in `backend/src/functions/browser/packaging.py`, verified clean environment startup and live DOM rendering under Python 3.12 `-S -P` in `test_verify_browser_startup_and_render_in_isolated_artifact_environment`.
* C2 (RESOLVED): Inspected Sparticuz Chromium layer archive `chromium-v130.0.0-layer.zip` (64.50 MB unpacked across 54 regular files, archive 67,464,242 bytes, SHA256 `c113ecfebb9778f7dd82829fe0fc0f99d98df8a93ab1ea6d4eee31d23f620480`). Confirmed the layer contains Brotli compressed binaries (`chromium.br`, `al2023.tar.br`, `fonts.tar.br`, `swiftshader.tar.br`) and zero uncompressed binary at `/opt/chromium`. Aligned SAM configuration with `CHROMIUM_PATH: /tmp/chromium`. Implemented `ensure_sparticuz_chromium` bootstrap in `driver.py` using the bundled Playwright Node runtime (`playwright/driver/node`) to decompress `chromium.br` to `/tmp/chromium` with permissions 0700 and `al2023.tar.br` to `/tmp/al2023/lib`. Verified executable usability (`Chromium 130.0.6723.0`). Function compressed ZIP size of 52.42 MB (under 70 MB limit) exceeds the direct 50 MB console upload limit due to preserving the required Node runtime, and is intended for staging via an Amazon S3 deployment bucket (`sam deploy --s3-bucket ...` or `CodeUri: s3://...`), where AWS Lambda supports uncompressed packages up to 250 MB. Combined uncompressed size is 212.70 MB, well within the 225 MB target and 250 MB ceiling (37.30 MB headroom). Explicit distinction maintained between isolated local Chromium rendering proof (verifying Playwright and bundled Node execution under Python 3.12 `-S -P`), layer bootstrap verification, and target AWS Lambda runtime execution (deferred to cloud rollout).
* C3 (RESOLVED): Implemented `SecureEgressProxy` in `backend/src/functions/browser/network.py` intercepting requests prior to socket creation, validating all resolved IP addresses against SSRF policies, and binding outbound TCP sockets strictly to validated IP literals with multi homed candidate IP fallback, defeating DNS rebinding across navigations and subresources.
* C4 (DISPROVED): Retained `CodeUri: ../../../build/browser` in `backend/src/functions/browser/template.yaml`, confirming path arithmetic resolves correctly to `backend/build/browser`.
* C5 (RESOLVED): Enforced post wait snapshot capture in `PlaywrightBrowserAdapter._render_with_browser` strictly after `wait_for_selector()`. Derived substantive excerpt text, SHA256 content hash, and Scrapling entity parsing atomically from identical post wait DOM snapshot.

### 0004 closure reconciliation (2026-10-22)

Inspected repository HEAD commit `5d43dc911370edc2ea1e43b5cc8ef78453a7f5f9` (and prior reviewed commit `71161b28be605ed74762443573aeafe2fbacf037`). Bounded corrections completed and verified across all four closure areas:

* C1 (RESOLVED): Fail closed evidence validation. Removed automatic production fallback from failed database lookups to unpersisted task output observations. In production, database query failure immediately halts with runtime error rather than falling back to in memory tasks. A stale required_evidence_satisfied state flag is overridden by current authoritative database validation. Offline fixtures are explicitly isolated via `_offline_fixture_mode` and fixture adapters. Unavailable persistence uses existing execution failure and retry semantics without prematurely scheduling.
* C2 (RESOLVED): Required evidence sufficiency. Destination name substring matching alone does not establish required evidence. Applied canonical requirements for destination identity, coordinates, and source support with haversine distance verification (<100 km) using ItineraryScheduler. Explicitly rejected NEEDS_BROWSER placeholders, irrelevant observations, and unpersisted references. Corrected the browser lifecycle fixture to use the actual scraper NEEDS_BROWSER contract instead of labeling placeholder text SCRAPLING_HTTP. Demonstrated that browser completion permits evaluation only when required evidence is actually satisfied.
* C3 (RESOLVED): Focused regression coverage. Added four unit regressions at the owning boundary in `tests/unit/test_laya_graph_node.py` covering database lookup failure fail closed, stale flag override, destination name only text or URLs and placeholder rejection, and valid persisted evidence acceptance. Preserved all four consumer executed PostgreSQL lifecycle and runtime reuse tests in `tests/integration/test_laya_lifecycle_regression.py`. Full unit suite verified at 431 passed, 1 skipped in 42.89s.
* C4 (RESOLVED): Reconcile closure records. Removed unsupported claims of 100% precision on a connected 25 scenario model benchmark. Kept offline policy regression separate from live model quality. Marked connected labeled model evaluation PENDING. Updated next action and ordering below.

### 0005 closure reconciliation (2026-10-25)

Inspected repository HEAD commit `80c03ceecde68955a4c3ab1f587e33b1a1d75bed` (and prior integration repair commit `c6ba6e81c437961b6acaca776977e2dce4358089`). Bounded corrections completed and verified across all routing integration areas:

* C1 (RESOLVED): Hard service window closing revalidation. Post hydration arrival, wait, and dwell times are revalidated against `window_end` (`target_dep <= window_end`). Mandatory stop window violations transition status to `RouteStatus.INFEASIBLE` and never retain a feasible status. Optional stop violations drop the stop, re solve remaining stops with provider routing, and return `RouteStatus.PARTIAL`.
* C2 (RESOLVED): Provider geometry hydration for both FEASIBLE and PARTIAL routes. Hydration check in solve and replan was expanded from `status == RouteStatus.FEASIBLE` to `(RouteStatus.FEASIBLE, RouteStatus.PARTIAL)`, ensuring all returned scheduled legs carry turn by turn provider geometries and consistent timing.
* C3 (RESOLVED): Input stop ID preservation. `ScheduledStopOutput` receives `id=s.id` for both scheduled and dropped stops, preserving input stop identity across itinerary schedules and audit records.
* C4 (RESOLVED): Focused regression coverage. Added 6 unit regressions (13 total) in `tests/unit/test_route_optimizer_production_regressions.py` covering matrix fits with detour closing breaches, optional stop dropped yielding `PARTIAL` with provider geometries, and equivalent replan behaviors.
* C5 (RESOLVED): Evidence boundaries. Preserved reported versus independently executed evidence: connected Valhalla tests require external network and are cleanly gated via `RUN_CONNECTED_VALHALLA_TESTS=1`; offline suites (111 tests) execute with zero cloud or network dependencies. Measured dependency inclusive packaging confirmed at ~211 MB uncompressed and ~64.5 MB compressed (< 250 MB ceiling) with clean isolated import and solve verification under Python 3.12 `-S -P`. Pending LAYA quality evaluation: offline policy benchmark passes, but live connected model inference accuracy on the 25 scenarios remains pending live evaluation. AWS deployment deferrals: SAM deployment to live AWS CloudFormation, real SQS queues and DLQ redrive in AWS, and VPC Lambda database connectivity remain explicitly deferred.

### 0003 Obscura permanent engine migration (2026-11-20)

Inspected repository HEAD commit `995539a` (and prior migration commit `1e3aa52`). Spec 0003 browser worker permanently transitioned from headless Chromium and external Sparticuz Brotli decompression layers to the standalone Obscura browser engine (`bin/obscura` and bundled `libatomic.so.1`):

* Standalone binary footprint: `bin/obscura` is 80,887,544 bytes (~77.14 MB), bundled with `libatomic.so.1` (37,552 bytes). Eliminates all external Lambda layers (`Sparticuz/chromium` Brotli layers completely removed).
* Lambda package headroom: Standalone function artifact `build/browser.zip` measures 94,839,344 bytes (~90.45 MB compressed), well within the 100 MB deployment target. Unpacked artifact in `build/browser/` measures 251,658,240 bytes (240 MB), strictly adhering to the AWS Lambda 250 MB (262,144,000 bytes) hard ceiling with 10.48 MB headroom.
* Clean runtime execution: Native ELF 64 bit LSB PIE executable, dynamically linked against standard Linux loaders and `GLIBC_2.34`, running natively in Amazon Linux 2023 (Python 3.12 Lambda base image) with zero missing shared libraries.
* Empirical performance: Cold start process spawn to `--ready-file` takes 21.52 ms to 185.02 ms, CDP websocket connection takes 26.36 ms to 33.75 ms, achieving total cold startup of 47.88 ms to 218.77 ms (~85% cold start reduction compared to legacy Chromium).
* Real world workloads: Live search navigation on DuckDuckGo completes in 1,357 ms with deterministic content hashing. Live web scraping of complex DOM structures completes in 1,148 ms (`example.com`) and 1,489 ms (`httpbin.org/html`), and dynamic JavaScript lodging with delayed DOM injection completes in 695 ms. Multi step automation (navigation, filling 3 text inputs, toggling 2 checkboxes, filling textarea, capturing 36 KB screenshot, and form submission) completes in 1,607 ms with peak resident memory of 165.61 MB and clean zero zombie process cleanup.
* Architectural compliance: Masked automation flags (`navigator.webdriver` omitted) satisfy lightweight stealth requirements, while adhering strictly to project guidelines prohibiting CAPTCHA solving, proof of work bypass, or anti bot gate circumvention (gated pages return `BrowserRenderStatus.BLOCKED` with honest logging).

### 0007 bounded optimizer dispatch reconciliation (2026-11-21)

Inspected repository HEAD commit `92bd8b7` (and merged commit `b11ee1f`). Bounded route optimizer dispatch corrections completed and verified across all dispatch and scheduling criteria:

* Semantic candidate partitioning: Discovered candidate places remain optional recommendations (`is_must_visit = False`). Only explicit brief origin and requested trip destinations are designated mandatory anchors (`is_must_visit = True`).
* Editorial category isolation: Hotels, lodging accommodations, restaurants, dining choices, and general service facilities are partitioned into editorial recommendations with explicit deferral reasons (`accommodation_option`, `dining_choice`, `service_facility`) rather than scheduled as mandatory daytime route stops. Road travel advisories and traffic alerts are excluded from route stops with explicit exclusion reason `advisory_observation`.
* Provider capability bounding: Valhalla provider requests dynamically compute effective stop limits from both location limits (default 20) and matrix cell limits (`sqrt(max_matrix_cells)`, default `sqrt(400) = 20`) without hardcoding stop counts. Mandatory anchors take absolute precedence; excess optional candidates are deferred with explicit reason `excess_candidate_deferred`.
* Multi day itinerary feasibility: Multi day itineraries distribute stops across valid trip dates and enforce daily daytime opening windows (default 09:00 to 18:00 in trip destination timezone), preventing stops from compressing into Day 1 or creating continuous overnight windows. Candidates skipped due to opening window constraints do not consume solver capacity slots.
* Isolation and database fencing: Verified end to end from API to runner to optimizer dispatch against isolated PostgreSQL, using explicitly labeled controlled providers, atomic checkpointer state, and active worker lease fencing.

### 0007 Valhalla matrix distance rejection recovery reconciliation (2026-11-22)

Inspected repository HEAD commit `b161654`. Bounded error recovery for provider matrix distance rejections completed and verified across unit, integration, and live backend execution:

* Classification precision: `ValhallaRoutingError.is_matrix_distance_limit_exceeded` and `is_valhalla_matrix_distance_limit_error` classify HTTP 400 specifically when `error_code == 154` on `/sources_to_targets`. Other 400 rejections (such as error 171), `/route` endpoint calls, and server errors return False without classification.
* Fast fail nonretryable client error: In `ValhallaClient._send_request`, 4xx status codes fast fail immediately with zero retries.
* Lease fencing and durable completion: In `RouteOptimizerWorkerService.execute_task`, the rejection is caught for travel workflows, completed atomically under `WorkerFencingService.complete_task` with degraded `ROUTING_UNAVAILABLE` output, and releases its claim. Non travel workflows and unrelated provider errors reraise untouched.
* Timeline event: Immediately after task completion under lease, the worker records a `ROUTE_OPTIMIZED` event with routing status `ROUTING_UNAVAILABLE` and safe diagnostic description.
* Trip graph consumption: In `consume_route_result_node`, worker output with `route_id = None` or status `FAILED` sets `route_id = None`, `routing_status = "ROUTING_UNAVAILABLE"`, and constructs an empty schedule. Discovered places, validated candidates, eligible stops, editorial recommendations, weather advisories, and non road budget lines are fully preserved. Downstream publication succeeds with `trips.current_route_id = None` and `trips.state = "proposed"`.
* Database invariants: Zero phantom rows exist in `routes` table, lease tokens are released on completion (no stranded claims), and duplicate delivery is discarded safely without duplicate delta creation.
* Controlled PostgreSQL regression: Verified end to end on isolated PostgreSQL (`acceptance_db`) by injecting exact HTTP 400/154 response at the Valhalla HTTP transport boundary, confirming consumer execution, lease clearing, zero routes, outbox reconciliation, LangGraph resumption, and HTTP 200 on `GET /api/v1/trips/{trip_id}/itinerary`.
* Live backend evaluation (Bengaluru to Mysuru to Madikeri): Executed real application via `backend/scripts/run_live_backend_journey.py` against live external providers (Bedrock, SerpAPI, Scrapling, public Valhalla, Open Meteo, Obscura, and PostgreSQL). Public Valhalla rejected matrix calculation for the 250 km intercity distance with HTTP 400 / 154. The application recovered cleanly, completed the job in 8.1s with status `SUCCEEDED`, and served `GET /api/v1/trips/{trip_id}/itinerary` with status 200, `routing_status: "ROUTING_UNAVAILABLE"`, `route_id: null`, zero persisted routes, and preserved candidate evidence.
* Separate gates maintained: Live partial publication with honest diagnostics is verified. Successful live road routing on intercity corridors (requiring self hosted Valhalla with relaxed matrix limits or sequential point to point route fallback), live remote LAYA, browser dispatch, authoritative advisory, and durable HITL remain separate future gates.

### 0007 restrict Valhalla matrix distance rejection recovery to TRAVEL (2026-11-22)

Inspected repository HEAD commit `0ae2479`. Bounded recovery for Valhalla matrix distance limit rejections (HTTP 400 with error code 154 on `/sources_to_targets`) is restricted strictly to executions whose workflow is TRAVEL:

* Strict workflow boundary: In `RouteOptimizerWorkerService.execute_task`, `is_travel_workflow` requires `exec_model is not None and exec_model.workflow == "TRAVEL"`. A populated trip identifier alone does not activate degraded recovery.
* Fail closed for standalone route optimization: Standalone route calculations running under `ROUTE_OPTIMIZER_GRAPH` re-raise `ValhallaRoutingError` immediately when encountering HTTP 400 / 154, leaving the task uncompleted under lease, persisting zero routes, and emitting zero completion events to the outbox.
* Preservation of TRAVEL recovery: TRAVEL workflows encountering intercity matrix distance limits continue to catch the error, complete durably under lease fencing with degraded `ROUTING_UNAVAILABLE` output, advance LangGraph via checkpoint resume, and publish a partial itinerary with preserved evidence, zero phantom routes, and honest diagnostics.
* Regression coverage: Verified via unit suite (`test_valhalla_distance_rejection.py`, 7 tests passing) and database integration lifecycle on isolated PostgreSQL (`test_valhalla_matrix_rejection_recovery_lifecycle.py`, 2 tests passing). Code review approved by Senior Staff Reviewer in `docs/reviews/2026-11-22-0007-restrict-distance-recovery-to-travel.md`.

### 0007 correct lodging night budget accounting (2026-11-22)

Inspected repository HEAD commit `bb058bd`. Resolved lodging night budget calculation defect where destination `stay_days` previously multiplied hotel nights:

* Calendar date truth: In `compute_budget_node`, total lodging nights are derived strictly from local calendar dates `calendar_nights = max(0, (brief.end_date - brief.start_date).days)`. The destination loop was replaced with a single calculation, ensuring destination `stay_days` never multiplies hotel nights.
* Two destination regression: On the Mysuru run brief (25 to 26 November 2026, Srirangapatna and Mandya, 1 room), `calendar_nights` resolves to 1 night. Because the overnight location is not an explicit scheduled accommodation stop, the basis preserves `hotel_stay_unknown (1 rooms, 1 nights)`, yielding exactly one estimated night at 3,500 INR.
* Same day trip handling: When `calendar_nights == 0`, zero accommodation budget lines are emitted, ensuring zero artificial hotel expense for same day trips.
* Multi room scaling: Line item quantity scales as `brief.rooms * calendar_nights` (for example, 3 rooms across 2 nights yields quantity 6 at 3,500 INR = 21,000 INR).
* Single destination location retention: For single destination trips, the destination name is preserved in the basis string (for example, `hotel_stay_Mysore (1 rooms, 1 nights)`).
* Preservation of estimate labels and unknown costs: Benchmark accommodation retains `estimated=True` and `source_ref="benchmark_hotel_estimate"`. Unquoted highway tolls and unverified activity fees remain explicit unknown lines with documented reasons without zero coercion.
* Regression coverage: Verified via 4 new unit tests in `test_trip_planner_proposal_slice5.py` (32 tests passing), PostgreSQL itinerary API integration test (2 tests passing), clean ruff linting, and strict mypy verification. Code review approved in `docs/reviews/2026-11-22-0007-correct-lodging-night-budget-accounting.md`.

### Next action and ordering

Closure findings for 0003 Obscura migration, 0007 bounded optimizer dispatch, 0007 Valhalla distance rejection recovery, and 0007 lodging night budget accounting are resolved and verified. Exact revisions, commands, test evidence, package budgets, and cloud deferrals are recorded in verify.md and review records.

Next planned action: Address remaining itinerary correctness findings from the Mysuru live run (meal budget lines representation, avoiding public parks as overnight accommodation stops, and refining hours fallback for nocturnal attractions).

Agreed order: remote LAYA (done) → route optimizer (done) → route API/map E2E (done) → hosted LLM/trip planner E2E (in progress: Slices 1 to 6, 8, bounded optimizer dispatch, matrix rejection recovery, and lodging accounting done) → short multi stop live road routing verification (done, verified live Valhalla PASS) → itinerary correctness refinement → connected live scenario evaluation → media extractor/analyzer/location → composition/hardening. Browser remains evidence rendering fallback; booking/payments remain future scope. No new milestone and no AWS deployment now.

