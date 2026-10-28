# Backend

## Overview

FastAPI modular monolith and asynchronous background workers for SWENA. This backend handles travel comparison, route solving with OR Tools, deterministic financial calculations, and official merchant handoffs.

## Key files

| File | Owns |
|---|---|
| `main.py` | Top level application entrypoint for local development |
| `src/functions/api/app.py` | FastAPI application instance, routes, and middleware registration |
| `src/config/config.py` | Environment configuration using Pydantic Settings |
| `src/contracts/money.py` | Strict Decimal money representation, budget lines, and zero coercion rules |
| `src/functions/optimizer/solver.py` | OR Tools routing and scheduling solver |
| `src/functions/evidence_collector/` | Evidence collection, immutable observation snapshots, fixture adapters, and SQS consumer |
| `src/functions/evidence_collector/scrape.py` | Scrapling adapter for web evidence retrieval |
| `src/functions/evidence_collector/packaging.py` | Standalone Lambda packaging, dependency inclusive artifact builder, and clean environment verification |
| `src/functions/orchestrator/execution/` | Orchestrator execution engine, worker fencing, outbox relay, lease recovery, and reconciliation |
| `src/functions/orchestrator/places/` | Canonical places persistence, PostGIS geometry models, schemas, and spatial lookup |
| `src/functions/orchestrator/evidence/` | Claim models, schemas, claim evidence junction, and verification policies |
| `src/functions/browser/` | Browser escalation worker, Playwright adapter, live SSRF network controls, Scrapling parser, and fenced persistence |
| `src/functions/browser/consumer.py` | SQS consumer Lambda handler with partial batch failure reporting and deadline timeouts |
| `src/functions/browser/packaging.py` | Standalone Lambda packaging, Node runtime preservation, 185 MB function budget, and clean environment verification |
| `src/contracts/decision.py` | Internal decision domain models, action enums, envelope payloads, and port interfaces |
| `src/functions/decision_router/` | Remote LAYA decision router client adapter, TypeSafe Jev wire schemas, sanitization, and offline fixtures |
| `src/functions/orchestrator/decision/` | Decision policy validator, database models, canonical replay digests, and idempotent repository |
| `src/contracts/routing.py` | Domain routing contracts, stop inputs, leg outputs, and fuel profiles |
| `src/functions/route_optimizer/` | OR Tools VRPTW routing solver, vehicle continuity tracker, multimodal parking hubs, Valhalla client, closure avoidance, mid trip replanning, and fenced worker service |
| `src/functions/route_optimizer/consumer.py` | Dedicated SQS consumer Lambda handler with partial batch failure reporting and deadline guards |
| `src/functions/route_optimizer/packaging.py` | Standalone Lambda packaging under 250 MB unpacked (0.28 MB lightweight, ~211 MB dependency inclusive), asset auditing, and clean isolated runtime verification |
| `src/functions/api/routes/optimization.py` | Asynchronous route optimization, job discovery by-job, map GeoJSON, route retrieval, and replanning endpoints |
| `src/functions/orchestrator/graphs/route_optimizer/` | RouteOptimizerGraph LangGraph state machine, solver task emission, and route finalization |
| `src/functions/orchestrator/routing/` | Route persistence repository, single child replan constraint, PostGIS spatial models, and RFC 7946 GeoJSON map assembler |
| `src/acceptance_server.py` | Local acceptance server and worker orchestration with injected MockValhallaClient for browser testing |

## Commands

```bash
# Install dependencies
uv sync

# Run backend development server
uv run python main.py

# Run tests
uv run pytest -v

# Run linter and type checker
uv run ruff check . && uv run mypy src

# Package isolated evidence collector Lambda
uv run python -m functions.evidence_collector.packaging

# Package isolated browser worker Lambda
uv run python -m functions.browser.packaging

# Package isolated route optimizer Lambda
uv run python -m functions.route_optimizer.packaging

# Run database migrations
uv run alembic upgrade head
```

## Conventions

