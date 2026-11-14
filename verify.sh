#!/usr/bin/env bash
# Master verification pipeline: lint + types + unit tests + production build.
#
# Database-backed integration tests and the Playwright E2E suite require the
# local infrastructure stack (PostgreSQL/PostGIS + Redis) and running servers:
#   make local-up && make backend-migrate && make backend-dev   (terminal 1)
#   ./start.sh frontend                                         (terminal 2)
#   cd backend && uv run pytest tests/integration/ -v
#   cd frontend && npx playwright test

set -e

echo "=== 1. Backend linter and typecheck (ruff + mypy) ==="
(cd backend && uv run ruff check . && uv run mypy src)

echo "=== 2. Backend unit test suite (database-free) ==="
(cd backend && uv run pytest tests/unit/ -v)

echo "=== 3. Frontend lint and production build (Next.js + Tailwind) ==="
(cd frontend && npm run lint && npm run build)

echo "=== 4. Frontend formatting check (Prettier) ==="
(cd frontend && npm run format:check)

echo "=== ALL VERIFICATIONS PASSED SUCCESSFULLY ==="
