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

## Gotchas

- The startup command in `start.sh` references `travel.runtime.fastapi.app:app`, which does not exist in `src`. Use `functions.api.app:app` or `main.py`.
- Budget summaries marked complete require all line items to have known amounts. A single unknown item marks the whole budget incomplete.
- Do not import worker implementations across different function folders.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
