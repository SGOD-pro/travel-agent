# Travel Planning Platform — Backend

Python 3.12 modular backend (FastAPI + LangGraph + OR-Tools) built with strict module boundaries, ports & adapters, and a PostgreSQL/PostGIS system of record.

## Structure

```text
src/
  config/              # Settings, AWS, database engine/session configuration
  contracts/           # Cross-module data contracts (money, trips, jobs, evidence, routing, weather)
  functions/           # Deployment modules — each owns a bounded responsibility:
    api/               # FastAPI app, routes, repositories, idempotency, unit of work
    orchestrator/      # Job lifecycle, LangGraph graphs (trip_planner, route_optimizer),
                       # execution engine (leases, outbox, recovery, daemon), decision,
                       # evidence, places, LLM & weather ports/adapters
    decision_router/   # Bounded LAYA inference client
    evidence_collector/# Search, permitted extraction, SSRF defense
    browser/           # Fenced dynamic rendering worker
    optimizer/         # OR-Tools stop-order solving, constraints, costs
    route_optimizer/   # Route packaging and runtime glue
  middleware/          # Auth (fail-closed bearer token verification)
infrastructure/        # Alembic migrations and migration environment
tests/
  unit/                # Fast in-memory unit tests (database-free)
  integration/         # Async database and migration tests (require live PostgreSQL)
main.py                # Local uvicorn entrypoint (functions.api.app:app)
```

## Setup & Commands

```bash
# Sync virtual environment and install dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/unit/ -v        # database-free
uv run pytest tests/integration/ -v # requires local PostgreSQL + Redis (make local-up)

# Run linting & type checks
uv run ruff check .
uv run mypy src

# Run database migrations
uv run alembic upgrade head

# Start the API server locally
uv run python main.py
```

See [docs/reference/DATABASE.md](../docs/reference/DATABASE.md) for schema details and [docs/reference/API.md](../docs/reference/API.md) for the HTTP contract.
