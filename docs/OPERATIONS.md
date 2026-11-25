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

`make clean` stops the stack and purges caches but KEEPS every Docker volume, so data survives. Every `down` variant (`down`, `clean`, `clean-data`, `gate-clean`) enables the `e2e` compose profile, so a Selenium container left by `make test-e2e` is stopped as well; a plain profile-less `down` would leave it running on host port 4444 and the next gate's e2e phase would fail at Selenium recreate before any test ran. The `make master` and `make everything` gates run in a separate `<dir>-gate` compose project and never touch the dev stack's volumes either. A successful `make everything` deliberately leaves that gate stack running and seeded (usable at `https://localhost`, or at the `HERD_GATEWAY_BIND` address when `.env` sets one, without re-seeding; `make gateway-url` prints which); `make gate-down` stops and purges it, after which `make up` restores the dev stack. The two projects publish the same host ports 80 and 443, so `make up`, `make dev`, and `make prod` refuse to start while another compose project publishes either port, naming the project that holds them (`scripts/check_gateway_ports_free.sh`, given the project name `docker compose config` reports for the checkout). A gate run checks the same before it starts its stack: by then it has stopped this checkout's dev stack and purged this checkout's old gate stack, so only another checkout's stack can block it, and without the check its seed, integration, and e2e phases would run against that other stack. Each stack's Traefik discovers only the containers of its own compose project (`--providers.docker.constraints` on the `com.docker.compose.project` label, set in the `traefik` service's command in `docker-compose.yml`, which is Traefik's whole static configuration; there is no `traefik.yml`), so one project's containers never join another project's routes. A failed run tears the gate stack down itself. Every gate run, pass or fail, leaves the dev stack stopped (its `gate-clean` step stops it before anything else runs), so after a failed gate `make up` alone restores the dev stack with its data. `make clean-data` is the destructive variant: it drops every dev-stack volume including Postgres data. Never run it on an environment you care about without a backup. `make master-clean` goes further still: it runs the internal `_clean-images` helper first, which deletes this checkout's compose images (those labelled with its dev project or its gate project) and prunes dangling layers, then runs `master` with both image builds forced to `--no-cache` (master-quick's dev-project build and the gate stack's own build), so every image rebuilds from scratch; it does not touch the test labs' images, other checkouts' images, or unrelated images on the host.

## Config-service first-run

There are two ways to get past the "login disabled" gate on a brand-new stack. Pick whichever fits your workflow.

### Option A: fill `.env` and skip the UI (recommended)

If your `.env` already has every required value, the config service auto-bootstraps on first startup. On boot, the service checks `/data/herd-config/config.json`; if the file is missing, every required var is present and non-empty in the process environment, and `AUTH_SECRET_KEY` and `AUTH_ALGORITHM` pass the services' startup rules (the secret not a `<...>` placeholder; the algorithm, when set, `HS256`, `HS384`, or `HS512`), it writes the file from those vars and the login form enables immediately. No wrench-icon detour, no manual paste.

Required values (also listed in `.env.example`):

- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- `AUTH_SECRET_KEY` (generate with `openssl rand -hex 32`)
- `INTERNAL_API_TOKEN` (generate with `openssl rand -hex 32`)

Optional but recommended: `SUPERADMIN_EMAIL`, `SUPERADMIN_USERNAME`, `SUPERADMIN_PASSWORD` seed the initial admin account on first startup. They are not required for bootstrap; if omitted, create the first admin through the UI after setup.

Optional: `AI_API_KEY` and `AI_MODEL` enable AI topology generation. Leaving them blank is supported; the Use AI button just stays hidden.

If any required var is missing, the config service logs a warning listing them and falls back to Option B; a refused `AUTH_SECRET_KEY` or `AUTH_ALGORITHM` is logged the same way, with the refusal message, and nothing is written.

### Option B: use the config UI