- Use Python 3.12 with uv as the package manager.
- Money values must use Decimal and the Money value object. Never use floating point numbers for currency.
- Unknown expenses such as unquoted tolls must stay explicit unknowns with an unknown reason. Never coerce unknown costs to zero.
- PostgreSQL with PostGIS is the single source of truth for trips, versions, and jobs.
- Validate incoming tokens using SWYRA Auth JWKS. Reject client supplied owner identifiers.
- Route solving uses OR Tools for stop sequencing while keeping road network geometry from routing providers.
- Orchestrator operations enforce parent first lock ordering (executions then tasks) and savepoint transaction isolation on fencing mutations.
- Source observations in PostgreSQL are write once and immutable, keyed by (execution_id, canonical_url, content_hash) with zero in place mutations.
- Evidence collection and worker fencing commit atomically in transaction 4; any failure rolls back staged observations cleanly.
- Canonical places persist reviewed public identity with PostGIS Point geometry and GiST spatial indexes.
- Claims link to source observations through claim evidence and preserve contradictory assertions as conflicted without overwriting.
- Offline tests use explicit fixture adapter injection; production runtime path constructs live SerpApi and Scrapling adapters with missing credentials producing explicit unavailable records.
- Evidence collector packages independently into an isolated Lambda artifact under 50 MB compressed and 250 MB uncompressed, excluding orchestrator, optimizer, and heavy third party libraries.
- Browser worker packages independently into an isolated Lambda artifact preserving Node runtime under 185 MB function unpacked, 70 MB compressed intended for S3 deployment bucket staging, and 250 MB combined with external Sparticuz Chromium layer (64.50 MB); layer Brotli archives decompress to /tmp/chromium at startup via bundled Node runtime.
- Orchestrator modules must not import evidence collector business code; task dispatch routes via dedicated SQS queues.
- Browser escalation worker handles evidence extraction and rendering only; booking, payment, and transaction automation remain deferred.
- Browser navigation enforces pre navigation DNS screening, SecureEgressProxy socket connection binding to validated IP literals defeating DNS rebinding, in flight route interception against private and metadata IPs, Service Worker blocking, and WebSocket elimination.
- Remote LAYA model is served from Kaggle through Cloudflare tunnel.
- Decision router imports only contracts.decision, config.config, standard library, httpx, and pydantic, with zero database models or orchestrator dependencies.
- Remote LAYA decision client enforces cumulative retry deadlines, exponential backoff for 5xx errors, immediate failure on 4xx responses, and context PII sanitization.
- Decision policy validator enforces four point revalidation for result reuse (matching phase, context version, policy version, active eligible choices) and deterministic fallback on errors, timeouts, or low confidence (< 0.700).
- Evidence routing validates required destination identity, canonical coordinates within 100 km, and substantive persisted sources; database query failures fail closed without falling back to unpersisted task outputs, and stale state flags cannot override authoritative validation.
- LangGraph evidence routing node records chronological decision envelopes in laya_decisions table and executes between candidate discovery and dynamic evidence escalation.
- Trip planner workflow compiles with an interrupt before await_evidence_results, resuming back to route_evidence_decision once completed task results or persisted source observations arrive.
- Route optimizer and trip planner end to end workflows precede media location development.
- Route optimizer enforces forbidden unreachable transitions via arc removal, dwell completion before venue closing, and honest solver statuses (OPTIMAL requires proof, timeout returns TIME_LIMIT).
- Multimodal routes for private vehicles automatically discover verified public parking hubs within DEFAULT_PARKING_SEARCH_RADIUS_METERS, failing closed with INFEASIBLE and NO_VERIFIED_PARKING_HUB when missing.
- Vehicle continuity tracker guarantees driving legs depart only from the stop where the vehicle is currently parked, preventing teleportation, and fuel calculations strictly isolate driving distance from walking.
- Valhalla HTTP client provides matrices and turn by turn routes with polyline6 decoding, bounded retry backoff, and coordinate boundary safety.
- Closure avoidance service transforms verified road restrictions into Valhalla exclude polygons and enforces post routing spatial revalidation with strict fail closed semantics.
- Shared configured Valhalla client provider instance is used across solve, multimodal, and replan workflows without implicit production haversine or hardcoded speed fallbacks.
- Post hydration arrival, wait, and dwell times are revalidated against window end; mandatory breaches transition to INFEASIBLE, while optional breaches drop the stop, re solve remaining stops, and return PARTIAL.
- Provider hydration and schedule revalidation apply to both FEASIBLE and PARTIAL routes in solve and replan paths, ensuring all scheduled legs carry provider geometries.
- ScheduledStopOutput preserves input stop identifier for both scheduled and dropped stops.
- Mid trip replanning freezes completed stops with historical timestamps, injects traveler GPS coordinates as departure origin, re optimizes remaining stops, and enforces two tier optimistic concurrency checks on immutable route versions.
- Route optimizer worker service executes under WorkerFencingService with parent first locking order, cooperative cancellation checks, and atomic completion.
- Route optimizer consumer reports SQS partial batch failures via batchItemFailures and uses remaining execution time guards (< 5000ms) for safe redrive.
- Standalone completion reconciliation scopes strictly to optimizer workflows, leaving orchestrator graph workflows in waiting states untouched.
- Route optimizer packages independently into an isolated Lambda artifact under 50 MB compressed and 250 MB uncompressed (0.28 MB lightweight, ~211 MB dependency inclusive), excluding LangGraph, Playwright, and FastAPI dependencies.
- Optimization endpoints enforce caller tenant ownership with explicit 403 Forbidden responses on tenant mismatch.
- Replan endpoint enforces (owner_id, "route_optimization:replan", idemp_key) idempotency semantics: identical payload replays the cached 202 response even after completion, changed payload returns 409 Conflict, and retry is permitted after terminal failure or cancellation when no child route was committed.
- Acceptance testing and local worker polling strictly isolate owned test fixtures and executions without global outbox acknowledgements or unrelated route mutations.
- RouteOptimizerGraph compiles with interrupt_before=[RouteOptimizerNode.AWAIT_SOLVER_RESULT], clearing pending_tasks upon resumption to eliminate duplicate dispatch, and preserves INFEASIBLE route outcomes with status SUCCEEDED (see spec 0006).
- GET /api/v1/optimization/routes/{route_id}/map returns RFC 7946 GeoJSON FeatureCollection with PostGIS stop coordinates, JSONB leg polylines, simplestyle presentation attributes, and 4 element bbox (see spec 0006).
- GET /api/v1/optimization/routes/{route_id}/versions traces root to child replan lineage with recursive PostgreSQL CTE, computing distance deltas, duration deltas, and dropped stops (see spec 0006).

## Gotchas

- The startup command in `start.sh` references `travel.runtime.fastapi.app:app`, which does not exist in `src`. Use `functions.api.app:app` or `main.py`.
- Budget summaries marked complete require all line items to have known amounts. A single unknown item marks the whole budget incomplete.
- Do not import worker implementations across different function folders.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
