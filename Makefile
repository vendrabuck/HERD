# Service lists (single source of truth)
#
# SERVICES:    every backend service with a pytest suite.
# DB_SERVICES: subset that owns a Postgres schema and ships Alembic migrations.
#              These are also the services worth opening a shell into.
SERVICES := common auth inventory reservations cabling acl execution config ai-orchestrator user-profile notifications integration secrets
DB_SERVICES := auth inventory reservations cabling acl execution user-profile notifications ai-orchestrator integration secrets

# Repo-root Python that lives outside services/, so it is not covered by the
# workspace ruff config's default discovery and lint/format must target it
# explicitly. Add new repo-root packages or scripts here so they are linted and
# format-checked locally and in CI. Today this is the seedtools seeding package
# (issue #791) and the CI image-vs-lock guard script (issue #593).
ROOT_PY := seedtools/ scripts/check_image_matches_lock.py

# The ephemeral master/everything gate stack runs in its OWN compose project so
# its volumes never collide with the dev stack's: the gate is always born fresh
# (gate-clean purges only the gate project) and `make up` after a gate run
# restores the dev stack WITH its data, no reseed needed. A SUCCESSFUL
# `make everything` leaves its gate stack running and seeded (both projects
# publish the same host ports, so run `make gate-down` before `make up`);
# master always tears its gate stack down. Lowercased because
# compose project names reject uppercase (leading non-alphanumerics are
# stripped, also a compose rule); derived from the directory so worktree gates
# are isolated per-worktree too.
#
# Gate-phase sub-makes that run tests pass COMPOSE_PROJECT_NAME=$(GATE_PROJECT)
# on the command line: make exports command-line variables to recipe
# environments, so every bare `docker compose` in those recipes AND in test
# code (integration tests stop/start containers, the config e2e test execs
# into the config container) resolves to the gate project. A standalone
# `make test-e2e` or `make test-integration` has no override and targets the
# dev stack as before.
GATE_PROJECT := $(shell printf '%s' '$(notdir $(CURDIR))' | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-\n' '-' | sed 's/^[^a-z0-9]*//')-gate
GATE_COMPOSE := docker compose -p $(GATE_PROJECT)

# The dev stack's own compose project name, same derivation as GATE_PROJECT
# minus the "-gate" suffix (docker compose's own default when no `name:` /
# `-p` is given). Used by nos-attach/nos-detach below to find the dev stack's
# network from a worktree whose CURDIR-derived name would otherwise be wrong;
# an explicit COMPOSE_PROJECT_NAME always wins, the same override
# _gate-ldap-stack-tests supports.
DEV_PROJECT := $(shell printf '%s' '$(notdir $(CURDIR))' | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-\n' '-' | sed 's/^[^a-z0-9]*//')

# Coverage package name per service. Most are app/; common ships herd_common/.
cov_pkg = $(if $(filter common,$(1)),herd_common,app)

# Self-documenting help is the default goal: a bare `make` prints the target list.
.DEFAULT_GOAL := help

.PHONY: help audit master master-quick master-clean everything everything-noload \
	up dev prod down build logs restart \
	migrate test coverage \
	$(addprefix test-,$(SERVICES)) \
	$(addprefix coverage-,$(SERVICES)) \
	$(addprefix migrate-,$(DB_SERVICES)) \
	$(addprefix shell-,$(DB_SERVICES)) \
	test-frontend test-integration test-integration-service test-contract test-load test-load-ui test-e2e test-e2e-seeded test-e2e-stop test-auth-ldap \
	test-root coverage-parallel coverage-frontend \
	install frontend-install frontend-dev lint format clean clean-data gate-clean gate-down seed seed-frr seed-nos \
	ldap-up ldap-down ldap-status ldap-logs ldap-reset _gate-ldap-tests \
	nos-up nos-down nos-status nos-logs nos-reset nos-attach nos-detach \
	nos-test-dialect nos-test-feature \
	_gate-ldap-stack-tests _gate-pg-live-tests \
	_master-stack-up _master-wait-healthy _master-stack-down _everything-seed _clean-images _test-e2e-run \
	_collect-stack-diagnostics

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
	uv run ruff check services/ $(ROOT_PY)
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

master: gate-clean master-quick  ## Full gate: master-quick + live LDAP + ephemeral stack contract/integration/e2e
	@echo ""
	@echo "=== Live LDAP auth tests ==="
	$(MAKE) _gate-ldap-tests
	@echo ""
	@echo "=== Starting ephemeral stack for integration + e2e ==="
	@# Always tear the stack down, even if integration or e2e fails.
	@trap '$(MAKE) _master-stack-down' EXIT INT TERM; \
		$(MAKE) _master-stack-up && \
		$(MAKE) _master-wait-healthy && \
		echo "" && echo "=== Contract tests ===" && \
		$(MAKE) test-contract && \
		echo "" && echo "=== Integration tests ===" && \
		$(MAKE) test-integration COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		echo "" && echo "=== E2E tests ===" && \
		$(MAKE) test-e2e COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		echo "" && echo "=== LDAP-mode stack tests (gate auth switched to LDAP) ===" && \
		$(MAKE) _gate-ldap-stack-tests COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		echo "" && echo "=== Postgres-live LDAP sync tests ===" && \
		$(MAKE) _gate-pg-live-tests
	@echo ""
	@echo "=== master complete ==="

# -- Master (clean-image variant) ---------------------------------------------
#
# Same as `make master` but first removes every Docker image that belongs to
# this compose project and forces `docker compose build --no-cache` so no
# BuildKit layer cache is reused. Unrelated Docker images and unrelated build
# cache on the machine are untouched.

# Removes every HERD-tagged Docker image (dev and gate compose projects) and
# prunes dangling layers, so the next `make up` or `make build` rebuilds from
# scratch. Internal helper for master-clean; unrelated images on the host are
# left untouched.
_clean-images:
	@echo ""
	@echo "=== Removing HERD compose images ==="
	-docker compose --profile e2e down --remove-orphans
	-$(GATE_COMPOSE) --profile e2e down -v --remove-orphans
	@ids=$$(docker images --filter "reference=herd-*" --filter "reference=$(GATE_PROJECT)-*" -q | sort -u); \
		if [ -n "$$ids" ]; then \
			echo "$$ids" | xargs -r docker rmi -f; \
		else \
			echo "No HERD images to remove."; \
		fi
	@echo ""
	@echo "=== Pruning dangling images ==="
	-docker image prune -f

master-clean: _clean-images  ## master with all HERD compose images rebuilt --no-cache
	@BUILD_NO_CACHE=1 $(MAKE) master

# -- Everything (full prod-grade gate: master + coverage + stress + fail-on-dirty) --
#
# Differences from `master`:
#   - `ruff format --check` instead of `ruff format` (no source mutation).
#   - Runs backend and frontend tests under coverage (master skips coverage).
#   - After seeding the gate stack, re-runs e2e via test-e2e-seeded (issue #629):
#     the first e2e pass above runs against an unseeded stack, so tests gated on
#     an available device always skip there; this second pass runs the same
#     suite against the now-seeded stack and fails the phase on any skip.
#   - Adds headless locust stress test after e2e.
#   - Runs the NOS lab dialect tier (nos-test-dialect) after the live LDAP
#     phase and before frontend coverage; `master` does not run it.
#   - On SUCCESS the gate stack is left RUNNING and seeded (a live,
#     freshly-validated stack at https://localhost); `make gate-down` stops
#     it, and the next gate run's gate-clean purges it anyway, so every gate
#     is still born fresh. On failure the stack is torn down as before.
# Everything stops on first failure; failure teardown is trapped.

# Set EVERYTHING_LOAD=0 (or use everything-noload) to skip the locust load
# test. The seed still runs (the stack left behind should be usable either
# way); only the load-test tail is skipped.
EVERYTHING_LOAD ?= 1

everything: gate-clean  ## Closest-to-CI gate: master + coverage + format-check + load tests
	@echo ""
	@echo "=== Installing Python dependencies ==="
	uv sync --all-extras
	@echo ""
	@echo "=== Installing frontend dependencies ==="
	cd frontend && npm ci
	@echo ""
	@echo "=== Checking Python formatting (fail on dirty) ==="
	uv run ruff format --check services/ $(ROOT_PY)
	@echo ""
	@echo "=== Linting Python ==="
	uv run ruff check services/ $(ROOT_PY)
	@echo ""
	@echo "=== Linting frontend ==="
	cd frontend && npx eslint src --max-warnings 0
	@echo ""
	@echo "=== Backend tests with coverage ($(words $(SERVICES)) services) ==="
	$(MAKE) coverage
	@echo ""
	@echo "=== Live LDAP auth tests ==="
	$(MAKE) _gate-ldap-tests
	@echo ""
	@echo "=== NOS lab dialect tests (real SR Linux and FRR, no stack) ==="
	$(MAKE) nos-test-dialect
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
	@ok=0; trap 'if [ "$$ok" != 1 ]; then $(MAKE) _master-stack-down; fi' EXIT INT TERM; \
		$(MAKE) _master-stack-up && \
		$(MAKE) _master-wait-healthy && \
		echo "" && echo "=== Contract tests ===" && \
		$(MAKE) test-contract && \
		echo "" && echo "=== Integration tests ===" && \
		$(MAKE) test-integration COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		echo "" && echo "=== E2E tests ===" && \
		$(MAKE) test-e2e COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		echo "" && echo "=== LDAP-mode stack tests (gate auth switched to LDAP) ===" && \
		$(MAKE) _gate-ldap-stack-tests COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		echo "" && echo "=== Postgres-live LDAP sync tests ===" && \
		$(MAKE) _gate-pg-live-tests && \
		echo "" && echo "=== Seeding gate stack ===" && \
		$(MAKE) _everything-seed && \
		echo "" && echo "=== E2E tests (seeded, no skips allowed) ===" && \
		$(MAKE) test-e2e-seeded COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		if [ "$(EVERYTHING_LOAD)" != "0" ]; then \
			echo "" && echo "=== Load / stress tests ===" && \
			HERD_BASE_URL=https://localhost $(MAKE) test-load; \
		else \
			echo "" && echo "=== Skipping load tests (EVERYTHING_LOAD=0) ==="; \
		fi && \
		$(MAKE) test-e2e-stop COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \
		ok=1
	@echo ""
	@echo "=== everything complete: gate stack left running and seeded at https://localhost ==="
	@echo "    make gate-down   stop it (then 'make up' restores the dev stack)"

everything-noload:  ## everything minus the load-test tail (stack still seeded and left up)
	$(MAKE) everything EVERYTHING_LOAD=0

# Seeds the running stack via the seedtools package (issue #791): users, drivers,
# templates, devices, ports, L1/L2 switches, cabling, device/user groups, 6
# isolated demo devices, and 60 demo lab topologies (50 valid + 10 deliberately
# invalid). Every subcommand is re-runnable and skips resources that already
# exist.
#
# Credential resolution lives in seedtools/client.py, in one place for every
# subcommand (highest priority first): SEED_EMAIL / SEED_PASSWORD, then
# SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD from the environment, then the same
# SUPERADMIN_* keys read from .env (parsed line by line, not sourced, so an
# unquoted placeholder value elsewhere in .env cannot break the run), then a
# generic admin@example.com placeholder. A missing .env is not an error: a stack
# bootstrapped with default creds still seeds.
#
# SEED_FRR=1 and SEED_NOS=1 keep their meaning here and layer the FRR
# live-config demo and the NOS test lab onto the full population; `make seed-frr`
# and `make seed-nos` stage just those pieces.
_everything-seed:
	@if [ ! -f .env ]; then \
		echo "No .env found; relying on shell SEED_*/SUPERADMIN_* or the seedtools defaults."; \
	fi
	uv run python -m seedtools full

# Public alias so `make seed` works against an already-running stack.
seed: _everything-seed  ## Seed a running stack with demo users, devices, cabling, and topologies

# The two lab demos on their own, against an already-seeded stack: what the
# retired scripts/seed_frr_demo.sh and scripts/seed_nos_lab.sh wrapped. Both are
# idempotent; `python -m seedtools frr --full` / `nos --full` layer the demo onto
# a full population instead.
seed-frr:  ## Seed a running stack with the FRR live-config demo (the two slice1 lab routers)
	uv run python -m seedtools frr

seed-nos:  ## Seed a running stack with the NOS test lab (real SR Linux + FRR lab nodes)
	uv run python -m seedtools nos

_master-stack-up:
	$(GATE_COMPOSE) up -d --build

_master-wait-healthy:
	@echo "Waiting for stack to report healthy..."
	@TIMEOUT=180; START=$$(date +%s); ELAPSED=0; \
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
	@echo "=== Tearing down ephemeral gate stack ==="
	-$(MAKE) test-e2e-stop COMPOSE_PROJECT_NAME=$(GATE_PROJECT)
	-$(GATE_COMPOSE) --profile e2e down -v --remove-orphans

# Public name for stopping a gate stack that a successful `make everything`
# left running (master always tears its stack down itself). After gate-down,
# `make up` restores the dev stack with its data.
gate-down: _master-stack-down  ## Stop and purge the gate stack an `everything` run left up

## --- Docker Compose ---

# -- Docker Compose -----------------------------------------------------------

up: dev  ## Start the full stack in dev mode (hot reload), alias of dev

dev:  ## Start the full stack in dev mode (build + detached)
	docker compose up --build -d

prod:  ## Start the stack without dev overrides (no reload)
	docker compose -f docker-compose.yml up --build -d

# Every compose `down` below carries --profile e2e: a profile-gated service that is
# not enabled is neither an active service nor an orphan, so a plain `down` (even
# with --remove-orphans) leaves the e2e Selenium container running. Left behind
# by the dev project it holds host port 4444 and the gate's e2e phase then fails
# at Selenium recreate ("Bind for 0.0.0.0:4444 failed") before any test runs.
down:  ## Stop the stack (including the e2e Selenium container if it is up)
	docker compose --profile e2e down

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

# Repo-root unit tests (tests/unit/) cover repo-root code like the seedtools
# package. They need no stack and are not under services/, so
# the per-service loops never reach them; both `test` and `coverage` depend on
# this target so they cannot be silently skipped locally or in CI.
test-root:  ## Run the repo-root unit tests (tests/unit, no stack needed)
	uv run pytest tests/unit/ -v

test: test-root  ## Run every backend service's unit suite (plus tests/unit)
	@for svc in $(SERVICES); do \
		echo ">>> test $$svc"; \
		(cd services/$$svc && uv run pytest tests/ -v) || exit $$?; \
	done

# The generated per-service pattern must not emit test-integration: that name
# belongs to the cross-service suite below, and make would let the later
# explicit recipe silently shadow the generated one (it did, until 2026-08-10;
# `make test-integration` could never reach the integration service's unit
# suite). The service's own suite gets an unambiguous name instead.
$(addprefix test-,$(filter-out integration,$(SERVICES))):
	cd services/$(@:test-%=%) && uv run pytest tests/ -v

test-integration-service:  ## Run the integration service's unit suite (test-<svc> for the others)
	cd services/integration && uv run pytest tests/ -v

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

# Shared body for test-e2e and test-e2e-seeded (issue #629), so the two
# cannot drift apart. HERD_E2E_REQUIRE_NO_SKIP is whatever the caller's
# environment/command line already has: test-e2e leaves it unset, test-e2e-seeded
# sets it to 1 before invoking this via a recursive $(MAKE).
# The Selenium recreate uses --wait, which blocks until the healthcheck in
# docker-compose.override.yml passes. Without it pytest starts within seconds of
# the container start, and the host-side Chromium can observe the new veth's
# address churn mid-load: net::ERR_NETWORK_CHANGED on the app bundle, an empty
# root element, and a 30s login timeout on the first Playwright test.
# --wait-timeout bounds that wait (issue #783): without it a wedged Grid
# healthcheck blocks here indefinitely with no diagnostic; compose fails
# with a clear "container ... did not become healthy" message instead.
_test-e2e-run:
	-docker compose --profile e2e rm -fsv selenium
	docker compose --profile e2e up -d --force-recreate --wait --wait-timeout 120 selenium
	uv run playwright install chromium  # no-op once cached; on a fresh host, missing OS libs need: uv run playwright install --with-deps chromium (sudo)
	@uv run python -c "import os, tempfile; print('e2e failure artifacts (if any) go to: ' + (os.environ.get('HERD_E2E_ARTIFACT_DIR') or os.path.join(tempfile.gettempdir(), 'herd-e2e-artifacts')))"
	uv run pytest tests/e2e/ -v --tb=short

test-e2e: _test-e2e-run  ## Run e2e tests, Selenium + Playwright (needs a running stack)

test-e2e-seeded:  ## Run e2e tests against a seeded stack; fails if any test skips (issue #629)
	HERD_E2E_REQUIRE_NO_SKIP=1 $(MAKE) _test-e2e-run

test-e2e-stop:  ## Stop and remove the e2e Selenium container
	-docker compose --profile e2e rm -fsv selenium

# Failure diagnostics collector (issue #648), shared by ci.yml's integration
# job and nightly.yml's full-stack job so the collection logic lives once and
# the two cannot drift apart. DIAG_DIR is where the files land (a caller sets
# it to an absolute or workflow-relative path; default is the cwd). Every
# collector line ends in `|| true`: the default Actions shell is `bash -e`,
# so a wedged daemon failing the first command would otherwise skip every
# collector after it, and each destination is created by its own redirect
# regardless. `docker compose ps -a` (not the bare form) is deliberate: an
# exited/crashed container is the state this step most needs to catch, and
# the bare form omits it. Per-service log files come from the same `-a`
# services listing so a container that already exited still gets its own
# file. This target must not depend on why the caller failed; it only reads
# current Docker/compose state.
DIAG_DIR ?= .
_collect-stack-diagnostics:
	mkdir -p $(DIAG_DIR)
	docker compose ps -a > $(DIAG_DIR)/compose-ps.txt || true
	docker ps -a > $(DIAG_DIR)/docker-ps-a.txt || true
	docker compose logs --no-color --timestamps > $(DIAG_DIR)/compose-logs.txt || true
	for svc in $$(docker compose ps --services -a 2>/dev/null); do \
		docker compose logs --no-color --timestamps "$$svc" > "$(DIAG_DIR)/logs-$$svc.txt" || true; \
	done

# -- LDAP test server (infra/ldap-test) ---------------------------------------
#
# A checked-in osixia/openldap compose file under infra/ldap-test seeds the
# exact fixtures the live-LDAP auth tests assert (user1..user25, Password1,
# dc=company,dc=local), plus dedicated ldapit-* identities for the
# stack-level integration tests (70-seed-integration.ldif, issue #572). It is
# stateless: down discards the directory, up reseeds it from the bundled
# LDIF. The compose healthcheck, bound as cn=admin, searches for
# cn=herd-it-eng (the last entry of the last LDIF file), so `up --wait`
# returns only once the seed is actually fully queryable, not merely once
# slapd answers.
#
# The master and everything gates run these tests through _gate-ldap-tests,
# which hard-requires the server (HERD_TEST_LDAP_REQUIRED=1 turns the
# unreachable-server skip into a failure) and stops it afterward only if the
# gate started it; a server you started yourself is left running.
#
# _gate-ldap-tests only proves the directory itself answers correctly
# (services/auth/tests/test_ldap_service_live.py, no HERD stack involved). Two
# later, sibling phases close the rest of the gap (issue #572), both run
# after test-e2e in both master and everything:
#
#   _gate-ldap-stack-tests proves the STACK can actually authenticate
#   against the directory: the ephemeral gate stack always boots with
#   AUTH_METHOD=local, so tests/integration/test_ldap_auth.py and the new
#   tests/integration/test_ldap_sync_admin.py were never exercised by any
#   gate before this. It connects ldap-test onto the gate project's network,
#   recreates ONLY the gate's auth service in LDAP mode (--no-deps
#   --force-recreate, so postgres/nats/etc keep running), runs those two
#   integration files, then ALWAYS restores auth to local mode and
#   disconnects the network before any later phase (seeding, load tests)
#   runs, so master/everything still end on a local-auth stack. Its recipe
#   deliberately never calls $(MAKE) (see the target's own comment) so
#   `make -n` stays a genuine dry run.
#
#   _gate-pg-live-tests runs right after it: the Postgres-live LDAP sync
#   suites (services/auth/tests/test_ldap_sync_service_live_pg.py and
#   services/common/tests/test_advisory_lock_live_pg.py, hard-required via
#   HERD_TEST_PG_REQUIRED=1) against the gate stack's own Postgres. It needs
#   no LDAP mode, so it is a separate step rather than nested inside
#   _gate-ldap-stack-tests.

# Pinned with -p (not left to the compose file's own `name: herd-ldap-test`)
# after an incident on 2026-08-26: COMPOSE_PROJECT_NAME is exported into
# _gate-ldap-stack-tests' recipe environment (that is how the gate targets
# pass it along), and a COMPOSE_PROJECT_NAME env var outranks a compose
# file's `name:` attribute in project-name resolution, so every bare
# $(LDAP_COMPOSE) call was silently resolving to the GATE project instead of
# herd-ldap-test. A `down -v --remove-orphans` run that way treats every
# gate-stack container as an orphan of a project whose compose file only
# defines one service, ldap, and removes them all: it turned a routine
# LDAP-test-server teardown into a full gate-stack teardown. -p is the only
# project-name source docker compose never lets an environment variable
# override, so it is pinned explicitly here even though the file also
# carries the same name via `name:` (kept for standalone `docker compose`
# invocations against this file with no -p, e.g. a developer running it by
# hand from infra/ldap-test/).
LDAP_COMPOSE := docker compose -p herd-ldap-test -f infra/ldap-test/docker-compose.yml
HERD_TEST_LDAP_HOST ?= 127.0.0.1
HERD_TEST_LDAP_PORT ?= 389

ldap-up:  ## Start the checked-in LDAP test server (infra/ldap-test), wait until seeded
	HERD_TEST_LDAP_PORT=$(HERD_TEST_LDAP_PORT) $(LDAP_COMPOSE) up -d --wait
	@echo "LDAP test server up on ldap://$(HERD_TEST_LDAP_HOST):$(HERD_TEST_LDAP_PORT)"

# --remove-orphans is safe here (unlike the calls _gate-ldap-stack-tests used
# to make before 2026-08-26): $(LDAP_COMPOSE) is pinned to project
# herd-ldap-test via -p, exclusively owned by this one compose file, so an
# orphan can only ever be a leftover container from a past version of that
# file's own service list, never another project's containers.
ldap-down:  ## Stop and remove the LDAP test server
	$(LDAP_COMPOSE) down -v --remove-orphans

ldap-status:
	$(LDAP_COMPOSE) ps

ldap-logs:
	$(LDAP_COMPOSE) logs -f

ldap-reset:
	$(LDAP_COMPOSE) down -v --remove-orphans
	$(MAKE) ldap-up

# Run only the live-LDAP auth tests, hard-required: asking for them explicitly
# and getting 10 skips because the server is down would be a silent no-op, so
# this fails loudly instead. `make ldap-up test-auth-ldap` starts the server
# first.
test-auth-ldap:  ## Run the live-LDAP auth tests (no skips; needs ldap-up)
	cd services/auth && HERD_TEST_LDAP_HOST=$(HERD_TEST_LDAP_HOST) HERD_TEST_LDAP_PORT=$(HERD_TEST_LDAP_PORT) \
		HERD_TEST_LDAP_REQUIRED=1 uv run pytest tests/test_ldap_service_live.py -v

# Gate phase used by master and everything: boot the server if (and only if)
# it is not already running, run the hard-required tests, then tear down only
# what this run started. The trap covers the failure path too, so a red test
# run cannot strand a gate-started container.
_gate-ldap-tests:
	@started=0; \
	if ! docker ps --format '{{.Names}}' | grep -q '^ldap-test$$'; then \
		$(MAKE) ldap-up; started=1; \
	else \
		echo "LDAP test server already running; leaving it up afterward."; \
	fi; \
	trap 'if [ "$$started" = 1 ]; then $(MAKE) ldap-down; fi' EXIT INT TERM; \
	$(MAKE) test-auth-ldap

# Checked-in emulated-gear test lab (infra/nos-test, docs/NOS_LAB.md): phase 0
# of the emulated-gear test tier (ADR 0010). License-free, needs no external
# Proxmox host, unlike network-simulator. Deliberately opt-in: NOT wired into
# `make test`, `make master`, or `make everything`, so it costs nothing on a
# host that never runs it. Pinned with -p (own compose project
# herd-nos-test, own network herd-nos-test-net) for the same reason
# LDAP_COMPOSE is pinned: an exported COMPOSE_PROJECT_NAME must never be able
# to redirect a `down` on this file onto another project's containers.
NOS_COMPOSE := docker compose -p herd-nos-test -f infra/nos-test/docker-compose.yml
HERD_TEST_SRL_HOST ?= 127.0.0.1
HERD_TEST_FRR_HOST ?= 127.0.0.1
HERD_TEST_SRL_PORT ?= 2223
HERD_TEST_FRR_PORT ?= 2224

nos-up:  ## Start the checked-in NOS test lab (infra/nos-test), wait until healthy
	HERD_TEST_SRL_PORT=$(HERD_TEST_SRL_PORT) HERD_TEST_FRR_PORT=$(HERD_TEST_FRR_PORT) \
		$(NOS_COMPOSE) up -d --build --wait
	@echo "NOS test lab up: SR Linux ssh://$(HERD_TEST_SRL_HOST):$(HERD_TEST_SRL_PORT), FRR ssh://$(HERD_TEST_FRR_HOST):$(HERD_TEST_FRR_PORT)"

nos-down:  ## Stop and remove the NOS test lab
	$(NOS_COMPOSE) down -v

nos-status:  ## Show the NOS test lab's container status
	$(NOS_COMPOSE) ps

nos-logs:  ## Tail the NOS test lab's logs
	$(NOS_COMPOSE) logs -f

nos-reset:  ## Recreate the NOS test lab from scratch (discards all node state)
	$(NOS_COMPOSE) down -v
	$(MAKE) nos-up

# Attach/detach the NOS test lab containers to the DEV stack's Docker network,
# so HERD's own execution service can reach them by CONTAINER NAME over
# Docker DNS (proven live 2026-09-11: netmiko from inside the execution
# container reached both nos-test-srl and nos-test-frr by name once
# attached). Container IPs are not stable across a recreate; container names
# are, which is why devices are seeded with field_data.ip set to the
# container name (see seed_nos_lab in seedtools/nos_lab.py), not an IP.
#
# Mirrors _gate-ldap-stack-tests' connect/disconnect trap: `docker compose
# down` on a network that still holds an attached container prints "Resource
# is still in use" but still exits 0 (verified live), so leaving a lab
# container attached never breaks `make clean`/`make down`. It DOES leave the
# network behind, so nos-detach must always be run to undo nos-attach.
#
# Scoped to the DEV stack only (never the gate stack): an explicit
# COMPOSE_PROJECT_NAME always wins, else DEV_PROJECT (CURDIR-derived) is
# used, the same override _gate-ldap-stack-tests supports, needed because a
# worktree's own CURDIR-derived name does not match the dev stack's project
# when the dev stack was booted from a different checkout directory.
NOS_LAB_CONTAINERS := nos-test-srl nos-test-frr

nos-attach:  ## Attach the NOS test lab containers to the dev stack's network (make up first)
	@net=$${COMPOSE_PROJECT_NAME:-$(DEV_PROJECT)}_herd-net; \
	if ! docker network inspect "$$net" >/dev/null 2>&1; then \
		echo "Dev stack network $$net not found; run 'make up' first (or set COMPOSE_PROJECT_NAME)."; \
		exit 1; \
	fi; \
	for c in $(NOS_LAB_CONTAINERS); do \
		if ! docker ps --format '{{.Names}}' | grep -qx "$$c"; then \
			echo "$$c is not running; run 'make nos-up' first."; \
			exit 1; \
		fi; \
		out=$$(docker network connect "$$net" "$$c" 2>&1); rc=$$?; \
		if [ $$rc -ne 0 ] && ! echo "$$out" | grep -qi already; then \
			echo "$$out"; \
			echo "Failed to attach $$c to $$net"; \
			exit 1; \
		fi; \
		echo "Attached $$c to $$net"; \
	done

nos-detach:  ## Detach the NOS test lab containers from the dev stack's network
	@net=$${COMPOSE_PROJECT_NAME:-$(DEV_PROJECT)}_herd-net; \
	echo "Target network: $$net"; \
	fail=0; \
	for c in $(NOS_LAB_CONTAINERS); do \
		if ! docker inspect "$$c" >/dev/null 2>&1; then \
			echo "$$c: container not found, nothing to detach"; \
			continue; \
		fi; \
		netjson=$$(docker inspect "$$c" --format '{{json .NetworkSettings.Networks}}' 2>/dev/null); \
		case "$$netjson" in \
			*"\"$$net\":"*) attached=yes ;; \
			*) attached=no ;; \
		esac; \
		if [ "$$attached" = no ]; then \
			echo "$$c: not attached to $$net"; \
			continue; \
		fi; \
		out=$$(docker network disconnect "$$net" "$$c" 2>&1); rc=$$?; \
		if [ $$rc -ne 0 ]; then \
			echo "$$out"; \
			echo "$$c: failed to detach from $$net"; \
			fail=1; \
		else \
			echo "$$c: detached from $$net"; \
		fi; \
	done; \
	exit $$fail

