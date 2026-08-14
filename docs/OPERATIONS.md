# Operations Runbook

Day-2 operational procedures for whoever is running a HERD deployment. For initial install see [FRESH_SETUP.md](../FRESH_SETUP.md). For admin-level app tasks see [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md).

## Quick-reference commands

```bash
make up            # start stack (dev mode: hot-reload)
make prod          # start stack (production mode)
make down          # stop stack, preserve volumes
make clean         # stop stack (volumes/data KEPT) + purge caches
make clean-data    # stop + delete all volumes including Postgres data (destructive)
make restart       # restart without losing data
make logs          # tail all container logs
make shell-<svc>   # exec into a service container (auth, inventory, ...)
make build         # rebuild images without starting
make migrate       # run alembic upgrade head in every migratable service
make migrate-<svc> # single-service migration
```

`make clean` stops the stack and purges caches but KEEPS every Docker volume, so data survives; the `make master` and `make everything` gates run in a separate `<dir>-gate` compose project and never touch the dev stack's volumes either. A successful `make everything` deliberately leaves that gate stack running and seeded (usable at `https://localhost` without re-seeding); `make gate-down` stops and purges it, after which `make up` restores the dev stack. A failed run tears the gate stack down itself. `make clean-data` is the destructive variant: it drops every dev-stack volume including Postgres data. Never run it on an environment you care about without a backup. `make master-clean` goes further still: it runs the internal `_clean-images` helper first, which deletes every HERD-tagged Docker image (dev and gate projects) and prunes dangling layers, then runs `master` with `--no-cache` so every image rebuilds from scratch; it does not touch unrelated images on the host.

## Config-service first-run

There are two ways to get past the "login disabled" gate on a brand-new stack. Pick whichever fits your workflow.

### Option A: fill `.env` and skip the UI (recommended)

If your `.env` already has every required value, the config service auto-bootstraps on first startup. On boot, the service checks `/data/herd-config/config.json`; if the file is missing and every required var is present and non-empty in the process environment, it writes the file from those vars and the login form enables immediately. No wrench-icon detour, no manual paste.

Required values (also listed in `.env.example`):

- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- `AUTH_SECRET_KEY` (generate with `openssl rand -hex 32`)
- `INTERNAL_API_TOKEN` (generate with `openssl rand -hex 32`)

Optional but recommended: `SUPERADMIN_EMAIL`, `SUPERADMIN_USERNAME`, `SUPERADMIN_PASSWORD` seed the initial admin account on first startup. They are not required for bootstrap; if omitted, create the first admin through the UI after setup.

Optional: `AI_API_KEY` and `AI_MODEL` enable AI topology generation. Leaving them blank is supported; the Use AI button just stays hidden.

If any required var is missing, the config service logs a warning listing them and falls back to Option B.

### Option B: use the config UI

1. Open `https://<host>`.
2. Click the wrench icon on the login page.
3. Log in with the config-page password: set `CONFIG_ADMIN_PASSWORD` to choose it, or read the random one-time password the config service logs on first boot (`docker compose logs config`).
4. If you used the random one-time password, you must change it (min 8, max 32 chars) before the config write and apply actions unlock.
5. Fill in the required database, auth, and API-token fields (same required list as above). The superadmin fields are optional; if you skip them, create the first admin account through the UI afterward.
6. Click **Save and Restart**. The config service writes `config.json` to the shared Docker volume and restarts this compose project's app services via the Docker socket (config, traefik, postgres, nats, and the frontend are skipped; other compose projects on the host are never touched).
7. Once containers come back healthy (`docker compose ps`), the login form re-enables and you can sign in as the superadmin.

Config-page values take precedence over `.env` for every key that has been saved through the config UI; a `config.json` that exists only from the first-run auto-bootstrap stays subordinate to `.env`, so pure-`.env` operation is unchanged until the first UI save (see the precedence section in `ENV_VARS.md` for the marker mechanics and the escape hatches). The config UI's own password is independent of the app config: set `CONFIG_ADMIN_PASSWORD` to pin it, otherwise the wrench icon accepts the random one-time password logged on first boot.

## Upgrade path

For new schema additions between HERD versions:

