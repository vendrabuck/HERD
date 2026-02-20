.PHONY: up down build test migrate logs shell-auth shell-inventory shell-reservations

# ── Docker Compose ──────────────────────────────────────────────────────────

up:
	docker compose up -d

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
	docker compose exec auth pytest tests/ -v
	docker compose exec inventory pytest tests/ -v
	docker compose exec reservations pytest tests/ -v

test-auth:
	docker compose exec auth pytest tests/ -v

test-inventory:
	docker compose exec inventory pytest tests/ -v

test-reservations:
	docker compose exec reservations pytest tests/ -v

# ── Dev shells ───────────────────────────────────────────────────────────────

shell-auth:
	docker compose exec auth bash

shell-inventory:
	docker compose exec inventory bash

shell-reservations:
	docker compose exec reservations bash

# ── Local dev (without Docker) ───────────────────────────────────────────────

install:
	uv sync

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	docker compose down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