1. Open `https://<host>`.
2. Click the wrench icon on the login page.
3. Log in with the config-page password: set `CONFIG_ADMIN_PASSWORD` to choose it, or read the random password the config service generates and logs once, at WARNING, on first boot (`docker compose logs config`).
4. If you used the generated password, you must change it (min 8, max 32 chars) before the config write and apply actions unlock. Until you change it, the generated password stays a working config login for anyone who can read the config container's log, including any log shipper that collects it: that session can read the merged settings (secret values masked) and change the password, which unlocks save and restart. Either set `CONFIG_ADMIN_PASSWORD` or change the generated password promptly, and treat the config container's log as sensitive until you do.
5. Fill in the required database, auth, and API-token fields (same required list as above). A blank or `<...>` placeholder `AUTH_SECRET_KEY`, or a set `AUTH_ALGORITHM` other than `HS256`, `HS384`, or `HS512`, is refused with 422 and nothing is written. The superadmin fields are optional; if you skip them, create the first admin account through the UI afterward. The page saves only the settings its schema lists: a save that names any other key is refused with 422 `Unknown settings: <keys>` and writes nothing, and a key placed in `config.json` by hand outside the schema is not shown in the editor and is carried over unchanged by the next save.
6. Click **Save and Restart**. The config service writes `config.json` to the shared Docker volume and asks the `restarter` container to restart the services that read it: the eleven that mount `herd-config:/etc/herd:ro`, in this compose project only (config, traefik, postgres, nats, the frontend, and Selenium are not restarted; other compose projects on the host are never touched). The config service holds no Docker socket; the restarter, reachable only from the config service on the internal `herd-ctl` network, is the one HERD container that does ([ADR 0017](design/0017-config-restart-without-the-docker-socket.md)). If a restart fails, the response's `errors` list names what failed and the exception class only (for example `Cannot connect to Docker: FileNotFoundError` from the restarter, or `Cannot reach the restart service: ConnectionRefusedError` when the restarter is down); the full text is in the restarter's or the config service's log (`docker compose logs restarter config`). A second Save and Restart while one runs answers `A restart is already in progress`.
7. Once containers come back healthy (`docker compose ps`), the login form re-enables and you can sign in as the superadmin, provided the seed created it: a `SUPERADMIN_PASSWORD` that breaks the registration password rules (8 to 72 characters, no NUL character) creates no account and auth logs one ERROR naming the rule (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#no-superadmin-account-after-the-first-boot)).

Config-page values take precedence over `.env` for every key that has been saved through the config UI; a `config.json` that exists only from the first-run auto-bootstrap stays subordinate to `.env`, so pure-`.env` operation is unchanged until the first UI save (see the precedence section in `ENV_VARS.md` for the marker mechanics and the escape hatches). The config UI's own password is independent of the app config: set `CONFIG_ADMIN_PASSWORD` to pin it, otherwise the wrench icon accepts the random password logged on first boot, which stays valid until it is changed.

The config login limits failed attempts. From a source address's third consecutive failure, that source waits before its next try (1 second, doubling to 60 seconds), and once `CONFIG_LOGIN_MAX_ATTEMPTS` failures (default 20) from any sources fall within `CONFIG_LOGIN_LOCKOUT_SECONDS` (default 300), every login waits that long, including yours. A waiting login gets HTTP 429 with a `Retry-After` header (the page shows `Too many failed login attempts; try again in N seconds` and keeps **Sign in** disabled for that wait, while a wrong password still shows `Invalid config password`), and each wait that begins logs one WARNING line starting `config_login_locked` with the scope and source address (`docker compose logs config`). A successful login clears the counts, and restarting the config service (`docker compose restart config`) clears them too. The source address is the client address Traefik forwards, so if you put another proxy in front of Traefik, configure Traefik's `forwardedHeaders.trustedIPs` for it or every client shares that proxy's address.

## Upgrade path

For new schema additions between HERD versions:

On a production host (a stack started with `make prod`):

```bash
git pull                                   # get the new code
make build                                 # rebuild images with the new code
docker compose -f docker-compose.yml up -d # recreate containers onto the new images
make migrate                               # alembic upgrade head for every service
```

The recreate names `-f docker-compose.yml` because that is what `make prod` runs (`docker compose -f docker-compose.yml up --build -d`; `make prod` itself can replace the build and recreate lines). A bare `docker compose up -d` also merges `docker-compose.override.yml`, which would recreate a production stack in dev mode: code bind mounts and hot reload, `HERD_FAULT_INJECTION` on, and the dev `WEBHOOK_ALLOWED_HOSTS`. On a dev stack started with `make up`, use the bare `docker compose up -d` (or `make up`) for the recreate step instead.

The order matters. `make migrate` execs into the running containers, and `make restart` (`docker compose restart`, after recreating only a Traefik that predates PR #1240, below) never swaps a container onto a rebuilt image, so migrating or restarting before the recreate would run the OLD image's migration files. Recreate first, then migrate: the exec then sees the new release's migrations.

Recreate Traefik when upgrading past PR #1240 (the Traefik static configuration moved from `infra/traefik/traefik.yml` into the `traefik` command in `docker-compose.yml`, and the file was deleted). A Traefik container created before that change still bind-mounts the deleted file, and RESTARTING it, rather than recreating it, fails: Docker recreates the missing mount source as a root-owned directory and the start ends with a `not a directory` mount error, so the gateway is down. Recreating is safe, so after the pull run `make up` (or `docker compose up -d`, or `docker compose -f docker-compose.yml up -d` under `make prod`; for a gate stack left up, `docker compose -p <dir>-gate up -d --no-deps --force-recreate traefik`) BEFORE anything that restarts containers. `make restart` itself now recreates such a Traefik first, but a plain `docker compose restart`, a Docker daemon restart, or a host reboot (Traefik is `restart: unless-stopped`) does not. If the failed start already happened, `infra/traefik/traefik.yml` is a root-owned directory in the checkout; remove it with `sudo rmdir infra/traefik/traefik.yml` (it is empty) and recreate Traefik. Until it is removed it also fails `tests/unit/test_gateway_ports_guard.py` and blocks checking out an older commit, which needs a file at that path.

Rebuild, and fix the config volume's owner once, when upgrading past issue #1326 (the config service runs as a non-root user and Save and Restart goes through the `restarter` container). `make up` or `make prod` builds the restarter and recreates the config service without the Docker socket; `make restart` swaps no image and does neither. A `herd-config` volume an earlier release created holds files owned by root, which the config service can read but not write: it logs one error line naming the fix, and every save answers 503 `The config data directory is not writable; the config service log names the one-time fix`. Run once, with the stack up or down:

```bash
docker compose run --rm --no-deps --user root config chown -R appuser:appuser /data/herd-config
```

Under `make prod` add `-f docker-compose.yml` after `docker compose`; for a gate stack add `-p <dir>-gate`. A volume created by this release needs nothing.

Check `AUTH_SECRET_KEY` and `AUTH_ALGORITHM` when upgrading past the access-token settings validation: every service except config refuses to start while `AUTH_SECRET_KEY` is unset, blank, still a `<...>` placeholder, or not usable as an HMAC key, or while `AUTH_ALGORITHM` is anything but `HS256`, `HS384`, or `HS512`, and the log line names the variable. Fix the value in `.env`, or in the config UI if a configuration was saved there. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#services-crash-loop-on-startup).

Check outbound webhook subscriptions when upgrading past the webhook destination rule: a subscription whose `target_url` host resolves to a loopback, private, or other non-public address now records `failed` deliveries with `destination not allowed` and is not sent to. Under `make prod`, `WEBHOOK_ALLOWED_HOSTS` is empty; name each internal receiver there (a hostname or a CIDR) before upgrading, or re-register it at a public address. See [EXTERNAL_API.md](EXTERNAL_API.md#destination-rule).

Decide what to do with older refused configure runs when upgrading past issue #1457. A `configure` run whose configuration failed schema validation stored a refusal that could quote the refused configuration, credentials included (a vtysh line with `neighbor ... password ...`), and that text is still in the run's `error`. The upgrade rewrites no row; instead execution withholds the old text whenever it answers a run (CFG-RUN-8): the run list, the run detail, and so the assistant's `list_executions_for_reservation` tool get `device '<name>': config failed schema validation (details withheld; recorded before values were removed from these messages)`. That covers the API and the assistant only, not a direct database read, a backup, or the log lines below. The old text reads `config failed schema validation:` followed by the library's message, the new text `config failed schema validation at <path>:` followed by the rule, so this finds the old rows (run it in `docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`; under `make prod` add `-f docker-compose.yml` after `docker compose`):

```sql
SELECT id, created_at, reservation_id
FROM execution.execution_runs
WHERE action = 'configure'
  AND status = 'FAILED'
  AND error LIKE '%: config failed schema validation: %';
```

To remove the quoted text and keep the rest of each row, run the same predicate as an update. It cannot be undone, so take a backup first if the text matters to an investigation:

```sql
UPDATE execution.execution_runs
SET error = regexp_replace(
  error,
  ': config failed schema validation: .*$',
  ': config failed schema validation (text removed after issue #1457)'
)
WHERE action = 'configure'
  AND status = 'FAILED'
  AND error LIKE '%: config failed schema validation: %';
```

Two copies outside the run rows can also hold it. Inventory's apply-job rows never stored this text (issue #1093), but inventory's warning log line `config apply: execution answered HTTP 422: <body>` from before the upgrade can, until Docker's log rotation drops it. And an assistant tool result is stored with its conversation in `ai_orchestrator.assistant_messages.content_blocks`: a run list the assistant read before the upgrade stays there until the conversation has been idle for `ASSISTANT_CONVERSATION_TTL_HOURS` (24 by default, longer while the purpose-classification exemption in [ENV_VARS.md](ENV_VARS.md) keeps it), when the hourly sweeper deletes it.

Since issue #1464 the assistant masks the stored configurations and run rows its tools relay to the model provider (AI-TOOL-12 in `docs/specs/ai-features.md`), and the credential mask for stored run arguments is wider: keys with a keyword part (`pre_shared_key`, `tacacsKey`), keywords with `:` or `=` attached, JSON held in a string, JSON-escaped text, XML elements, Python reprs, URL passwords, and the keywords `authentication`, `auth`, `phash`, `psk`, `token`, and `bearer`. The upgrade rewrites no row. A conversation the assistant had before the upgrade keeps its unmasked tool results in `ai_orchestrator.assistant_messages.content_blocks` until it is swept as above, but every later turn masks them again before resending them (AI-TOOL-13). Its earlier assistant replies are resent as written: one that quoted a credential goes to the provider again on each later turn of that conversation, and through purpose classification's transcripts when that is on, until the conversation is swept (24 hours idle by default, longer while the purpose-classification exemption keeps it). To end that sooner, delete the affected conversations; what the provider already received cannot be recalled. A run stored before the upgrade whose arguments hold something only the wider mask catches keeps it in clear in its row: execution's run reads answer it as stored, and only the assistant's tools mask it on read. If such a run was masked for another line, its retry may now be refused with 409 because its config version no longer masks to the stored copy; a run whose configuration holds nothing the wider mask newly matches retries as before. Start a new run instead.

After the recreate, the admin-only About page (Administration, then About) is the fastest way to confirm every service actually landed on the new build: a row marked "differs" or "unreachable" names the service that still needs a rebuild. See `docs/ADMIN_HANDBOOK.md`.

Since issue #593, `make build` installs every service's third-party dependencies from the workspace `uv.lock` (each Dockerfile runs `uv export --frozen --package <svc>` and installs that requirements file verbatim), not by resolving fresh from PyPI at build time. A checkout with a stale `uv.lock` (a `pyproject.toml` dependency bump with no matching `uv lock` run) fails the build outright, the same failure `uv lock --check` reports for a host install; run `uv lock` and rebuild.

Alembic migrations are per-service (one migration chain per database schema). On a brand-new stack, tables auto-create on startup via `create_all` and the schema is stamped at the Alembic head, so migrations aren't strictly required. Once a schema carries that stamp, startup never runs `create_all` again (issue #419): the schema evolves only through Alembic. Between the recreate and the migrate, a service whose new release adds a table boots without it and logs a warning naming the stamped revision and the new head; `make migrate` creates the table, and the warning clears on the next restart. A new COLUMN is different: a release that adds a column to a table a service reads at startup is not bootable until `make migrate-<svc>` has run, because the startup read names the column the stamped schema still lacks and the service exits with `column <table>.<name> does not exist`. Under `make up` the dev reloader keeps the crashed container alive, so `make migrate-<svc>` (a `docker compose exec`) still works; then `docker compose restart <svc>`. Under `make prod` there is no reloader and the container crash-loops, so `docker compose exec` races the restarts: run the migration in a one-off container instead, `docker compose -f docker-compose.yml run --rm <svc> alembic upgrade head`, then `docker compose -f docker-compose.yml restart <svc>`. Auth's own startup seed reads only ids since issue #1261, so a release that adds a column to `users` no longer stops auth from booting before the migrate. The event consumers are gated on that same missing-tables signal (issue #463): execution, notifications, and integration hold their NATS consumer start while model tables are missing, so events in the window stay pending on the stream instead of dead-lettering, and the deferred consumer starts automatically once `make migrate` creates the tables, with no container restart needed.

If a migration fails mid-flight, the service stays down. Fix the cause, re-run `make migrate-<service>` for just that one.

`make migrate-inventory` revision 0022 (issue #1053) adds `hypervisors.device_group_id`
and leaves it null on every existing hypervisor, which means admins only: after the
upgrade, non-admins cannot see or book any dynamic template until an admin sets
**Bookable by device group** on each hypervisor (Administration, then Hypervisors).
`make migrate-reservations` revision 0017 (issue #997) adds
`reservations.provision_started_at`; rows that predate it have no stamp, and the
provisioning backstops measure those from `updated_at` as before.
`make migrate-reservations` revision 0018 (issue #1522) is a one-time data pass:
every `reservations.pending_fork_prune_device_ids` holding the JSON literal `null`
or an empty list becomes SQL NULL, so the expiration sweep stops selecting
reservations with no pending fork prune and reaches the ones that have one. It is
safe to rerun, and `downgrade` is a no-op. Restart or rebuild the reservations
service onto the new code before the migration or right after it: a process still
running the previous code writes the JSON `null` form again on its next clear (the
dev reloader does not fire on a git-driven file swap, so `docker compose restart
reservations`). From this version the expiration sweep also repairs such a marker,
and any corrupt one, to SQL NULL: the reconciler repairs up to 20 such markers per
tick, oldest first, so a backlog of n drains in ceil(n/20) ticks instead of occupying
the batch forever (more ticks if older real markers whose prune keeps failing hold
some of the 20 places).

`make migrate-cabling` (part of a plain `make migrate`) includes a one-time data
scrub (revision 0013, hardening): every stored canvas across `topologies`,
`topology_versions`, `reservation_fork`, `fork_versions`, and
`topology_templates` has each device node's `data.device` reduced to the fixed
allowlist described in `docs/ARCHITECTURE.md`'s "Device node persistence"
section, removing `field_data` and anything else outside it. It is batched and
idempotent (a rerun, or a database this revision has already touched, is a
no-op), and `downgrade` is a no-op by design: the stripped data is not
recoverable.

## Remote access to NATS and Postgres

`docker-compose.yml` publishes NATS (4222, 8222) and Postgres (`POSTGRES_PORT`, default 5433) on loopback only, matching the Traefik dashboard's loopback bind (issue #708): NATS carries no broker authentication at all, and a wide bind on either would be reachable from the LAN or the open internet on a host with no firewall in front of it. The recipes below reach NATS from a one-off `natsio/nats-box` container on the stack's Docker network, which needs no host port at all. From a remote host, either run the same commands over SSH, or forward the port first (`ssh -L 4222:localhost:4222 <host>` or `-L 5433:localhost:5433`) and point a local client (the `nats` CLI, `psql`) at `localhost`. Never widen the compose binding to reach either service remotely.

## Inspecting the NATS DLQ

Five durable consumers feed off two source streams (`HERD_RESERVATIONS` for `herd.reservations.*`, `HERD_HEALTH` for `herd.health.*`). Each consumer routes its failures to its own 4-token DLQ subject so one consumer's failures do not mask another's. All DLQ subjects are captured by a single dedicated `HERD_DLQ` stream (`herd.*.dlq.>` subjects), created by the execution service at startup. The DLQ subjects are deliberately one token longer than any consumer's 3-token filter, so a DLQ'd message is never redelivered to the consumer that failed it:

- `execution` consumer (`execution-consumer`), DLQ `herd.reservations.dlq.execution`
- `notifications` consumer (`notifications-consumer`), DLQ `herd.reservations.dlq.notifications`
- `notifications` health consumer (`notifications-health-consumer`), DLQ `herd.health.dlq.notifications`
- `integration` webhooks consumer (`integration-webhooks-consumer`), DLQ `herd.reservations.dlq.integration`
- `integration` webhooks health consumer (`integration-webhooks-health-consumer`, issue #831), DLQ `herd.health.dlq.integration`

Messages that poisoned any consumer (bad JSON, JSON that is not an object, or exhausted `max_deliver=5`) land on the consumer's DLQ subject, as does an execution event whose failure no retry can change, on its first delivery (log action `nats_dlq_permanent`; for example a dynamic recipe whose template, hypervisor, or secret is missing, or whose package fails the load's checks). They are retained in `HERD_DLQ` within the stream's `max_age` (see JetStream durability below); on the dev/test path they are retained only until the next container recreate.

The compose `nats` service runs `nats:2.10.29-alpine` (pinned with its digest), which ships only `nats-server`: there is no `nats` CLI inside it, so running `nats` in that container fails with "executable file not found". Run the CLI from a one-off `natsio/nats-box` container on the stack's network instead, the same form `docs/TROUBLESHOOTING.md` uses. The network is `<compose project>_herd-net` (for example `herd-public_herd-net` for a checkout in `herd-public/`; `docker network ls | grep herd-net` lists it). Every command below goes through this one shell variable; `nats-box:0.14.5` carries natscli 0.1.5, whose flags these commands use:

```bash
NATS="docker run -i --rm --network herd-public_herd-net natsio/nats-box:0.14.5 nats --server nats://nats:4222"
```

```bash
# The DLQ stream: message count, first and last sequence
$NATS stream info HERD_DLQ

# How many messages each DLQ subject holds
$NATS stream subjects HERD_DLQ

# The newest message on one DLQ subject
$NATS stream get HERD_DLQ --last-for 'herd.reservations.dlq.execution'      # execution
$NATS stream get HERD_DLQ --last-for 'herd.reservations.dlq.notifications'  # notifications (reservations)
$NATS stream get HERD_DLQ --last-for 'herd.health.dlq.notifications'        # notifications (health)
$NATS stream get HERD_DLQ --last-for 'herd.reservations.dlq.integration'    # integration webhooks (reservations)
$NATS stream get HERD_DLQ --last-for 'herd.health.dlq.integration'          # integration webhooks (health)

# Walk the stream one message at a time by sequence (the range is in stream info)
$NATS stream get HERD_DLQ 1
```

These are read-only: `stream get` fetches by sequence or subject and creates no consumer, so inspecting leaves no state on the server.

Each DLQ message is a verbatim copy of the original event payload. Reservation events look like `{"event": "reservation.created", "reservation_id": "...", ...}`; health events look like `{"event": "device.health_transition", "device_id": "...", "transition_kind": "bad_news", ...}`.

### Replaying a DLQ message

Once you understand what made the message fail and have fixed the underlying cause, re-publish it on the original subject. Every durable consumer bound to that subject receives it: for `herd.reservations.*` that is execution, notifications, and integration's webhooks consumer; for `herd.health.*` it is notifications and integration's webhooks health consumer (execution publishes health events, it does not consume them).

Know what a replay can and cannot do before sending one:

- Broker dedup: JetStream drops a message whose `Nats-Msg-Id` it saw inside the stream's duplicate window, so the command below stamps a fresh id with natscli's `{{ID}}` template. A replay without the header is not deduplicated by the broker at all.
- Consumer dedup: consumers key idempotency on the payload `event_id`, which the replay keeps. A consumer that already handled the event does not act on it twice (notifications are unique per recipient and event, and integration skips a webhook target its ledger marked `delivered`, though it tries again for one marked `dead` or `failed`). The consumer that dead-lettered the event never recorded it, so it is the one the replay is for.
- Event corroboration: execution checks every reservation lifecycle event against the reservation's current status before acting (`created` needs `PENDING_PROVISION` or `ACTIVE`, `provision_requested` needs `PENDING_PROVISION`, `wiring_changed` needs `ACTIVE`, `cancelled`, `completed`, and `failed` need a terminal status). A replay whose reservation has moved on is acknowledged and dropped with the log action `nats_event_unverified`, so replaying an old `reservation.created` for a reservation that has since ended does nothing in execution. Replays are meaningful while the reservation still holds the status the event describes, and for terminal events whose teardown failed (see the dynamic-instance teardown section of `docs/TROUBLESHOOTING.md`).

Save the payload from `stream get` (the JSON body only) to `msg.json`, then publish it on the ORIGINAL subject, not the DLQ subject:

```bash
$NATS pub -H 'Nats-Msg-Id:{{ID}}' 'herd.reservations.created' --force-stdin < msg.json
```

### Discarding DLQ messages

If the events are stale (e.g. you've already manually fixed the state), purge the relevant DLQ subject. `--force` skips the confirmation prompt, which cannot be answered through `docker run` without a terminal:

```bash
$NATS stream purge HERD_DLQ --force --subject 'herd.reservations.dlq.execution'
$NATS stream purge HERD_DLQ --force --subject 'herd.reservations.dlq.notifications'
$NATS stream purge HERD_DLQ --force --subject 'herd.health.dlq.notifications'
$NATS stream purge HERD_DLQ --force --subject 'herd.reservations.dlq.integration'
$NATS stream purge HERD_DLQ --force --subject 'herd.health.dlq.integration'
```

### JetStream durability

Under `make up` and the gate stack, JetStream state (streams, unconsumed events, DLQ contents, consumer positions) is ephemeral by design: it lives in a directory the dev compose override points at instead of a mounted volume, so it is lost on a container recreate (image bump, `up -d --build`, `--force-recreate`, `gate-down`). A plain `docker restart` keeps it, since the container filesystem is untouched; only a recreate wipes it. This is deliberate, for clean-start test isolation: a volume there would let stale events from a previous run outlive a wiped Postgres, and a consumer pulling one would NAK it into the DLQ as unresolvable state.

Under `make prod`, JetStream state persists in the `nats-data` named volume, mounted at `/data`; a recreate (`docker compose -f docker-compose.yml up -d --force-recreate nats`, an image bump, a host reboot) keeps stream contents. The volume is purged only by `make clean-data`, not by a normal `make down`.

The transactional-outbox relay (`herd_common.outbox`) marks an event published once JetStream acks the publish, and does not replay it. So on the ephemeral dev/test path, an event a lagging or down consumer had not yet pulled before a recreate is gone; the expiration-sweep backstops (dynamic-provisioning timeout, physical-only stranded-`PENDING_PROVISION` revert) cover some of the resulting gaps, not all. This is the accepted tradeoff for the ephemeral path; `make prod`'s durable store does not have this gap.

Each stream's retention is capped by `max_age`, applied through `herd_common.jetstream.ensure_stream` (an add-or-update helper: plain `add_stream` against a stream that already exists with a different configuration errors instead of returning it, which would break boot on an upgraded-in-place stack). The cap is configurable via `NATS_STREAM_MAX_AGE_SECONDS` (see `docs/ENV_VARS.md`), read by the reservations service (`HERD_RESERVATIONS`) and the execution service (`HERD_HEALTH`, `HERD_DLQ`); 0 disables the cap. Without a cap, a durable `make prod` volume would grow unbounded.

Durable CONSUMERS get the same add-or-update treatment, for the same reason (issue #895): `herd_common.jetstream.ensure_consumer` calls `add_consumer` for every durable at boot, which updates an already-existing durable's config in place (a compatible field change, such as this issue's `ConsumerConfig.backoff` removal, updates cleanly; proven live against nats-server 2.10.29). This matters only where a durable's config can outlive a code change: under `make up` and the gate stack every durable is recreated empty along with its ephemeral stream, but under `make prod`'s persistent `nats-data` volume a durable created by an older image keeps its old config across a plain image upgrade unless something explicitly re-applies it. Before `ensure_consumer` existed, `js.pull_subscribe`'s own create-or-bind logic only created a MISSING durable and otherwise bound to whatever config the server already had, so an upgrade would have silently kept the stale config forever; no manual `nats consumer rm` step is needed for this fix to take effect on an upgrade.

## TLS certificate rotation

Certs live in `infra/traefik/certs/`. `infra/traefik/dynamic.yml` loads exactly two files there, `server-chain.crt` (the certificate, followed by any intermediates) and `server.key`, both as the certificate for clients that send SNI and as the default store's `defaultCertificate`, which is what a client that sends no SNI gets; every client that connects by IP address is one. Neither file is tracked (issue #1239), and the repository no longer ships the old root, intermediate, and leaf certificates. Because the pair is also the default certificate, a pair Traefik cannot load (missing, empty, or a key that does not match) leaves it with no default store, and it then answers no TLS handshake at all rather than falling back to its built-in certificate (log line `Error while creating certificate store ... tlsStoreName=default`; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

```
infra/traefik/certs/
  README.md            # tracked, so the directory exists in every clone
  server-chain.crt     # loaded by Traefik (certFile); written by make certs, or yours
  server.key           # loaded by Traefik (keyFile); written by make certs, or yours
```

`make certs` (which `make up`, `make dev`, `make prod`, and the gates' stack start run first, and which the CI integration job and nightly run as a step before their bare `docker compose up`) generates them when `server.key` is absent: an RSA 2048 key and a self-signed certificate valid for ten years, subject `CN=HERD dev gateway`, with the subject alternative names `DNS:localhost`, `IP:127.0.0.1`, `IP:<HERD_GATEWAY_BIND>` when that is set to an address other than `0.0.0.0` (read from the environment or `.env`, as the gateway URL is), and every entry of `CERT_SANS` (comma-separated `DNS:<name>` and `IP:<address>` entries; anything else is refused and nothing is written). It never writes over an existing key: with both files present it prints one line and runs no openssl; with the key present and `server-chain.crt` missing it issues a new self-signed certificate for the existing key. Trusting the certificate on clients is a manual step (FRESH_SETUP.md step 3 has the Ubuntu, Windows, and macOS commands); nothing in the Makefile installs it.

To rotate the generated certificate (a new name or address, or a suspected key leak):

1. `make certs CERT_FORCE=1`, with `CERT_SANS=...` if clients use names or addresses beyond localhost and the bind address. It regenerates the key and the certificate and says so.
2. Recreate Traefik so it loads them: `docker compose up -d --no-deps --force-recreate traefik` (add `-p <project>` for the gate stack). Traefik watches `dynamic.yml`, not the certificate files, so a running gateway keeps the old certificate until then. Recreate rather than restart a Traefik created before #1240 (see the restart rule in [DEVELOPMENT.md](specs/DEVELOPMENT.md#gateway-ports-guard-and-traefik)).
3. Trust the new certificate on every client again.

To use a certificate of your own (a CA-signed one, or a validity macOS accepts, at most 825 days), write its full chain to `server-chain.crt` and its key to `server.key`, then recreate Traefik as above. `make certs` leaves an existing pair alone, so it never replaces yours; do not pass `CERT_FORCE=1` afterwards. The SAN list must name every host name and IP address users browse to (for example `IP:192.0.2.10` or `DNS:lab.example.com`).

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
afterwards. With several secrets replicas, the others need no restart: each
reads a key version it has not seen from the key table on first use, and every
write encrypts under the newest unretired version in the table. Rotations are
serialized by a Postgres advisory lock, so a second rotation waits and then
moves to the next version; if one is refused anyway it answers 409 and changed
nothing, so retry it. A 503 `Secret key material is unavailable to this service`
means a replica cannot unwrap a stored key version (a missing row or a
different `SECRETS_KEK` from the other replicas); the log names the version
under action `key_version_unavailable`.

Losing `SECRETS_KEK` with no `SECRETS_KEK_PREVIOUS` window makes stored
secrets unrecoverable; there is no backdoor. Keep the KEK in whatever secret
management wraps your `.env`, and note that database backups (above) are
useless for secret recovery without the KEK from the same era, which is a
feature.

## Single sign-on recovery

Applies when `AUTH_METHOD=oidc` (ADR 0016; not yet supported for operators until its
phase 6). Every single sign-on account is linked to exactly one identity, the pair of
the ID token's `iss` (the issuer) and `sub` (the provider's id for the person), compared
as exact strings. HERD never links a sign-in to an account by email: a sign-in whose
pair is unknown is a first sign-in, and it is refused when its email or username
belongs to any account. Three superadmin routes repair the link, API only (issue
#1399): `POST /api/auth/admin/federated-identities/rewrite-issuer`,
`PUT /api/auth/users/{user_id}/federated-identity`, and
`GET /api/auth/users/{user_id}/federated-identity/candidates`. None changes an account
id, so groups, grants, reservations, and tokens are kept. The rules are IAM-OIDC-29 to
IAM-OIDC-39 in [identity-and-access.md](specs/identity-and-access.md).

The commands below run from any machine that reaches the gateway. Set `GW` to the
address users browse to (for example `GW=https://herd.example.com`); add curl's
`--cacert <file>` when the client does not trust the gateway's certificate. They
use `jq`. The config page does not offer the `OIDC_*` settings until ADR 0016 phase 6,
so change them in `.env` and recreate auth with `docker compose up -d auth` (a restart
does not re-read `.env`).

### Getting a superadmin session in OIDC mode

The routes answer only a superadmin, in any `AUTH_METHOD`. In OIDC mode the superadmin
is the local account seeded from `SUPERADMIN_EMAIL`, and password sign-in is refused
unless `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=true`. Use whichever of these you have:

- Break-glass on: `TOKEN=$(curl -sS -X POST "$GW/api/auth/login" -H 'Content-Type: application/json' -d '{"email": "<superadmin email>", "password": "<superadmin password>"}' | jq -r .access_token)`
- A superadmin API token minted earlier (`POST /api/auth/tokens` with `"role": "superadmin"`): `TOKEN=$(curl -sS -X POST "$GW/api/auth/tokens/exchange" -H 'Content-Type: application/json' -d '{"token": "<api token>"}' | jq -r .access_token)`
- A superadmin session that is still signed in (its refresh token works in every mode).

With none of them, set `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=true` in `.env`, recreate
auth, and use the first line. Set it back to `false` and recreate auth again when you
are done; auth logs a WARNING at startup and at every such sign-in while it is on. An
access token lasts `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` (default 30); fetch a new one the
same way when it expires.

### Changing the OIDC issuer

Use this when `OIDC_ISSUER` must change while the provider keeps every person's `sub`:
a renamed provider URL, a tenant URL change, a trailing slash added or dropped to match
a new discovery document, or a provider migration that preserves user ids. If the new
provider issues different `sub` values, do NOT use the rewrite: it would give each
account to whoever holds the same `sub` string at the new provider; relink people one
at a time instead (next section).

1. Get a superadmin session (above) and keep it for the whole change.
2. Dry run, from the issuer accounts were linked under (the current `OIDC_ISSUER`,
   exactly, trailing slash included) to the new one:

   ```bash
   curl -sS -X POST "$GW/api/auth/admin/federated-identities/rewrite-issuer" \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"old_issuer": "https://idp.example.com/realms/herd", "new_issuer": "https://idp.example.com/realms/herd/", "dry_run": true}'
   ```

   The answer is `{"dry_run": true, "matched": N, "conflicts": C, "rewritten": 0,
   "collisions": [...], "collisions_truncated": false}`. `matched` should be the number
   of single sign-on accounts; 0 means `old_issuer` is not the stored string.
   `conflicts` must be 0 (see "If the rewrite refuses" below when it is not).
3. Run it: the same command with `"dry_run": false`. It answers `"rewritten": N`, or
   409 with `detail` `Rewrite refused: ...; nothing was changed` and the same
   `conflicts` and `collisions` fields.
4. Change `OIDC_ISSUER` in `.env` to the new value and recreate auth:
   `docker compose up -d auth`.
5. Run the dry run of step 2 again, old issuer to new. If `matched` is above 0, run
   step 3 again: those are people who signed in for the first time between steps 3 and
   4 (below).
6. Verify: one person signs in with single sign-on and lands in their own account.

Between steps 3 and 4, while the provider still issues the old issuer, a returning
person finds no identity and is refused as a collision, but a person signing in for the
FIRST time is given a new account under the old issuer, which would lock them out after
the change; step 5 moves those. While `OIDC_ISSUER` names an issuer the provider's
discovery document does not, every sign-in answers "Single sign-on is unavailable" and
nothing is created at all. Keep the window short and tell users. Do not swap steps 3
and 4: with the new issuer configured and the rewrite not yet run, a returning person
whose provider email changed since their account was created is given a second account
under the new issuer, whose identity then makes the rewrite refuse.

If the rewrite refuses (or a dry run shows `conflicts`): each entry of `collisions`
names the two accounts of one person, `old_issuer_account_id` (the account linked under
the old issuer, which holds the person's groups, grants, and reservations) and
`new_issuer_account_id` (a second account created under the new issuer). Keep the first.
For each pair, retire the second account's identity, deactivate it, then run step 3
again:

```bash
curl -sS -X PUT "$GW/api/auth/users/<new_issuer_account_id>/federated-identity" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"issuer": "urn:herd:retired", "subject": "<new_issuer_account_id>"}'
curl -sS -X POST "$GW/api/auth/users/<new_issuer_account_id>/deactivate" -H "Authorization: Bearer $TOKEN"
```

The relink replaces the second account's identity with (`urn:herd:retired`, its own
account id). `urn:herd:retired` is a reserved issuer no accepted ID token ever carries
(a token's `iss` must equal `OIDC_ISSUER`, and auth accepts only an http or https URL
there), so nobody can sign in to that account any more, and
the person's `sub` under the new issuer is freed; the rewrite then gives that pair to the
first account. Never reactivate a retired account without relinking it first. Anything the person created in the second account stays there (nothing
moves ownership between accounts, and nothing deletes an account). When
`collisions_truncated` is true, clear the listed pairs and dry-run again for the rest.

### A user's first single sign-on login is refused as a collision

The person sees "Single sign-on failed. If this continues, contact an administrator."
Auth's log says why, without any claim value:

```bash
docker compose logs --no-log-prefix auth | jq -R 'fromjson? | select(.action == "login_failure" and .source == "oidc") | {timestamp, reason, user_id}'
```

`email_taken` for a person who has signed in with single sign-on before means their
provider now names them differently. If `OIDC_ISSUER` changed, follow the previous
section. Otherwise the provider reissued their `sub` (their account there was deleted
and recreated, or the provider was replaced), and their refused sign-in was normally
kept as a relink candidate against their HERD account.

A candidate proves only that SOMEONE signed in at the provider with an account carrying
that email; anyone who can do that can plant one. A token with no `email_verified`
claim is accepted and captured (only an explicit `false` is refused, before the
collision check), and Microsoft Entra ID normally omits the claim, so any provider
account carrying the email can plant a candidate. The person's own attempt may also
leave NO candidate: their provider email may no longer equal the account's, the attempt
may be refused as `username_invalid` or `email_unverified` before the collision check,
or the capture may fail (logged as `oidc_relink_candidate_capture_failed`); then the
only candidate in the minute could be someone else's. The checks below bind a candidate
to the person by time alone, which is why every precondition in step 4 is required. The
relink is the security step:

1. Find the account id: `curl -sS "$GW/api/auth/users?limit=500" -H "Authorization: Bearer $TOKEN" | jq '.items[] | select(.email == "<their email>") | .id'` (page with `skip` past 500 accounts).
2. Read its candidates and note `captured_total`:

   ```bash
   curl -sS "$GW/api/auth/users/<account id>/federated-identity/candidates" -H "Authorization: Bearer $TOKEN"
   ```

   The answer is `{"user_id", "captured_total", "candidates": [{"id", "issuer",
   "fingerprint", "created_at", "last_seen_at", "expires_at", "attempts"}]}`, newest
   first. A candidate lives 24 hours; the subject is never shown, only a fingerprint.
3. Agree a minute with the person over a channel that AUTHENTICATES them, chosen by
   you, not by them: in person with identification, or a call YOU place to a number
   already on file. A call the person places to a number of their choosing is not
   enough. Have them try to sign in once at that minute.
4. Read the candidates again. Relink only when ALL of these hold:
   - `captured_total` went up by exactly one, exactly one live candidate exists, and
     its `created_at` falls in the agreed minute;
   - at the provider you confirmed that exactly one provider account carries the email,
     and that it is the person's;
   - the person confirms, over the same authenticated channel, that they saw the
     sign-in failure page at that time;
   - auth's log has no `oidc_relink_candidate_capture_failed` line in the window
     (`docker compose logs --no-log-prefix auth | jq -R 'fromjson? | select(.action == "oidc_relink_candidate_capture_failed")'`).

   ```bash
   curl -sS -X PUT "$GW/api/auth/users/<account id>/federated-identity" \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"candidate_id": "<candidate id>"}'
   ```

   The answer is the identity, `{user_id, issuer, subject, created_at, last_login_at}`;
   the relink discards the account's other candidates.
5. The person signs in again and lands in their account. Their old `sub` no longer
   signs in.

When more than one candidate is live, or `captured_total` grew by more than one, or the
only candidate's time does not match, or any other precondition fails, do NOT relink:
someone else may be signing in with an account carrying this email. Investigate at the
provider (which accounts hold the email, who created them). A candidate that is not the
person's is harmless as long as nobody relinks to it.

Any one provider account carrying the email can block this path indefinitely: it can
re-plant every 24 hours so that a second candidate is always live. A route to clear
candidates would not help, since re-planting is immediate. The way out is the typed-pair
relink below, with a subject read at the provider, or removing the rogue provider
account and repeating the agreed-minute check after its candidates expire.

Alternative, when the provider shows the person's `sub` (Keycloak: the user's ID;
Okta: the user's id; Dex: the unpadded base64url encoding of a protobuf message whose
field 1 is the user id and field 2 the connector id, as `dex_subject` in
`tests/integration/test_oidc_auth.py` computes it): relink by the typed pair, with the
issuer exactly as `OIDC_ISSUER` holds it:

```bash
curl -sS -X PUT "$GW/api/auth/users/<account id>/federated-identity" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"issuer": "<OIDC_ISSUER value>", "subject": "<new sub>"}'
```

To check that a typed `sub` is the same as a candidate, compare the candidate's
`fingerprint` with the pair's (next section). Microsoft Entra ID issues a different
`sub` per application and its portal does not show it; use the candidate path there.

The relink answers 404 `Relink candidate not found for this account` for a candidate
that expired or belongs to another account, 409 `Only a single sign-on account can be
linked to an identity` for a local or LDAP account, and 409 `This identity is already
linked to another account` when another account holds that pair (a second account: see
"If the rewrite refuses" above). Check the pair before every relink: whoever signs in
with it gets the account.

`email_taken` or `username_taken` for a person who never signed in with single sign-on
means a local or LDAP account created before the switch holds that email or username.
HERD does not link or convert such an account (ADR 0016, Out of scope), no candidate is
kept for it, and no route changes that. The person can be given a different email or
username at the provider, so the first sign-in provisions a new account; carrying the
old account's groups and ownership over is a manual step.

### Sessions after a relink or rewrite

Neither route revokes a session. A sign-in already in flight when a relink or rewrite
commits is refused (IAM-OIDC-39), but tokens issued before it keep working for their
lifetimes: an access token for `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` (default 30) plus 10
seconds, and a refresh token for up to `AUTH_REFRESH_TOKEN_EXPIRE_DAYS` (default 7)
through rotation. If a relink may have let the wrong person in, deactivate the account
(`POST /api/auth/users/<id>/deactivate`): while it is inactive, auth refuses its
refresh tokens and every auth route. Deactivation revokes nothing, though: reactivating
the account lets every unexpired refresh token work again. Keep it deactivated until
`AUTH_REFRESH_TOKEN_EXPIRE_DAYS` have passed since the doubtful relink, then reactivate
it and relink it correctly. Other services trust an access token until it expires
whatever auth decides.

### Comparing fingerprints

Audit lines never hold an issuer or a subject. A relink line (`action`
`oidc_identity_relink`) carries `old_fingerprint` and `new_fingerprint`, the
fingerprints of the pairs before and after; a rewrite line (`oidc_issuer_rewrite`)
carries `old_issuer_fingerprint` and `new_issuer_fingerprint`; a candidate's
`fingerprint` is its pair's. To learn whether a suspect pair or issuer is the one in a
line, compute its fingerprint inside the auth container, which holds the key:

```bash
docker compose exec auth python -c "import sys; from app.services.federated_identity_service import identity_fingerprint; print(identity_fingerprint(*sys.argv[1:]))" '<issuer>' '<subject>'
```

Pass one argument for an issuer and two for a pair. A fingerprint is HMAC-SHA256,
truncated to 16 hex characters, under a key derived from `AUTH_SECRET_KEY`, so only a
holder of that secret can compute or test one; rotating `AUTH_SECRET_KEY` changes every
fingerprint, and lines from before a rotation cannot be compared with values computed
after it.

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
  jq -R 'fromjson? | select(.action == "nats_dlq_exhausted" or .action == "nats_dlq_permanent" or .action == "nats_poison_message")'

# Slow requests (over 1s)
docker compose logs --no-log-prefix --tail 5000 | \
  jq -R 'fromjson? | select((.duration_ms // 0) > 1000)'

# The wiring retry tick's per-tick line from execution
docker compose logs execution --no-log-prefix --tail 2000 | \
  jq -R 'fromjson? | select(.action == "wiring_retry_tick")'
```

The `wiring_retry_tick` line carries these fields. `rows_due` is the rows the tick selected this time, at most `WIRING_RETRY_BATCH_SIZE` per ledger. `skipped_not_retryable` and `skipped_frozen` count selected rows that the tick's two Python checks set aside before any drive (a pinned not-retryable reason; a build row of a frozen reservation); the selects apply the same two rules, so this happens only to a reservation frozen between the select and the check, and both are normally 0. Every other selected row is handed to its layer's retry, and `rows_retried` (and `l1_rows_retried`, `l2_rows_retried`, `l3_rows_retried`) counts those rows, whether or not a driver call was made for them; each lands in exactly one of `reconnected`, `released`, `superseded` (settled with no driver call), `in_progress` (the manual retry held the row), or `still_failed`. `still_failed` therefore includes rows the tick selected and left unchanged with no driver call, for example build rows whose intent could not be read while cabling or inventory was unreachable, rows a stale-intent check parked for release, and the rows counted in `skipped_upstream`. `skipped_upstream` counts the rows, across the three ledgers, that the tick left unchanged because an upstream read (a switch or its template from inventory, or for L2 the fabric lookup) failed inside their reservation's apply; only that reservation is passed over, the rest of the batch is driven, and each such reservation gets one warning with action `wiring_retry_reservation_upstream_failed` (`reservation_id`, `layer`, `error_class`, `rows_unchanged`) (issue #1204). While the L2 retry releases the allocations of rows it found superseded, an inventory read (the switch or its template) that fails is caught inside that release for the one allocation it concerns and logged with action `vlan_delete_upstream_failed` (issue #1209), and a failed cabling fabric lookup there is logged with action `vlan_delete_unjudged` (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for both); the tick keeps a backstop around the release, so an error that still escaped it would leave the outcomes standing and log one warning with action `wiring_retry_superseded_release_upstream_failed` (`layer`, `error_class`, `allocation_count`). A row deleted before the read-back is counted in `rows_due` only, and so are all of a layer's rows when that layer's retry raises anything those catches do not take (logged as `wiring retry tick: L<n> reattempt failed; rows stay FAILED`); `skipped_upstream` keeps the rows it had already counted for that layer. `excluded_not_retryable` and `excluded_frozen` count the `FAILED` rows under the attempts cap that the selects leave out, across the three wiring ledgers: rows with a pinned not-retryable reason, and build rows of ended (frozen) reservations. They are a standing population, repeated on every tick, and they no longer take up the batch (issue #1190); `null` means the count could not be read on that tick. The line is logged at `INFO` while `rows_due` or an excluded count is above zero and at `DEBUG` otherwise. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for what to do about excluded rows.

Configure verbosity per service via `LOG_LEVEL` (default `INFO`; use `DEBUG` for investigations and remember to turn it back down).

Log rotation (issue #1352): every service in every compose file the repository ships uses Docker's `json-file` driver with `max-size: "10m"` and `max-file: "5"` (the `x-logging` anchor at the top of each file), so each container keeps at most five files of 10 MB, 50 MB in all, and `docker compose logs` reaches back only that far. Before #1352 no service set a limit, and a container's log grew until the disk filled unless the host's `daemon.json` set one. A changed logging block applies when the container is recreated (`docker compose up -d`), not on a restart; Traefik's access log (`--accessLog=true`, one line per request) is the fastest to rotate.

## Healthchecks and monitoring

- `docker compose ps` shows per-container health. Unhealthy containers need a logs inspection.
- Every backend service exposes `GET /health`, reachable through the gateway at `https://<host>/api/<svc>/health` (e.g. `curl -k https://localhost/api/auth/health`, or `curl -k "$(make -s gateway-url)/api/auth/health"` when `HERD_GATEWAY_BIND` binds the gateway to one address and `localhost` is refused). From inside a container use `docker compose exec <svc> python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').status)"`; the service images do not ship curl.
- `/health` is a liveness answer only: it returns a constant 200 `{"status": "ok", "service": "<name>"}` and checks no database, broker, or peer service, so it stays 200 while Postgres or NATS is down. The compose healthchecks call it, so a container marked healthy in `docker compose ps` is serving HTTP, nothing more; read the service logs for dependency failures. There is no readiness route.
- Live OpenAPI docs: `https://<host>/api/<service>/docs` (auth required for most endpoints).

There is no built-in Prometheus/Grafana stack; roll your own based on the JSON logs and `/health` endpoints.

## Scaling and sizing

- Postgres: single node by default. Horizontal scaling would require adding a pooler (PgBouncer) and replication; not in the default compose.
- Database connections (issue #1352): each of the eleven database services opens at most `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections, 5 + 10 by default, so 11 x (5 + 10) = 165 against the compose Postgres `max_connections=200`, leaving 35 for psql, `make migrate`, and test harnesses. Every extra execution replica adds another 15, so three extra replicas (165 + 45 = 210) exceed 200. Past `max_connections` a new connection fails with `too many clients already`, so keep 11 x (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) plus 15 per extra replica under 200, by lowering `DB_MAX_OVERFLOW` or raising `max_connections` in the postgres `command` of `docker-compose.yml`. `max_connections` takes effect only when the postgres container is recreated (`docker compose up -d postgres`). `docs/ENV_VARS.md` has the variables.
- Services are stateless; scale by bumping `replicas` in compose and putting Traefik or another load balancer in front. Today they run 1x each.
- Health polling scales horizontally (issue #24): run extra execution replicas with `EXECUTION_POLLER_ONLY=true` (background machinery only, no API routers) and set `HEALTH_POLL_SCHEDULER_ENABLED=false` on the API replicas for a clean split. Concurrent pollers against one schema are safe: due rows are claimed via `SELECT ... FOR UPDATE SKIP LOCKED` plus a conditional update, so two replicas never poll the same device. Bound each replica with `HEALTH_POLL_BATCH_SIZE` and `HEALTH_POLL_MAX_CONCURRENCY`, and watch the per-tick `health_tick` log (`rows_due` versus `polls_fired`) to decide when to add a replica.
- NATS JetStream is a single node. Under `make prod` message persistence is in the `nats-data` volume; the dev and gate stacks keep JetStream state in an unmounted directory, so it does not survive a recreate (see JetStream durability above). For HA, cluster three NATS nodes.
- ai-orchestrator runs with `mem_limit: 2048m` and `restart: unless-stopped` (issue #1202). Each uploaded PDF is read in a child process capped by the kernel at 512 MiB of address space, 20 CPU seconds, and 30 seconds of wall time, at most two at once and one per user; every other upload type is read inside the service, at most two at once. The limit is the sum of two PDF children (1024 MiB), two in-process reads at their measured worst (384 MiB), the uploads of the up to 20 requests holding or waiting for a reader (500 MiB), and the service itself (about 128 MiB). A refused read is a 400 to the user (a 503 when the reader is busy) and one `WARNING` log line, `pdf read refused` or `file read refused`, with an `outcome` field (`memory`, `killed`, `cpu`, `deadline`, `parse-error`, `unavailable`, `busy`, or, for the 500 cases, `start-failed`, `limits-failed`, `malformed`); the file's content is never logged. Many `busy` lines mean more uploads than two readers serve (or one user sending PDFs in parallel, which is refused); many `start-failed` or `limits-failed` lines mean the container cannot start or limit a child (a seccomp or ulimit policy refusing `setrlimit`, or no `pidfd_open`). A PDF read refused at a resource limit costs the user 20,000 tokens of `AI_DAILY_TOKEN_QUOTA` when a quota is set. If the container reaches its memory limit, the kernel kills a process in it; a killed reader answers the "ended by the kernel" 400, a killed service restarts under the restart policy (`docker compose ps` shows the restart count). The limits are constants, not settings (`docs/specs/ai-features.md`, AI-UPLOAD-19, AI-UPLOAD-22, and AI-UPLOAD-25).
- Auth checks passwords on its own pool of bcrypt worker threads, `AUTH_PASSWORD_HASH_THREADS` (0 by default: half the CPUs the auth container may use, between 1 and 4, counting a cgroup CPU quota rounded up when one can be read; a set value above those CPUs is lowered to them with a WARNING `password_hash_threads_lowered`, and a container under a CPU quota that auth cannot read may set the value explicitly); each thread takes up to one core while it hashes, about a fifth of a second per login at cost 12, so the pool answers roughly five logins a second per thread, and every other auth route keeps answering however busy the pool is. A login or registration that waits 5 seconds without a free thread is answered 503 `Too many password checks in progress; try again shortly` with `Retry-After: 5` (the login page shows `Could not reach HERD. Try again in a moment.`); many such 503s in the access log mean more logins than the pool clears, from many addresses at once (one address is held to its own allowance, below). Raise the thread count if the host has idle cores, and look for a flood in the access log. Separately, each source address may make `AUTH_LOGIN_MAX_ATTEMPTS` password attempts at once (60 by default), refilling evenly over `AUTH_LOGIN_WINDOW_SECONDS` (60): successes count, an admin-token registration does not, and past the allowance the answer is 429 `Too many attempts; try again later` with `Retry-After`. A source that starts being refused logs one WARNING with action `auth_login_limited` and its address (`docker compose logs auth --no-log-prefix | jq -R 'fromjson? | select(.action == "auth_login_limited")'`). The source is the address Traefik saw, so users behind one NAT or proxy share an allowance; raise the two settings for such a site. The counts live in the auth process (a restart clears them; replicas count separately) (`docs/specs/identity-and-access.md`, IAM-LOGIN-7 and IAM-HASH-7).
- Execution service's driver cache (`/data/driver-cache`) grows with driver churn; size the volume accordingly. Each driver is extracted into its own directory, `<driver id>-<random hex>` (issue #1097); a new package version replaces the old directory on its next load.

## Disaster scenarios

### Postgres is down

- Every service with a database (all but config) keeps running and keeps answering `GET /health` with 200, since that route checks nothing; every request that needs the database fails until Postgres is back.
- A service that starts or restarts while Postgres is down fails during startup (its schema check cannot connect) and exits; its `restart: unless-stopped` policy restarts it until Postgres answers.
- Config service keeps running (no DB).
- Recover: bring Postgres back up, let the services auto-recover. No manual intervention needed for the app; replay any lost transactions from backups if data was corrupted.

### NATS is down

- Reservations service still accepts writes. Lifecycle events are written to the transactional outbox in the same transaction as the state change (issue #21), so nothing is lost while NATS is unreachable; the relay just cannot publish them yet.
- Execution service can't consume events during the outage, so L1/L2 driver operations are NOT triggered yet; a reservation goes `ACTIVE` but its devices are not configured on real hardware until events flow again. Health-transition events are likewise buffered in the execution outbox.
- Recover: bring NATS up. The per-service outbox relays reconnect and drain their unpublished rows on the next tick after NATS is reachable again (at most `OUTBOX_RELAY_TICK_SECONDS` later; the issue #682 wake path is deliberately ignored while the relay is waiting out an outage), so reservation and health events are delivered once (not lost), just delayed. No manual catch-up event is needed for these streams.
- A service that STARTED while NATS was down is different: its first connect gives up after a few tries (about 10 seconds; issue #1083), it logs `NATS unavailable at ...` (reservations) or `Failed to connect to NATS; ...` (execution, notifications, integration), finishes starting, and stays without NATS until it is restarted. After NATS is back, restart every service that logged one of those lines (`docker compose restart <svc>`); its outbox rows and the durable consumers' unacknowledged messages are kept, so nothing is lost. A connection that was established before the outage reconnects by itself, without limit.
- Reservations can instead log `JetStream stream HERD_RESERVATIONS could not be created or updated at ...` (RES-EVENT-5): the broker answered but the stream create or update failed. That service keeps its connection and needs no restart; its relay publishes as soon as the stream exists (execution's consumer also creates it), and until then its events wait in the outbox and are retried every tick.

### Config volume is wiped

A missing `config.json` is not an error for the app services: every setting falls back to the environment, so a stack whose `.env` carries the required values keeps running, and a running service keeps the values it read at startup. What is lost:

- Values saved only through the config UI. Each service picks up the environment value for those keys at its next restart; restore the volume from a backup (see Backup and restore) to get them back.
- The login page's configured state. It shows the setup banner until `config.json` exists again. The config service writes it from the environment at its next start when every required variable is set and the access-token settings pass its check (`docker compose restart config`); otherwise go through the wrench-icon flow (same flow as initial setup).
- The config-page password file. The config service generates and logs a new password on its next use unless `CONFIG_ADMIN_PASSWORD` is set.

### Main commit accidentally force-pushed

Always avoid. If it happened, restore from the most recent verified backup. All GitHub operations (push, merge, force-push) are performed by the repo owner directly, never by automation.

## Useful cross-references

- [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md) for app-level admin tasks (device groups, templates, promotions).
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for user-reported symptoms and their root causes.
- [ENV_VARS.md](ENV_VARS.md) for every env var.
- [ROLES.md](ROLES.md) for the endpoint-level permission matrix.
- [ARCHITECTURE.md](ARCHITECTURE.md) for the architecture overview.
