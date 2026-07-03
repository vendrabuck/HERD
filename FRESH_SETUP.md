# HERD Fresh System Setup

Complete guide for building HERD from scratch on a new machine.

## Prerequisites

- Docker and Docker Compose (v2+)
- Git
- Node.js 22+ and npm (for local frontend dev/testing)
- Python 3.12+ and uv (for local backend dev/testing)
- openssl (only if regenerating TLS certs)

## 1. Clone the repository

```bash
git clone <repo-url> HERD
cd HERD
```

## 2. Create the environment file

```bash
cp .env.example .env
```

Edit `.env` and fill in all values:

```
POSTGRES_USER=herd
POSTGRES_PASSWORD=<choose a strong password>
POSTGRES_DB=herd

AUTH_SECRET_KEY=<generate with: openssl rand -hex 32>
AUTH_ALGORITHM=HS256
AUTH_ACCESS_TOKEN_EXPIRE_MINUTES=30
AUTH_REFRESH_TOKEN_EXPIRE_DAYS=7

SUPERADMIN_EMAIL=admin@example.com
SUPERADMIN_USERNAME=admin
SUPERADMIN_PASSWORD=<choose a strong password, min 8 chars>

INVENTORY_SERVICE_URL=http://inventory:8000
AUTH_SERVICE_URL=http://auth:8000

CORS_ORIGINS=https://localhost,https://<your-host-ip>

INTERNAL_API_TOKEN=<generate with: openssl rand -hex 32>

SECRETS_KEK=<generate with: python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())">

NATS_URL=nats://nats:4222
```

The `AUTH_SECRET_KEY` and `INTERNAL_API_TOKEN` should be unique random strings.
The superadmin account is created on first startup only; these values are ignored on subsequent restarts.

`SECRETS_KEK` is the encryption key for the secrets service (base64-encoded 32
bytes, not hex). It is required for `make prod`; the dev stack (`make up`)
supplies a dev-only key via `docker-compose.override.yml` so you can defer it
during evaluation. Store it safely: losing the KEK makes stored secrets
unrecoverable, and there is no default.

## 3. TLS certificates (HTTPS)

The repository includes pre-generated certs for `localhost` / `127.0.0.1` in `infra/traefik/certs/`. The included chain works out of the box for local-only development on `https://localhost`. If you need to reach the stack from another machine on your network (or with a different hostname):

1. Generate a new PKI chain (Root CA, Intermediate CA, Server cert with your IP as SAN)
2. Place files in `infra/traefik/certs/`
3. Update `infra/traefik/dynamic.yml` if filenames differ
4. Update `CORS_ORIGINS` in `.env` to match your IP

To trust the certs on client machines, install `infra/traefik/certs/root-ca.crt` as a trusted root CA.

## 4. Build and start all services

```bash
make up
```

This runs `docker compose up --build -d` which:
- Builds all service images (config, auth, inventory, reservations, cabling, acl, execution, ai-orchestrator, user-profile, notifications, frontend)
- Starts PostgreSQL 16, NATS JetStream, Traefik, and all application containers
- Each service auto-creates its database tables on startup via SQLAlchemy `create_all`
- The auth service seeds the superadmin account on first startup

Dev mode (default `make up`) mounts source directories and enables `--reload` via `docker-compose.override.yml`.
For production (no reload, no volume mounts):

```bash
make prod
```

## 5. Verify the system is healthy

```bash
docker compose ps
```

All containers should show `(healthy)`. Expect 14 services:
postgres, nats, traefik, config, auth, inventory, reservations, cabling, acl, execution, ai-orchestrator, user-profile, notifications, frontend.

Check logs if any service is unhealthy:

```bash
make logs
```

## 6. Access the application

- Web UI: `https://<your-host-ip>` (or `https://localhost`)
- HTTP auto-redirects to HTTPS
- Traefik dashboard: `http://<your-host-ip>:8080`
- API endpoints: `https://<your-host-ip>/api/auth`, `/api/inventory`, `/api/reservations`, `/api/cabling`, `/api/acl`, `/api/execution`, `/api/ai`, `/api/config`, `/api/notifications`, `/api/user-profile`

Log in with the superadmin credentials you set in `.env`.

## 7. Local development setup (optional)

For running tests and linting locally (outside Docker):

```bash
make install            # uv sync --all-extras (Python deps)
make frontend-install   # cd frontend && npm install
```

### Run tests

```bash
make test               # all backend tests (SQLite in-memory)
make test-common        # single service
make test-auth
make test-inventory
make test-reservations
make test-cabling
make test-acl
make test-execution
cd frontend && npm test  # frontend tests (vitest)
```

### Coverage

```bash
make coverage           # all backend services with terminal report
make coverage-auth      # single service (terminal + HTML report)
make coverage-frontend  # vitest with coverage
```

### Lint and format

```bash
make lint               # ruff check + eslint
make format             # ruff format + ruff check --fix
```

### Full validation

```bash
make master             # clean, install deps, format, lint, test all, build frontend + Docker
```

### Frontend dev server

```bash
make frontend-dev       # cd frontend && npm run dev
```

## Teardown and rebuild

To completely destroy all containers, volumes (database data), and caches:

```bash
make clean
```

Then start fresh from step 4.

To just restart without losing data:

```bash
make restart
```

## Database migrations (existing systems only)

On a fresh system, tables are auto-created by each service on startup; you do not need to run migrations.

For upgrading an existing database after schema changes:

```bash
make migrate            # runs alembic upgrade head in all service containers
make migrate-auth       # single service
make migrate-inventory
make migrate-reservations
make migrate-cabling
make migrate-acl
make migrate-execution
```

## Useful commands

```bash
make logs               # tail all container logs
make shell-auth         # exec into auth container
make shell-inventory    # exec into inventory container
make shell-reservations # exec into reservations container
make shell-cabling      # exec into cabling container
make shell-acl          # exec into acl container
make shell-execution    # exec into execution container
make build              # rebuild images without starting
make down               # stop containers (preserves volumes)
make clean              # stop containers + delete volumes + purge caches
```