# NOS lab test tiers (issue #785, decided 2026-09-12). Every file under
# tests/nos_lab/ belongs to exactly one of the two lists below, and
# tests/unit/test_nos_lab_ci_wiring.py fails if a new file belongs to
# neither, so a new suite cannot quietly go unrun.
#
# - DIALECT suites drive ONE driver against ONE lab node over SSH. No HERD
#   stack is involved, so they are cheap enough to run on every PR
#   (.github/workflows/ci.yml, job nos-dialect).
# - FEATURE suites drive a real device through HERD's own API (reservation,
#   fork save, execution service), so they need a booted and seeded stack
#   with the lab attached to its network. Those run in nightly.yml, after
#   the seed step.
#
# Both workflows call these targets rather than spelling out a pytest
# invocation, so a workflow and a local run cannot drift apart. Neither
# target is wired into `make master` or `make everything`: keeping the lab
# opt-in there is a separate decision (docs/NOS_LAB.md).
NOS_DIALECT_TESTS := \
	tests/nos_lab/test_nos_lab_live.py \
	tests/nos_lab/test_frr_l3_driver_live.py \
	tests/nos_lab/test_frr_mgmt_driver_live.py \
	tests/nos_lab/test_srl_l2_driver_live.py

NOS_FEATURE_TESTS := \
	tests/nos_lab/test_frr_l3_via_stack_live.py \
	tests/nos_lab/test_srl_l2_via_stack_live.py

