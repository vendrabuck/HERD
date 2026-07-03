# Service lists (single source of truth)
#
# SERVICES:    every backend service with a pytest suite.
# DB_SERVICES: subset that owns a Postgres schema and ships Alembic migrations.
#              These are also the services worth opening a shell into.
SERVICES := common auth inventory reservations cabling acl execution config ai-orchestrator user-profile notifications integration secrets
DB_SERVICES := auth inventory reservations cabling acl execution user-profile notifications ai-orchestrator integration secrets

# Root-level Python helper scripts. These live outside services/ so they are not
# covered by the workspace ruff config's default discovery, so lint/format target
# them explicitly. Add new repo-root scripts here so they are linted and
# format-checked locally and in CI. Today this is just the seed script.
ROOT_PY := seed_devices_public.py

# Coverage package name per service. Most are app/; common ships herd_common/.
cov_pkg = $(if $(filter common,$(1)),herd_common,app)

# Self-documenting help is the default goal: a bare `make` prints the target list.
.DEFAULT_GOAL := help

.PHONY: help audit master master-quick master-clean clean-images everything \
	up dev prod down build logs restart \
	migrate test coverage \
	$(addprefix test-,$(SERVICES)) \
	$(addprefix coverage-,$(SERVICES)) \
	$(addprefix migrate-,$(DB_SERVICES)) \
	$(addprefix shell-,$(DB_SERVICES)) \
	test-frontend test-integration test-contract test-load test-load-ui test-e2e test-e2e-stop test-auth-ldap \
	test-root coverage-parallel coverage-frontend \
	install frontend-install frontend-dev lint format clean seed \
	ldap-up ldap-down ldap-status ldap-logs ldap-reset \
	_master-stack-up _master-wait-healthy _master-stack-down _everything-seed

## --- Meta ---

# -- Help ---------------------------------------------------------------------
#
# `make` or `make help` prints every target annotated with a `## ` comment on the
# same line, grouped under the `## --- heading ---` markers below. Add a `## text`
# suffix to a target's rule line to surface it here; per-service generated targets
# (test-<svc>, migrate-<svc>, ...) are summarized rather than listed individually.

help:  ## Show this help (the default target)
	@echo "HERD Makefile. Common targets:"
	@echo ""
	@awk 'BEGIN {FS = ":.*## "} \
		/^## --- / {sub(/^## --- /, ""); sub(/ ---$$/, ""); printf "\n%s\n", $$0; next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Per-service targets (one per name in SERVICES / DB_SERVICES):"
	@echo "  test-<svc>      run one service's unit suite"
	@echo "  coverage-<svc>  run one service's suite under coverage (html)"
	@echo "  migrate-<svc>   alembic upgrade head in one DB service"
	@echo "  shell-<svc>     open a bash shell in one DB service container"
	@echo "  SERVICES    = $(SERVICES)"
	@echo "  DB_SERVICES = $(DB_SERVICES)"

# -- Audit (dependency / security) --------------------------------------------
#
# Audits installed dependencies for known vulnerabilities: pip-audit for the
# Python workspace (uses the dev-group pip-audit) and `npm audit` for the
# frontend. Advisory only; it does not fail the build by default. Run
# `make audit AUDIT_STRICT=1` to make either non-empty result a hard failure.

audit:  ## Audit Python and frontend deps for known vulnerabilities
	@echo "=== Python dependency audit (pip-audit) ==="
	@if [ -n "$$AUDIT_STRICT" ]; then \
		uv run pip-audit; \
	else \
		uv run pip-audit || echo "pip-audit reported findings (advisory; set AUDIT_STRICT=1 to fail)"; \
	fi
	@echo ""
	@echo "=== Frontend dependency audit (npm audit) ==="
	@if [ -n "$$AUDIT_STRICT" ]; then \
		cd frontend && npm audit; \
	else \
		cd frontend && npm audit || echo "npm audit reported findings (advisory; set AUDIT_STRICT=1 to fail)"; \
	fi

## --- Validation pipelines ---

# -- Master (full validation: lint + unit + build + integration + e2e) --
#
# master-quick is the fast iteration target: lint + unit tests + frontend + build.
# master is the full "one command validates everything" pipeline and brings up
# an ephemeral stack for integration + e2e before tearing it back down.

