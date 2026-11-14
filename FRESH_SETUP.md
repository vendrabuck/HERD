# HERD Fresh System Setup

Complete guide for building HERD from scratch on a new machine.

## Prerequisites

- Docker and Docker Compose 2.27.1 or later. The Traefik service interpolates `${COMPOSE_PROJECT_NAME}`, which Compose sets for interpolation (from `-p`, the environment, `.env`, or the directory name) only since 2.27.1 (compose-go #628). On an older Compose every command fails with `required variable COMPOSE_PROJECT_NAME is missing a value: the compose project name is empty (needs Docker Compose 2.27.1 or later); Traefik would route nothing`; upgrade Compose (`docker compose version` shows yours).
- Git
- Node.js 22.22.2+ and npm 11.11.0+ (for local frontend dev/testing; `frontend/package.json` declares `engines.node: ">=22"`, but `engine-strict` in `frontend/.npmrc` binds every dependency's own engines floor too, and jsdom 30 declares `^22.22.2 || ^24.15.0 || >=26`, so a Node 22.x host below 22.22.2 is refused by `npm ci`/`npm install`, issue #885)
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

The `AUTH_SECRET_KEY` and `INTERNAL_API_TOKEN` should be unique random strings. Every backend service except config refuses to start while `AUTH_SECRET_KEY` is blank, still a `<...>` placeholder, or not usable as an HMAC key (a PEM block or ssh key, a truncated or lone `-----BEGIN` line included, or a JSON Web Key), and `AUTH_ALGORITHM` must be `HS256`, `HS384`, or `HS512`.
The superadmin account is created on first startup only; these values are ignored on subsequent restarts.

By default Traefik publishes the gateway (ports 80 and 443) on every IPv4 interface, so the stack answers at `https://localhost` and at each address the host has. To serve it on one address only, for example the wired NIC of a host that also has wifi, add `HERD_GATEWAY_BIND=<that address>` to `.env` (issue #1256; `.env.example` carries a commented example). `localhost` and every other interface are then refused, and the Makefile's seed, test, and gate targets address the stack at `https://<that address>` instead of `https://localhost`; `make gateway-url` prints the URL it derived. `make certs` (step 3) names that address in the gateway's certificate, so a browser there stops warning once the certificate is trusted.

`SECRETS_KEK` is the encryption key for the secrets service (base64-encoded 32
bytes, not hex). It is required for `make prod`; the dev stack (`make up`)
supplies a dev-only key via `docker-compose.override.yml` so you can defer it
during evaluation. Store it safely: losing the KEK makes stored secrets
unrecoverable, and there is no default.

## 3. TLS certificates (HTTPS)

The repository tracks no certificate and no key for the gateway. `make certs` generates them, and `make up` (step 4), `make dev`, `make prod`, and the gates run it first, so a fresh clone is served its own certificate without a separate step (issue #1239). Run it by hand to see what it does:

```bash
make certs
```

When `infra/traefik/certs/server.key` does not exist it writes two files there, the ones `infra/traefik/dynamic.yml` loads:

- `server.key`, a new RSA 2048 key (mode 0600);
- `server-chain.crt`, a SELF-SIGNED certificate for that key, valid for ten years (3650 days), with no CA chain.

Its subject alternative names are `DNS:localhost` and `IP:127.0.0.1`, plus `IP:<address>` when `.env` (or the environment) sets `HERD_GATEWAY_BIND` to an address other than `0.0.0.0`, plus every entry of `CERT_SANS`, a comma-separated list of `DNS:<name>` and `IP:<address>` entries for any other name or address clients use, for example `make certs CERT_SANS=DNS:lab.example.com,IP:192.0.2.20`. `dynamic.yml` also makes it Traefik's default certificate, so a client that connects by IP address (and so sends no SNI) gets it too.

`make certs` never writes over an existing key. With both files present it prints one line and changes nothing; with the key present but `server-chain.crt` missing it issues a new certificate for that key and leaves the key as it is. To regenerate both on purpose, for example after adding a `CERT_SANS` entry or changing `HERD_GATEWAY_BIND`, run `make certs CERT_FORCE=1`; it says it is regenerating over the existing key. A Traefik that is already running keeps serving the old certificate until it is recreated: `docker compose up -d --no-deps --force-recreate traefik`.

The certificate is self-signed, so every client warns until you trust it by hand. `make certs` prints the commands and runs none of them (they need administrator rights):

- Ubuntu or Debian: `sudo cp infra/traefik/certs/server-chain.crt /usr/local/share/ca-certificates/herd-dev.crt && sudo update-ca-certificates` (the file name must end in `.crt`). Chrome and Firefox on Linux keep their own stores; import the file there as an authority if they still warn.
- Windows: copy `server-chain.crt` to the machine and import it into the Local Computer store under Trusted Root Certification Authorities (`certutil -addstore Root server-chain.crt` in an administrator prompt, or double-click the file and choose Install Certificate).
- macOS: add it to the System keychain and mark it Always Trust. macOS 10.15 and later refuse TLS server certificates valid for more than 825 days even when trusted, so a ten-year certificate still warns there; issue one of your own (below) for a Mac client.

After `CERT_FORCE=1` every client must trust the new certificate again. The host-side harnesses (seed, integration, contract, e2e, load) skip certificate verification, so they need no trust step.

To use a certificate of your own instead (a CA-signed one, or a shorter validity), put its full chain in `infra/traefik/certs/server-chain.crt` and its key in `infra/traefik/certs/server.key` before the first `make up`; `make certs` then leaves both alone. Set `CORS_ORIGINS` in `.env` to include the address clients browse to.

## 4. Build and start all services

```bash
make up
```

This runs `docker compose up --build -d` which:
- Builds all service images (config, auth, inventory, reservations, cabling, acl, execution, ai-orchestrator, user-profile, notifications, integration, secrets, frontend)
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

All containers should show `(healthy)`. Expect 17 services:
postgres, nats, traefik, config, restarter, auth, inventory, reservations, cabling, acl, execution, ai-orchestrator, user-profile, notifications, integration, secrets, frontend.

Check logs if any service is unhealthy:

```bash
make logs
```

## 6. Access the application

- Web UI: `https://<your-host-ip>` (or `https://localhost`); with `HERD_GATEWAY_BIND` set, only `https://<that address>`, the URL `make gateway-url` prints
- HTTP auto-redirects to HTTPS
- Traefik dashboard: bound to loopback only (`127.0.0.1:8080` on the Docker host, issue #708). The dashboard and its `/api` have no authentication, so it is never published on a routable interface under `make prod`. To view it from another machine, open an SSH tunnel and browse the local end: `ssh -L 8080:127.0.0.1:8080 <user>@<your-host-ip>`, then `http://localhost:8080`. This applies to `make up` as well; on the Docker host itself `http://localhost:8080` works directly.
- API endpoints: `https://<your-host-ip>/api/auth`, `/api/inventory`, `/api/reservations`, `/api/cabling`, `/api/acl`, `/api/execution`, `/api/ai`, `/api/config`, `/api/notifications`, `/api/user-profile`, `/api/v1`, `/api/secrets`

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
make test-secrets
make test-integration-service  # the integration service's unit suite
cd frontend && npm test  # frontend tests (vitest)
make test-frontend-tz    # the same suite under TZ=America/Los_Angeles, as CI runs it a second time
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
make master             # full gate: uv.lock check, deps, format, lint, unit, frontend, image build, live LDAP, ephemeral-stack contract + integration + e2e, LDAP-mode stack tests, Postgres-live suites
```

### Frontend dev server

```bash
make frontend-dev       # cd frontend && npm run dev
```

## Teardown and rebuild

To completely destroy all containers, volumes (database data), and caches:

```bash
make clean-data
make clean
```

(`make clean` alone stops the stack and purges caches but keeps volumes, so
database data survives a plain clean.)

Then start fresh from step 4.

To just restart without losing data:

```bash
make restart
```

## Database migrations (existing systems only)

On a fresh system, tables are auto-created by each service on startup; you do not need to run migrations.

For upgrading an existing database after schema changes:

```bash
make migrate            # runs alembic upgrade head in each of the eleven database services
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
make clean              # stop containers + purge caches (volumes/data kept)
make clean-data         # stop containers + delete volumes (destructive)
```