# Recursion alias. GNU make executes a recipe line that literally contains
# `$(MAKE)` even under `-n`, which for the two targets below would mean a
# `make -n` boots containers and runs a multi-minute live suite for real
# (that is why `make -n _gate-ldap-tests` is not a dry run today). Going
# through an alias hides the literal from that check, so `make -n` prints
# and stops, while a real run recurses exactly as before. The one thing the
# alias gives up is jobserver propagation under `-j`; neither target is ever
# run in parallel.
SUBMAKE := $(MAKE)

# Boot the lab if (and only if) it is not already up, run the dialect
# suites hard-required (HERD_TEST_NOS_REQUIRED=1 turns "lab unreachable"
# into a failure instead of the suites' normal skip, so a CI job asking for
# them can never silently no-op), then tear down only what this run started.
# The trap covers the failure path, so a red run cannot strand a
# target-started lab. Same shape as _gate-ldap-tests above.
nos-test-dialect:  ## Run the NOS lab dialect suites (boots the lab if it is not already up)
	@started=0; running=1; \
	for c in $(NOS_LAB_CONTAINERS); do \
		docker ps --format '{{.Names}}' | grep -qx "$$c" || running=0; \
	done; \
	if [ "$$running" = 1 ]; then \
		echo "NOS test lab already running; leaving it up afterward."; \
	else \
		$(SUBMAKE) nos-up; started=1; \
	fi; \
	trap 'if [ "$$started" = 1 ]; then $(SUBMAKE) nos-down; fi' EXIT INT TERM; \
	HERD_TEST_NOS_REQUIRED=1 uv run pytest $(NOS_DIALECT_TESTS) -v