```bash
git pull                         # get the new code
make build                       # rebuild images with the new code
docker compose up -d             # recreate containers onto the new images
make migrate                     # alembic upgrade head for every service
```

The order matters. `make migrate` execs into the running containers, and `make restart` (`docker compose restart`) never swaps a container onto a rebuilt image, so migrating or restarting before the `docker compose up -d` recreate would run the OLD image's migration files. Recreate first, then migrate: the exec then sees the new release's migrations.

Alembic migrations are per-service (one migration chain per database schema). On a brand-new stack, tables auto-create on startup via `create_all` and the schema is stamped at the Alembic head, so migrations aren't strictly required. Once a schema carries that stamp, startup never runs `create_all` again (issue #419): the schema evolves only through Alembic. Between the recreate and the migrate, a service whose new release adds a table boots without it and logs a warning naming the stamped revision and the new head; `make migrate` creates the table, and the warning clears on the next restart. The event consumers are gated on that same missing-tables signal (issue #463): execution, notifications, and integration hold their NATS consumer start while model tables are missing, so events in the window stay pending on the stream instead of dead-lettering, and the deferred consumer starts automatically once `make migrate` creates the tables, with no container restart needed.

If a migration fails mid-flight, the service stays down. Fix the cause, re-run `make migrate-<service>` for just that one.

## Inspecting the NATS DLQ

Three durable consumers feed off two source streams (`HERD_RESERVATIONS` for `herd.reservations.*`, `HERD_HEALTH` for `herd.health.*`). Each consumer routes its failures to its own 4-token DLQ subject so one consumer's failures do not mask another's. All DLQ subjects are captured by a single dedicated `HERD_DLQ` stream (`herd.*.dlq.>` subjects), created by the execution service at startup. The DLQ subjects are deliberately one token longer than any consumer's 3-token filter, so a DLQ'd message is never redelivered to the consumer that failed it:

- `execution` consumer (`execution-consumer`), DLQ `herd.reservations.dlq.execution`
- `notifications` consumer (`notifications-consumer`), DLQ `herd.reservations.dlq.notifications`
- `notifications` health consumer (`notifications-health-consumer`), DLQ `herd.health.dlq.notifications`

Messages that poisoned any consumer (bad JSON or exhausted `max_deliver=5`) land on the consumer's DLQ subject and are retained in `HERD_DLQ`. Inspect with the `nats` CLI (install via `brew install nats-io/nats-tools/nats` or the binary from github.com/nats-io/natscli):

```bash
# List the DLQ stream and its retained messages
docker compose exec nats nats stream info HERD_DLQ

# Dump the last message per DLQ subject
docker compose exec nats nats sub 'herd.reservations.dlq.execution' --last-per-subject     # execution
docker compose exec nats nats sub 'herd.reservations.dlq.notifications' --last-per-subject  # notifications (reservations)
docker compose exec nats nats sub 'herd.health.dlq.notifications' --last-per-subject        # notifications (health)

# Or use a durable pull consumer to walk messages one at a time
docker compose exec nats nats consumer add HERD_DLQ dlq-inspector \
  --filter 'herd.reservations.dlq.execution' --ack none --deliver all --pull
docker compose exec nats nats consumer next HERD_DLQ dlq-inspector
```

Each DLQ message is a verbatim copy of the original event payload. Reservation events look like `{"event": "reservation.created", "reservation_id": "...", ...}`; health events look like `{"event": "device.health_transition", "device_id": "...", "transition_kind": "bad_news", ...}`.

### Replaying a DLQ message

Once you understand what made the message fail and have fixed the underlying cause, re-publish it on the original subject. Every durable consumer bound to that subject reprocesses it: for `herd.reservations.*` that is both execution and notifications; for `herd.health.*` it is notifications only (execution publishes health events, it does not consume them):

```bash
docker compose exec nats nats pub 'herd.reservations.created' "$(cat msg.json)"
```

### Discarding DLQ messages