master-quick:  ## Fast gate: format + lint + unit + frontend + build
	@echo ""
	@echo "=== Installing Python dependencies ==="
	uv sync --all-extras
	@echo ""
	@echo "=== Installing frontend dependencies ==="
	cd frontend && npm ci
	@echo ""
	@echo "=== Formatting Python ==="
	$(MAKE) format
	@echo ""
	@echo "=== Linting Python ==="
	uv run ruff check services/
	@echo ""
	@echo "=== Linting frontend ==="
	cd frontend && npx eslint src --max-warnings 0
	@echo ""
	@echo "=== Backend tests ($(words $(SERVICES)) services) ==="
	$(MAKE) test
	@echo ""
	@echo "=== Frontend tests ==="
	cd frontend && npm test
	@echo ""
	@echo "=== Building frontend ==="
	cd frontend && npm run build
	@echo ""
	@echo "=== Building Docker images ==="
	@if [ "$$BUILD_NO_CACHE" = "1" ]; then \
		docker compose build --no-cache; \
	else \
		docker compose build; \
	fi
	@echo ""
	@echo "=== master-quick complete ==="

master: clean master-quick  ## Full gate: master-quick + ephemeral stack contract/integration/e2e
	@echo ""
	@echo "=== Starting ephemeral stack for integration + e2e ==="
	@# Always tear the stack down, even if integration or e2e fails.
	@trap '$(MAKE) _master-stack-down' EXIT INT TERM; \
		$(MAKE) _master-stack-up && \
		$(MAKE) _master-wait-healthy && \
		echo "" && echo "=== Contract tests ===" && \
		$(MAKE) test-contract && \
		echo "" && echo "=== Integration tests ===" && \
		$(MAKE) test-integration && \
		echo "" && echo "=== E2E tests ===" && \
		$(MAKE) test-e2e
	@echo ""
	@echo "=== master complete ==="

# -- Master (clean-image variant) ---------------------------------------------
#
# Same as `make master` but first removes every Docker image that belongs to
# this compose project and forces `docker compose build --no-cache` so no
# BuildKit layer cache is reused. Unrelated Docker images and unrelated build
# cache on the machine are untouched.

clean-images:
	@echo ""
	@echo "=== Removing HERD compose images ==="
	-docker compose down -v --remove-orphans
	@ids=$$(docker images --filter "reference=herd-*" -q | sort -u); \
		if [ -n "$$ids" ]; then \
			echo "$$ids" | xargs -r docker rmi -f; \
		else \
			echo "No HERD images to remove."; \
		fi
	@echo ""
	@echo "=== Pruning dangling images ==="
	-docker image prune -f

master-clean: clean-images  ## master with all HERD compose images rebuilt --no-cache
	@BUILD_NO_CACHE=1 $(MAKE) master

# -- Everything (full prod-grade gate: master + coverage + stress + fail-on-dirty) --
#
# Differences from `master`:
#   - `ruff format --check` instead of `ruff format` (no source mutation).
#   - Runs backend and frontend tests under coverage (master skips coverage).
#   - Adds headless locust stress test after e2e.
# Everything stops on first failure; teardown is trapped.

everything: clean  ## Closest-to-CI gate: master + coverage + format-check + load tests
	@echo ""
	@echo "=== Installing Python dependencies ==="
	uv sync --all-extras
	@echo ""
	@echo "=== Installing frontend dependencies ==="
	cd frontend && npm ci
	@echo ""
	@echo "=== Checking Python formatting (fail on dirty) ==="
	uv run ruff format --check services/
	@echo ""
	@echo "=== Linting Python ==="
	uv run ruff check services/
	@echo ""
	@echo "=== Linting frontend ==="
	cd frontend && npx eslint src --max-warnings 0
	@echo ""
	@echo "=== Backend tests with coverage ($(words $(SERVICES)) services) ==="
	$(MAKE) coverage
	@echo ""
	@echo "=== Frontend tests with coverage ==="
	$(MAKE) coverage-frontend
	@echo ""
	@echo "=== Building frontend ==="
	cd frontend && npm run build
	@echo ""
	@echo "=== Building Docker images ==="
	docker compose build
	@echo ""
	@echo "=== Starting ephemeral stack for integration + e2e + load ==="
	@trap '$(MAKE) _master-stack-down' EXIT INT TERM; \
		$(MAKE) _master-stack-up && \
		$(MAKE) _master-wait-healthy && \
		echo "" && echo "=== Contract tests ===" && \
		$(MAKE) test-contract && \
		echo "" && echo "=== Integration tests ===" && \
		$(MAKE) test-integration && \
		echo "" && echo "=== E2E tests ===" && \
		$(MAKE) test-e2e && \
		echo "" && echo "=== Seeding stack for load tests ===" && \
		$(MAKE) _everything-seed && \
		echo "" && echo "=== Load / stress tests ===" && \
		HERD_BASE_URL=https://localhost $(MAKE) test-load
	@echo ""
	@echo "=== everything complete ==="