# The via-stack half. Unlike nos-test-dialect this one does NOT boot
# anything by itself beyond the attachment: it needs a running HERD stack
# (`make up`, or nightly's `docker compose up -d`) and a running lab
# (`make nos-up`), and fails fast with nos-attach's own wording when the
# stack network is missing, rather than seeding into nothing. The detach
# trap is armed BEFORE the attach so a failure anywhere, including inside
# nos-attach's own loop, still leaves the lab off the stack's network.
# COMPOSE_PROJECT_NAME selects which stack: it flows through to
# nos-attach/nos-detach, which is why nightly calls this target with
# COMPOSE_PROJECT_NAME=herd (its stack is the default-named project, not a
# CURDIR-derived one).
#
# Credentials: the two suites authenticate against the stack as its
# superadmin and read SEED_EMAIL/SEED_PASSWORD, else
# SUPERADMIN_EMAIL/SUPERADMIN_PASSWORD, from the ENVIRONMENT (not from
# .env, which only the stack itself reads), so export them before calling
# this target. Both suites probe the credentials up front and say so
# instead of failing inside a test.
#
# Deliberately NOT wired into master or everything (nightly only, via
# nightly.yml): it needs a running HERD stack plus the lab, and the two
# needs together are a heavier local ask than the dialect tier below carries.
nos-test-feature:  ## Run the NOS lab via-stack feature suites (needs a running stack and lab)
	@net=$${COMPOSE_PROJECT_NAME:-$(DEV_PROJECT)}_herd-net; \
	if ! docker network inspect "$$net" >/dev/null 2>&1; then \
		echo "Dev stack network $$net not found; run 'make up' first (or set COMPOSE_PROJECT_NAME)."; \
		exit 1; \
	fi; \
	trap '$(SUBMAKE) nos-detach' EXIT INT TERM; \
	$(SUBMAKE) nos-attach && \
	$(SUBMAKE) seed-nos && \
	HERD_TEST_NOS_REQUIRED=1 uv run pytest $(NOS_FEATURE_TESTS) -v