If the events are stale (e.g. you've already manually fixed the state), purge the relevant DLQ subject:

```bash
docker compose exec nats nats stream purge HERD_DLQ \
  --subject 'herd.reservations.dlq.execution'
docker compose exec nats nats stream purge HERD_DLQ \
  --subject 'herd.reservations.dlq.notifications'
docker compose exec nats nats stream purge HERD_DLQ \
  --subject 'herd.health.dlq.notifications'
```

## TLS certificate rotation

Certs live in `infra/traefik/certs/`. `infra/traefik/dynamic.yml` loads exactly two files: `server-chain.crt` (leaf plus intermediate, concatenated) and `server.key`. Rotating `server.crt` alone changes nothing Traefik serves; regenerate the chain file.

```
infra/traefik/certs/
  root-ca.crt          # distribute to clients; not loaded by Traefik
  intermediate-ca.crt
  server.crt           # leaf; input to server-chain.crt
  server-chain.crt     # loaded by Traefik (certFile)
  server.key           # loaded by Traefik (keyFile)
```

To rotate:

1. Generate a new PKI chain (keep the same filenames for a drop-in rotation, or update `dynamic.yml` if you change names).
2. Replace the files in `infra/traefik/certs/`.
3. Bounce Traefik: `docker compose restart traefik`. No other service restart is needed; backends don't serve TLS themselves.
4. Re-distribute `root-ca.crt` to client machines (install it as a trusted root CA).

The SAN in `server.crt` must match the hostname or IP users hit (e.g. `IP:192.0.2.10` or `DNS:lab.example.com`). Bumping this requires regenerating the server cert with the new SAN.

## Secrets-service key rotation

Two independent rotations; know which one you need (see `docs/ARCHITECTURE.md`
for the envelope-encryption model):

**KEK rotation** (the environment key leaked or is due for rotation). Cheap:
it re-wraps stored data-encryption keys and never touches secret rows.

1. Generate a new key: `python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`.
2. In `.env`, move the current `SECRETS_KEK` value to `SECRETS_KEK_PREVIOUS`
   and set `SECRETS_KEK` to the new key.
3. `docker compose up -d secrets` (recreate so the new env applies; a plain
   restart does not re-read `.env`). Boot re-wraps every stored key under the
   new KEK and logs `keyring_rewrapped`.
4. Remove `SECRETS_KEK_PREVIOUS` from `.env`. Done; the old KEK is dead.

**DEK rotation** (rotate the key that actually encrypts values). O(number of
secrets), all in one transaction:

```
POST /api/secrets/keys/rotate      (admin JWT)
```

introduces a new key version, re-encrypts every secret to it, and retires (but
retains) prior versions so nothing becomes undecryptable. Verify with a reveal
afterwards.

Losing `SECRETS_KEK` with no `SECRETS_KEK_PREVIOUS` window makes stored
secrets unrecoverable; there is no backdoor. Keep the KEK in whatever secret
management wraps your `.env`, and note that database backups (above) are
useless for secret recovery without the KEK from the same era, which is a
feature.

## Backup and restore

What to preserve:

- **Postgres data**: a single Docker volume (`postgres-data`) holding one database with one schema per service. Dump via `pg_dump`:
  ```bash
  docker compose exec postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > backup-$(date +%Y%m%d).sql
  ```
  Restore:
  ```bash
  cat backup-YYYYMMDD.sql | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
  ```
- **Config volume** (`herd-config`): holds `config.json` and `config_auth.json`. Copy the contents out:
  ```bash
  docker compose cp config:/data/herd-config ./herd-config-backup
  ```
- **Driver storage**: either the `driver-storage` Docker volume (default local filesystem at `/data/drivers`) or your MinIO bucket if configured. Back up with `docker compose cp` or MinIO's replication.
- **Uploaded topologies, reservations, etc.**: all in Postgres; covered by the DB dump.

Test restore at least annually on a throwaway stack.

## Log inspection recipes

All services emit JSON logs via `herd_common.logging.setup_logging`. Per-service tail plus `jq` filter is usually enough:

```bash
# --no-log-prefix strips the "container | " prefix so jq can parse the lines;
# jq -R 'fromjson?' skips any non-JSON lines (postgres, nats, traefik).

# Errors in the reservations service
docker compose logs reservations --no-log-prefix --tail 1000 | \
  jq -R 'fromjson? | select(.level=="ERROR")'

# Every reservation-create decision in the last 500 lines
docker compose logs reservations --no-log-prefix --tail 500 | \
  jq -R 'fromjson? | select((.action? // "") | startswith("reservation_"))'

# All 5xx responses across every service
docker compose logs --no-log-prefix --tail 2000 | \
  jq -R 'fromjson? | select((.status_code // 0) >= 500)'

# NATS DLQ routings from execution
docker compose logs execution --no-log-prefix | \
  jq -R 'fromjson? | select(.action == "nats_dlq_exhausted" or .action == "nats_poison_message")'

# Slow requests (over 1s)
docker compose logs --no-log-prefix --tail 5000 | \
  jq -R 'fromjson? | select((.duration_ms // 0) > 1000)'
```

Configure verbosity per service via `LOG_LEVEL` (default `INFO`; use `DEBUG` for investigations and remember to turn it back down).

## Healthchecks and monitoring

- `docker compose ps` shows per-container health. Unhealthy containers need a logs inspection.
- Every backend service exposes `GET /health` (200 when healthy), reachable through the gateway at `https://<host>/api/<svc>/health` (e.g. `curl -k https://localhost/api/auth/health`). From inside a container use `docker compose exec <svc> python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').status)"`; the service images do not ship curl.
- Live OpenAPI docs: `https://<host>/api/<service>/docs` (auth required for most endpoints).

There is no built-in Prometheus/Grafana stack; roll your own based on the JSON logs and `/health` endpoints.

## Scaling and sizing

- Postgres: single node by default. Horizontal scaling would require adding a pooler (PgBouncer) and replication; not in the default compose.
- Services are stateless; scale by bumping `replicas` in compose and putting Traefik or another load balancer in front. Today they run 1x each.
- Health polling scales horizontally (issue #24): run extra execution replicas with `EXECUTION_POLLER_ONLY=true` (background machinery only, no API routers) and set `HEALTH_POLL_SCHEDULER_ENABLED=false` on the API replicas for a clean split. Concurrent pollers against one schema are safe: due rows are claimed via `SELECT ... FOR UPDATE SKIP LOCKED` plus a conditional update, so two replicas never poll the same device. Bound each replica with `HEALTH_POLL_BATCH_SIZE` and `HEALTH_POLL_MAX_CONCURRENCY`, and watch the per-tick `health_tick` log (`rows_due` versus `polls_fired`) to decide when to add a replica.
- NATS JetStream is a single node; message persistence is in the `nats` volume. For HA, cluster three NATS nodes.
- Execution service's driver cache (`/data/driver-cache`) grows with driver churn; size the volume accordingly.

## Disaster scenarios

### Postgres is down

- Services that depend on it (auth, inventory, reservations, cabling, acl, execution) will fail their `/health` and crash-loop.
- Config service keeps running (no DB).
- Recover: bring Postgres back up, let the services auto-recover. No manual intervention needed for the app; replay any lost transactions from backups if data was corrupted.

### NATS is down

- Reservations service still accepts writes. Lifecycle events are written to the transactional outbox in the same transaction as the state change (issue #21), so nothing is lost while NATS is unreachable; the relay just cannot publish them yet.
- Execution service can't consume events during the outage, so L1/L2 driver operations are NOT triggered yet; a reservation goes `ACTIVE` but its devices are not configured on real hardware until events flow again. Health-transition events are likewise buffered in the execution outbox.
- Recover: bring NATS up. The per-service outbox relays reconnect and drain their unpublished rows on the next tick, so reservation and health events are delivered once (not lost), just delayed. No manual catch-up event is needed for these streams.

### Config volume is wiped

Services that can't find `config.json` will crash-loop. Rebuild by going through the config-service first-run again (same flow as initial setup).

### Main commit accidentally force-pushed

Always avoid. If it happened, restore from the most recent verified backup. All GitHub operations (push, merge, force-push) are performed by the repo owner directly, never by automation.

## Useful cross-references

- [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md) for app-level admin tasks (device groups, templates, promotions).
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for user-reported symptoms and their root causes.
- [ENV_VARS.md](ENV_VARS.md) for every env var.
- [ROLES.md](ROLES.md) for the endpoint-level permission matrix.
- [ARCHITECTURE.md](ARCHITECTURE.md) for the architecture overview.