# Seeds the running stack via seed_devices_public.py: users, drivers, templates,
# devices, ports, L1/L2 switches, cabling, device/user groups, 6 isolated demo
# devices, and 60 demo lab topologies (50 valid + 10 deliberately invalid). The
# script is re-runnable and skips resources that already exist.
#
# Credential resolution (highest priority first):
#   1. SEED_EMAIL / SEED_PASSWORD already in the shell env.
#   2. SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD read from .env via grep (not
#      `source`, so unquoted placeholder values elsewhere in .env cannot crash
#      the recipe). Values are taken verbatim after the first `=`, so an embedded
#      `=` survives; surrounding quotes in .env are not stripped, so keep .env
#      values unquoted.
#   3. The seed script's own fallback (admin@example.com) if both are empty.
# If .env is missing we say so and fall through to the script's defaults rather
# than failing: a stack bootstrapped with default creds still seeds.
_everything-seed:
	@if [ ! -f .env ]; then \
		echo "No .env found; relying on shell SEED_*/SUPERADMIN_* or the seed script defaults."; \
	fi
	@email=$$(grep -E '^SUPERADMIN_EMAIL=' .env 2>/dev/null | head -1 | cut -d= -f2-); \
	 pw=$$(grep -E '^SUPERADMIN_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2-); \
	 export SEED_EMAIL="$${SEED_EMAIL:-$$email}"; \
	 export SEED_PASSWORD="$${SEED_PASSWORD:-$$pw}"; \
	 export SEED_BASE_URL="$${SEED_BASE_URL:-$${HERD_BASE_URL:-https://localhost/api}}"; \
	 if [ -z "$$SEED_EMAIL" ]; then \
		echo "No SEED_EMAIL/SUPERADMIN_EMAIL resolved; seed script will fall back to its admin@example.com default."; \
	 fi; \
	 echo "Seeding $$SEED_BASE_URL (users, devices, switches, cabling, groups, isolated demo devices, demo topologies) as $${SEED_EMAIL:-<script default>}"; \
	 uv run python seed_devices_public.py

# Public alias so `make seed` works against an already-running stack.
seed: _everything-seed  ## Seed a running stack with demo users, devices, cabling, and topologies

_master-stack-up:
	docker compose up -d --build

_master-wait-healthy:
	@echo "Waiting for stack to report healthy..."
	@TIMEOUT=180; START=$$(date +%s); \
		until curl -skf https://localhost/api/auth/health > /dev/null 2>&1; do \
			NOW=$$(date +%s); ELAPSED=$$((NOW - START)); \
			if [ $$ELAPSED -gt $$TIMEOUT ]; then \
				echo "Timed out after $${TIMEOUT}s waiting for stack"; exit 1; \
			fi; \
			sleep 2; \
		done; \
		echo "Stack healthy after $${ELAPSED}s"

_master-stack-down:
	@echo ""
	@echo "=== Tearing down ephemeral stack ==="
	-$(MAKE) test-e2e-stop
	-docker compose down -v

## --- Docker Compose ---

# -- Docker Compose -----------------------------------------------------------

up: dev  ## Start the full stack in dev mode (hot reload), alias of dev

dev:  ## Start the full stack in dev mode (build + detached)
	docker compose up --build -d

prod:  ## Start the stack without dev overrides (no reload)
	docker compose -f docker-compose.yml up --build -d

down:  ## Stop the stack
	docker compose down

build:  ## Build all service images
	docker compose build

logs:  ## Tail logs for all services
	docker compose logs -f

restart:  ## Restart all running services
	docker compose restart

## --- Database migrations ---

# -- Database migrations ------------------------------------------------------
#
# Per-service `migrate-<svc>` and the aggregator `migrate` are generated from
# DB_SERVICES. Only `config` is DB-less, so it has no migrate target;
# ai-orchestrator IS DB-backed (it owns the ai_orchestrator schema) and is
# included in DB_SERVICES, so `make migrate-ai-orchestrator` works.

migrate:  ## Run alembic upgrade head in every DB service
	@for svc in $(DB_SERVICES); do \
		echo ">>> migrate $$svc"; \
		docker compose exec $$svc alembic upgrade head || exit $$?; \
	done

$(addprefix migrate-,$(DB_SERVICES)):
	docker compose exec $(@:migrate-%=%) alembic upgrade head

## --- Testing ---

# -- Testing ------------------------------------------------------------------
#
# Per-service `test-<svc>` and the aggregator `test` are generated from SERVICES.

# Repo-root unit tests (tests/unit/) cover root scripts like
# seed_devices_public.py. They need no stack and are not under services/, so
# the per-service loops never reach them; both `test` and `coverage` depend on
# this target so they cannot be silently skipped locally or in CI.
test-root:  ## Run the repo-root unit tests (tests/unit, no stack needed)
	uv run pytest tests/unit/ -v

test: test-root  ## Run every backend service's unit suite (plus tests/unit)
	@for svc in $(SERVICES); do \
		echo ">>> test $$svc"; \
		(cd services/$$svc && uv run pytest tests/ -v) || exit $$?; \
	done

$(addprefix test-,$(SERVICES)):
	cd services/$(@:test-%=%) && uv run pytest tests/ -v

test-frontend:  ## Run the frontend vitest suite
	cd frontend && npm test

test-integration:  ## Run cross-service integration tests (needs a running stack)
	uv run pytest tests/integration/ -v --timeout=30 -x

# Contract: OpenAPI shape-signature snapshots. Each service's /openapi.json is
# fetched from the live stack and compared against tests/contract/snapshots/<svc>.json.
# Requires the stack to be up (master/everything trap blocks handle that).
# Regenerate snapshots intentionally with HERD_UPDATE_OPENAPI_SNAPSHOTS=1.
test-contract:  ## Run OpenAPI contract snapshot tests (needs a running stack)
	uv run pytest tests/contract/ -v

test-load:  ## Run headless locust load test (needs a running stack)
	cd tests/load && uv run locust -f locustfile.py --host $${HERD_BASE_URL:-https://localhost} --headless -u 20 -r 5 --run-time 1m

test-load-ui:  ## Run locust with its web UI (needs a running stack)
	cd tests/load && uv run locust -f locustfile.py --host $${HERD_BASE_URL:-https://localhost}

test-e2e:  ## Run Selenium e2e tests (needs a running stack)
	-docker compose --profile e2e rm -fsv selenium
	docker compose --profile e2e up -d --force-recreate selenium
	uv run pytest tests/e2e/ -v --tb=short

test-e2e-stop:  ## Stop and remove the e2e Selenium container
	-docker compose --profile e2e rm -fsv selenium

# -- Local LDAP test server ---------------------------------------------------
#
# A standalone osixia/openldap container lives outside this repo (defaults to
# $(HOME)/ldapserver, see docs/local-ldap on this machine). The targets below
# drive its lifecycle and run the live-LDAP auth tests against it. Override
# HERD_LDAP_DIR to point elsewhere.

HERD_LDAP_DIR ?= $(HOME)/ldapserver
HERD_TEST_LDAP_HOST ?= 127.0.0.1
HERD_TEST_LDAP_PORT ?= 389

ldap-up:
	@if [ ! -d "$(HERD_LDAP_DIR)" ]; then \
		echo "LDAP server dir not found at $(HERD_LDAP_DIR)."; \
		echo "Set HERD_LDAP_DIR or follow docs/local-ldap to install it."; exit 1; \
	fi
	@if [ -x "$(HERD_LDAP_DIR)/setup.sh" ] && ! docker ps --format '{{.Names}}' | grep -q '^ldap-test$$'; then \
		cd "$(HERD_LDAP_DIR)" && ./setup.sh; \
	else \
		cd "$(HERD_LDAP_DIR)" && docker compose start; \
	fi
	@echo "LDAP server up on ldap://$(HERD_TEST_LDAP_HOST):$(HERD_TEST_LDAP_PORT)"

ldap-down:
	@if [ -d "$(HERD_LDAP_DIR)" ]; then cd "$(HERD_LDAP_DIR)" && docker compose stop; fi

ldap-status:
	@if [ -d "$(HERD_LDAP_DIR)" ]; then cd "$(HERD_LDAP_DIR)" && docker compose ps; \
	else echo "LDAP server dir not found at $(HERD_LDAP_DIR)"; fi

ldap-logs:
	@if [ -d "$(HERD_LDAP_DIR)" ]; then cd "$(HERD_LDAP_DIR)" && docker compose logs -f; \
	else echo "LDAP server dir not found at $(HERD_LDAP_DIR)"; exit 1; fi

ldap-reset:
	@if [ ! -d "$(HERD_LDAP_DIR)" ]; then echo "LDAP server dir not found at $(HERD_LDAP_DIR)"; exit 1; fi
	cd "$(HERD_LDAP_DIR)" && docker compose down -v && ./setup.sh

# Run only the live-LDAP auth tests. Requires the directory above to be up;
# `make ldap-up test-auth-ldap` will start it first if needed.
test-auth-ldap:
	cd services/auth && HERD_TEST_LDAP_HOST=$(HERD_TEST_LDAP_HOST) HERD_TEST_LDAP_PORT=$(HERD_TEST_LDAP_PORT) \
		uv run pytest tests/test_ldap_service_live.py -v

# -- Coverage -----------------------------------------------------------------
#
# Per-service `coverage-<svc>` and the aggregator `coverage` are generated from
# SERVICES. common uses --cov=herd_common; all others use --cov=app.
#
# Set COV_XML=1 to additionally emit a coverage-<svc>.xml report at the repo
# root for each service (used by CI as both the test gate and artifact source).

coverage: test-root  ## Run every backend suite under coverage (COV_XML=1 emits xml)
	@for svc in $(SERVICES); do \
		pkg=app; [ "$$svc" = "common" ] && pkg=herd_common; \
		echo ">>> coverage $$svc ($$pkg)"; \
		xml=; [ -n "$$COV_XML" ] && xml="--cov-report=xml:$(CURDIR)/coverage-$$svc.xml"; \
		(cd services/$$svc && uv run pytest tests/ -v --cov=$$pkg --cov-report=term $$xml) || exit $$?; \
	done

# Parallel-friendly aggregator: each service is its own make target, so
# `make -j4 coverage-parallel COV_XML=1` runs four suites at once (each suite
# runs in its own service dir, so .coverage data files never collide). CI uses
# this; `coverage` keeps the sequential loop for readable local output.
coverage-parallel: test-root $(addprefix coverage-,$(SERVICES))  ## coverage, parallel-safe via make -j (CI)

# Per-service coverage. Default emits an html report for local browsing;
# COV_XML=1 swaps it for the xml report CI uploads (coverage-<svc>.xml at the
# repo root), keeping coverage-parallel and coverage byte-identical artifacts.
$(addprefix coverage-,$(SERVICES)):
	cd services/$(@:coverage-%=%) && uv run pytest tests/ -v \
		--cov=$(call cov_pkg,$(@:coverage-%=%)) --cov-report=term \
		$(if $(COV_XML),--cov-report=xml:$(CURDIR)/coverage-$(@:coverage-%=%).xml,--cov-report=html)

coverage-frontend:  ## Run the frontend suite under coverage
	cd frontend && npx vitest run --coverage

# -- Dev shells ---------------------------------------------------------------
#
# Per-service `shell-<svc>` is generated from DB_SERVICES (the services worth
# poking around inside). config is DB-less so it has no shell target;
# ai-orchestrator is DB-backed and is included.

$(addprefix shell-,$(DB_SERVICES)):
	docker compose exec $(@:shell-%=%) bash

## --- Local dev (without Docker) ---

# -- Local dev (without Docker) -----------------------------------------------

install:  ## Install Python deps (uv sync --all-extras)
	uv sync --all-extras

frontend-install:  ## Install frontend deps (npm install)
	cd frontend && npm install

frontend-dev:  ## Run the Vite dev server
	cd frontend && npm run dev

## --- Lint and format ---

# -- Lint and format ----------------------------------------------------------
#
# Lint and format cover services/, the repo-root Python in ROOT_PY, and the
# frontend. CI mirrors this (ruff check + ruff format --check over the same
# paths), so a style violation in a root script fails locally and in CI.

lint:  ## Lint backend (services/ + root scripts) and frontend
	uv run ruff check services/ $(ROOT_PY)
	cd frontend && npx eslint src --max-warnings 0

format:  ## Format and autofix backend Python (services/ + root scripts)
	uv run ruff format services/ $(ROOT_PY)
	uv run ruff check --fix services/ $(ROOT_PY)

## --- Cleanup ---

# -- Cleanup ------------------------------------------------------------------

clean:  ## Tear down stack and remove caches/coverage artifacts
	docker compose down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name coverage.xml -delete 2>/dev/null || true
	find . -type f -name .coverage -delete 2>/dev/null || true
