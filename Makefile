.PHONY: up down build restart test migrate logs shell-auth shell-inventory shell-reservations lint format dev prod

# ── Docker Compose ──────────────────────────────────────────────────────────

up: dev

dev:
	docker compose up --build -d

prod:
	docker compose -f docker-compose.yml up --build -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

restart:
	docker compose restart

# ── Database migrations ──────────────────────────────────────────────────────

migrate:
	docker compose exec auth alembic upgrade head
	docker compose exec inventory alembic upgrade head
	docker compose exec reservations alembic upgrade head

migrate-auth:
	docker compose exec auth alembic upgrade head

migrate-inventory:
	docker compose exec inventory alembic upgrade head

migrate-reservations:
	docker compose exec reservations alembic upgrade head

# ── Testing ──────────────────────────────────────────────────────────────────

test:
	cd services/auth && uv run pytest tests/ -v
	cd services/inventory && uv run pytest tests/ -v
	cd services/reservations && uv run pytest tests/ -v
	cd services/cabling && uv run pytest tests/ -v

test-auth:
	cd services/auth && uv run pytest tests/ -v

test-inventory:
	cd services/inventory && uv run pytest tests/ -v

test-reservations:
	cd services/reservations && uv run pytest tests/ -v

test-cabling:
	cd services/cabling && uv run pytest tests/ -v

# ── Dev shells ───────────────────────────────────────────────────────────────

shell-auth:
	docker compose exec auth bash

shell-inventory:
	docker compose exec inventory bash

shell-reservations:
	docker compose exec reservations bash

# ── Local dev (without Docker) ───────────────────────────────────────────────

install:
	uv sync --all-extras

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

# ── Lint and format ──────────────────────────────────────────────────────────

lint:
	uv run ruff check services/
	cd frontend && npx eslint src

format:
	uv run ruff format services/
	uv run ruff check --fix services/

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	docker compose down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
