.PHONY: local-up local-down backend-test backend-lint backend-migrate frontend-build frontend-dev backend-dev run-all verify

local-up:
	docker compose -f deployment/local/docker-compose.yml up -d

local-down:
	docker compose -f deployment/local/docker-compose.yml down

backend-migrate:
	cd backend && uv run alembic upgrade head

backend-test:
	cd backend && uv run pytest -v

backend-lint:
	cd backend && uv run ruff check . && uv run mypy src

backend-dev:
	cd backend && uv run uvicorn travel.runtime.fastapi.app:app --host 0.0.0.0 --port 8000 --reload

frontend-build:
	cd frontend && npm run build

frontend-dev:
	cd frontend && npm run dev

frontend-test-e2e:
	cd frontend && npx playwright test

run-all:
	./start.sh all

verify: backend-lint backend-test frontend-build frontend-test-e2e


