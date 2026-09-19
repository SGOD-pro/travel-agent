#!/usr/bin/env bash
set -e

echo "=== 1. Checking backend linter and typecheck ==="
(cd backend && uv run ruff check . && uv run mypy src)

echo "=== 2. Running backend unit test suite ==="
(cd backend && uv run pytest -v)

echo "=== 3. Building frontend (Next.js + Tailwind) ==="
(cd frontend && npm run build)

echo "=== 4. Running Playwright E2E test suite ==="
(cd frontend && npx playwright test)

echo "=== ALL VERIFICATIONS PASSED SUCCESSFULLY ==="