# Postgres-live coverage for the ADR 0011 sync surface (issue #572): the
# advisory-lock SQL and _SyncSlot's cross-replica branch never run on the
# SQLite engine every other sync test uses. Any reachable Postgres works
# (these tests create no application schema), so this target just needs a
# DSN: it is built from .env's POSTGRES_USER/PASSWORD/DB plus the gate
# stack's published Postgres host port (POSTGRES_PORT, default 5433, the
# same default docker-compose.yml's postgres service publishes on), NOT a
# COMPOSE_PROJECT_NAME lookup, since the host port is fixed regardless of
# compose project and gate-clean already guarantees the dev stack is stopped
# (so port 5433 unambiguously belongs to the gate's postgres) before either
# gate boots. HERD_TEST_PG_REQUIRED=1 turns "Postgres unreachable" into a
# hard failure instead of the tests' normal skip, matching _gate-ldap-tests'
# HERD_TEST_LDAP_REQUIRED=1 discipline. The cabling suites added for issue #626
# (the fork row lock) and issue #721 (the fork port-claim lock) are the
# exception to "any reachable Postgres works": they exercise the real
# cabling.reservation_fork/fork_versions/fork_connections/connections tables, so
# they need the gate's ALREADY-MIGRATED schema, not a throwaway server; each
# creates a few throwaway rows per test (random ids) and deletes them in a
# finally, so they leave the gate's seeded data untouched.
_gate-pg-live-tests:
	@pguser=$$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | head -1 | cut -d= -f2-); \
	pgpass=$$(grep -E '^POSTGRES_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2-); \
	pgdb=$$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | head -1 | cut -d= -f2-); \
	pgport=$$(grep -E '^POSTGRES_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2-); \
	pgport=$${pgport:-5433}; \
	dsn="postgresql+asyncpg://$${pguser:-herd}:$${pgpass:-herd}@127.0.0.1:$${pgport}/$${pgdb:-herd}"; \
	echo "Postgres-live LDAP sync tests against 127.0.0.1:$${pgport}/$${pgdb:-herd}"; \
	(cd services/auth && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \
		uv run pytest tests/test_ldap_sync_service_live_pg.py -v) && \
	(cd services/common && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \
		uv run pytest tests/test_advisory_lock_live_pg.py -v) && \
	(cd services/common && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \
		uv run pytest tests/test_outbox_wake_live_pg.py -v) && \
	(cd services/cabling && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \
		uv run pytest tests/test_fork_restore_save_race_live_pg.py -v) && \
	(cd services/cabling && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \
		uv run pytest tests/test_fork_port_claim_race_live_pg.py -v) && \
	(cd services/cabling && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \
		uv run pytest tests/test_l3_route_key_width_live_pg.py -v)

