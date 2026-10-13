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
| `src/functions/orchestrator/execution/` | Orchestrator execution engine, worker fencing, outbox relay, lease recovery, and reconciliation |
| `src/functions/orchestrator/places/` | Canonical places persistence, PostGIS geometry models, schemas, and spatial lookup |
| `src/functions/orchestrator/evidence/` | Claim models, schemas, claim evidence junction, and verification policies |

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
- Offline development and CI use test only fixture adapters (FixtureSearchAdapter); live provider adapters are deferred to Slice 3+.

## Gotchas

- The startup command in `start.sh` references `travel.runtime.fastapi.app:app`, which does not exist in `src`. Use `functions.api.app:app` or `main.py`.
- Budget summaries marked complete require all line items to have known amounts. A single unknown item marks the whole budget incomplete.
- Do not import worker implementations across different function folders.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
