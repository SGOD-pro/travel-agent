# Travel Planning Platform — Backend

Python 3.12 modular backend built with clean architecture, strict domain boundaries, and ports & adapters.

## Structure

```text
src/travel/
  domain/         # Pure business models (Money, Trips, Jobs) with zero framework/DB dependencies
  application/    # Ports (CachePort, TripRepositoryPort, JobRepositoryPort, UnitOfWorkPort)
  infrastructure/ # Adapters for Redis (Upstash HTTP and local asyncio) and Settings
  persistence/    # SQLAlchemy 2.0 ORM models, Repositories, Unit of Work, and Alembic migrations
tests/
  unit/           # Fast in-memory unit tests (100% database-free)
  integration/    # Async database and migration tests
```

## Setup & Commands

```bash
# Setup virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"

# Run tests
uv run pytest -v

# Run linting & type checks
uv run ruff check .
uv run mypy src

# Run database migrations
uv run alembic upgrade head
```