# Gate phase used by master and everything, run after test-e2e (issue #572):
# proves the STACK, not just the directory, can authenticate against LDAP.
# Ensures infra/ldap-test is running (started-here-so-torn-down-here, same
# pattern as _gate-ldap-tests, and self-sufficient: this phase never assumes
# _gate-ldap-tests left the server up), connects it onto the gate compose
# project's network so the gate's auth container can resolve it by name, then
# recreates ONLY the auth service (--no-deps, so postgres/nats/other services
# keep running undisturbed) with AUTH_METHOD=ldap and the matching LDAP_*
# settings for infra/ldap-test's fixtures (see infra/ldap-test/ldif). Shell
# env beats the .env file in compose interpolation, so this override is
# scoped to this one `docker compose up` call and never touches the repo's
# .env. The restore step (trap, so a failing test still runs it) recreates
# auth back to its default (local) env, waits for it to report healthy again,
# disconnects the network, and tears down ldap-test only if this phase
# started it, so master/everything end on a local-auth stack exactly as
# before this phase existed.
# Deliberately calls neither `$(MAKE) ldap-up` nor `$(MAKE) ldap-down` (unlike
# _gate-ldap-tests above): a recipe line containing a literal $(MAKE) reference
# is executed for real even under `make -n` (GNU Make's documented escape
# hatch for accurate recursive dry-run reporting), and this recipe already
# mixes in real docker network/compose mutation, so keeping it $(MAKE)-free
# keeps `make -n master` / `make -n everything` (and `make -n
# _gate-ldap-stack-tests` on its own) genuine dry runs that print without
# touching any container. It inlines the same $(LDAP_COMPOSE) commands
# ldap-up/ldap-down wrap instead.
#
# Also deliberately uses BARE `docker compose` throughout (never
# $(GATE_COMPOSE)'s explicit -p), so this target works against whichever
# compose project COMPOSE_PROJECT_NAME names, not only the master/everything
# gate project: master/everything pass COMPOSE_PROJECT_NAME=$(GATE_PROJECT)
# (see the "make exports command-line variables to recipe environments" note
# near GATE_PROJECT's definition), while nightly.yml's plain `docker compose
# up -d` stack has no such override, so this target resolves the SAME default
# project docker compose itself would (falls back to $(GATE_PROJECT) only
# when COMPOSE_PROJECT_NAME is entirely unset, e.g. a standalone local run).
_gate-ldap-stack-tests:
	@net=$${COMPOSE_PROJECT_NAME:-$(GATE_PROJECT)}_herd-net; \
	auth_cid=$$(docker compose ps -q auth 2>/dev/null); \
	if [ -z "$$auth_cid" ]; then \
		echo "No running auth container for project $${COMPOSE_PROJECT_NAME:-$(GATE_PROJECT)} (docker compose ps -q auth returned nothing)."; \
		echo "This target recreates an ALREADY-RUNNING gate stack's auth service; it does not start one."; \
		echo "Bring the target stack up first (e.g. make _master-stack-up / make up), or check COMPOSE_PROJECT_NAME."; \
		exit 1; \
	fi; \
	config_cid=$$(docker compose ps -q config 2>/dev/null); \
	if [ -n "$$config_cid" ] && \
		docker compose exec -T config test -f /data/herd-config/config.json 2>/dev/null && \
		! docker compose exec -T config test -f /data/herd-config/config.bootstrapped 2>/dev/null; then \
		echo "This stack's config.json was saved through the config UI (no config.bootstrapped marker)."; \
		echo "Per herd_common.config_loader precedence it now outranks environment variables, so this target's AUTH_METHOD=ldap / LDAP_* overrides would be silently ignored."; \
		echo "Restore config.bootstrapped, or clear the relevant keys via the config UI, then retry."; \
		exit 1; \
	fi; \
	ldap_started=0; \
	if ! docker ps --format '{{.Names}}' | grep -q '^ldap-test$$'; then \
		echo "Starting infra/ldap-test (LDAP test server)..."; \
		HERD_TEST_LDAP_PORT=$(HERD_TEST_LDAP_PORT) $(LDAP_COMPOSE) up -d --wait; \
		ldap_started=1; \
	else \
		echo "LDAP test server already running; leaving it up afterward."; \
	fi; \
	connect_out=$$(docker network connect "$$net" ldap-test 2>&1); \
	rc=$$?; \
	if [ $$rc -ne 0 ] && ! echo "$$connect_out" | grep -qi "already"; then \
		echo "$$connect_out"; \
		echo "Failed to connect ldap-test to $$net"; \
		if [ "$$ldap_started" = 1 ]; then $(LDAP_COMPOSE) down -v; fi; \
		exit 1; \
	fi; \
	wait_auth_healthy() { \
		to=$$1; s=$$(date +%s); \
		cid=$$(docker compose ps -q auth); \
		until [ "$$(docker inspect -f '{{.State.Health.Status}}' "$$cid" 2>/dev/null)" = "healthy" ]; do \
			n=$$(date +%s); \
			if [ $$((n - s)) -gt $$to ]; then \
				echo "Timed out after $${to}s waiting for gate auth to report healthy"; \
				return 1; \
			fi; \
			sleep 2; \
		done; \
		echo "Gate auth healthy after $$(( $$(date +%s) - s ))s"; \
	}; \
	restore() { \
		rc=$$?; \
		echo ""; \
		echo "=== Restoring gate auth to local mode ==="; \
		docker compose up -d --no-deps --force-recreate auth; \
		wait_auth_healthy 90 || true; \
		docker network disconnect "$$net" ldap-test 2>/dev/null || true; \
		if [ "$$ldap_started" = 1 ]; then $(LDAP_COMPOSE) down -v; fi; \
		exit $$rc; \
	}; \
	trap restore EXIT INT TERM; \
	echo "" && echo "=== Recreating gate auth in LDAP mode ===" && \
	AUTH_METHOD=ldap \
		LDAP_SERVER_URL=ldap://ldap-test:389 \
		LDAP_BIND_DN=cn=admin,dc=company,dc=local \
		LDAP_BIND_PASSWORD=admin \
		LDAP_USER_BASE_DN=ou=people,dc=company,dc=local \
		LDAP_USER_FILTER='(uid={username})' \
		LDAP_USERNAME_ATTRIBUTE=uid \
		LDAP_EMAIL_ATTRIBUTE=mail \
		LDAP_USE_TLS=false \
		LDAP_GROUP_MEMBER_ATTRIBUTE=member \
		LDAP_GROUP_NAME_ATTRIBUTE=cn \
		docker compose up -d --no-deps --force-recreate auth && \
	wait_auth_healthy 90 && \
	echo "" && echo "=== LDAP integration tests against the gate stack ===" && \
	HERD_INTEGRATION_LDAP=1 \
		HERD_TEST_LDAP_HOST=$(HERD_TEST_LDAP_HOST) \
		HERD_TEST_LDAP_PORT=$(HERD_TEST_LDAP_PORT) \
		uv run pytest tests/integration/test_ldap_auth.py tests/integration/test_ldap_sync_admin.py \
			-v --timeout=60

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

clean:  ## Stop the stack (KEEPING volumes/data) and remove caches/coverage artifacts
	docker compose --profile e2e down --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name coverage.xml -delete 2>/dev/null || true
	find . -type f -name .coverage -delete 2>/dev/null || true

# Pre-gate cleanup for master/everything. Both compose projects publish the same
# host ports, so the dev stack must be STOPPED before the gate stack can boot;
# stopping without -v is what preserves the dev volumes (data and seed) across a
# gate run. The gate project itself is purged WITH volumes so every gate is born
# on a fresh database.
gate-clean: clean  ## Stop the dev stack (data kept), purge any stale gate-project stack
	$(GATE_COMPOSE) --profile e2e down -v --remove-orphans

clean-data:  ## DESTRUCTIVE: tear down the dev stack INCLUDING volumes (the pre-gate-isolation `make clean`)
	docker compose --profile e2e down -v --remove-orphans
