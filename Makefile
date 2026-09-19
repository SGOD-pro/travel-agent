.PHONY: local-up local-down backend-test backend-lint backend-migrate frontend-build frontend-dev verify

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

frontend-build:
	cd frontend && npm run build

frontend-dev:
	cd frontend && npm run dev

verify: backend-lint backend-test frontend-build

