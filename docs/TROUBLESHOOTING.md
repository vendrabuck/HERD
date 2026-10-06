# Troubleshooting

Common failure modes and how to diagnose them. Organized by what the user sees.

## Login and setup

### Login form inputs are disabled with a "not configured" banner

`config.json` has not been written to the `herd-config` volume, so the login page is gating behind the setup flow. Two fixes:

- Put every required value in `.env` (see `.env.example`) and `make restart`. The config service's first-start auto-bootstrap will write `config.json` from the env on boot, and the login form will enable. Check `docker compose logs config` for a message like `Config bootstrapped from environment` or a warning naming the missing var.
- Or click the wrench icon on the login page, log in with the config-page password (set `CONFIG_ADMIN_PASSWORD`, or read the one-time password from `docker compose logs config`), fill in required settings, and click **Save and Restart**.

See [OPERATIONS.md](OPERATIONS.md#config-service-first-run).

### Services crash-loop on startup

Most common cause: `AUTH_SECRET_KEY` is missing or empty. Either put it in `.env` or complete the config-service setup.

Second most common: config service started but `config.json` was never written. Same fix as above.

Check with `make logs` or `docker compose logs <service>`; the startup error message names the missing setting.

### Login succeeds but every API call returns 401

Either:
- The `AUTH_SECRET_KEY` changed after tokens were issued. Users need to log in again.
- Clock skew between the client and the backend is larger than the token lifetime.
- The bearer token expired (30 min default). The auth client should refresh automatically; if it's stuck, clear local storage and log in fresh.

### LDAP login fails immediately

When `AUTH_METHOD=ldap` (see [ENV_VARS.md](ENV_VARS.md#ldap--active-directory)), a 401 on every login typically means one of:

- Service-account bind is failing: double-check `LDAP_BIND_DN` and `LDAP_BIND_PASSWORD`. The auth service logs `ldap_bind_failure` with the DN that refused.
- User not found under `LDAP_USER_BASE_DN`, or the `LDAP_USER_FILTER` does not match (e.g. the directory uses `uid=` not `sAMAccountName=`). The log line is `ldap_user_not_found`.
- TLS handshake fails. Start with `LDAP_USE_TLS=false` and a plain `ldap://` URL against a lab server to confirm the rest of the config, then re-enable TLS once the DN / filter are known good.
- The directory entry has no `mail` attribute (or whatever `LDAP_EMAIL_ATTRIBUTE` is set to). HERD refuses to provision a user it cannot address by email; the log line is `ldap_missing_email`.
- The email returned by the directory collides with an existing local account. Either delete the local row or set `AUTH_METHOD=local` temporarily.

### `/register` returns 409 "Local registration is disabled"

The stack is in LDAP mode (`AUTH_METHOD=ldap`). Accounts are provisioned on the first successful directory bind, not via the register form. Either log in with your directory credentials or ask the admin to switch `AUTH_METHOD=local`.

## Inventory and visibility

### Empty device list

Happens to non-admin users. Three possibilities, in order of likelihood:

1. **No device groups assigned to your user group.** Ask an admin; the fix is adding a user-group permission on the device group in question. See [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md#device-groups-visibility).
2. **Inventory or auth service is returning 503** (after B4, upstream-failure is surfaced rather than silently hidden; the inventory helpers map an unreachable or erroring auth service to 503, the cross-service convention for an unavailable dependency). Check `https://<host>/api/inventory/device-groups/visible-devices?user_id=<self>` in browser devtools network tab; if it's returning 503, check `make logs` for errors in inventory or auth.
3. **All devices in your groups are currently reserved** and "Show reserved" is off in the palette. Toggle the filter.

To tell (1) from (2): an admin should see the same devices you cannot; if the admin also gets 503 on the helper endpoints, it's (2).

### Deleting a device is refused with `409 device_in_use`, `409 device_cabled`, or `503`

Inventory refuses an admin device delete while wiring still depends on the device (issues #900 and #940). There is no force flag.

- `409` with `"error": "device_in_use"`: a live reservation (PENDING, PENDING_PROVISION, or ACTIVE) holds the device as a booked member, or a non-archived reservation fork routes through it as a transit hop. `reservation_ids` is the union of both; `transit_reservation_ids` is the subset that holds it only as a transit hop. Cancel those reservations or let them end, then retry. A delete just after a cancel can still precede execution's asynchronous teardown, so wait a moment.
- `409` with `"error": "device_cabled"` (checked after the one above): a cabling connection still names the device on either end. `connection_count` is the true total and `connection_ids` a sample of up to 10. Delete the cables first (Administration, then Connections), then delete the device.
- `503` "Could not verify device is not in use": the reservations or cabling service could not be asked, or cabling answered without the connection fields (an older cabling image). The delete fails closed; check both services' health, and after an upgrade confirm cabling was rebuilt.

### Palette is empty but Inventory list has devices

The equipment palette only shows DUTs (`Management` connection type) that aren't already on the canvas. If your inventory is all infrastructure switches, nothing appears in the palette by design.

## Reservations

### Reservation is `FAILED`

The row is audit-only: no devices were actually reserved. The reservation service tried to flip exclusive devices to `RESERVED` in inventory, the call failed all retries (default 3 attempts, 0.5s initial backoff), so the row landed in `FAILED` and the NATS `reservation.created` event was suppressed (so downstream provisioning never ran).

Diagnose:

- Check reservations service logs for `action=reservation_provision_failed` with the reservation id.
- Inspect inventory service health at `https://<host>/api/inventory/devices/{id}`.
- Check that `INTERNAL_API_TOKEN` is set consistently across services (a mismatch here is the most common cause).

Recover: create a new reservation for the same devices and window. Keep the `FAILED` row for audit or cancel it to hide it.

A second cause (issue #898): a `PENDING` reservation whose whole window had already elapsed when the expiration sweep reached it (the sweep was down, or the window was very short) is moved to `FAILED` without any inventory or fork call, and the reservations log carries `action=reservation_window_elapsed`. Nothing was provisioned; book a new window.

### `503 Failed to reserve devices in inventory after retries`

Same root cause as `FAILED` above, surfaced at the API layer instead of landing a `FAILED` row. The create call raised after retries exhausted; the row was persisted as `FAILED` before the raise. Same fix.

### `409 Time conflict: devices X already reserved`

Exclusive device is already held by another reservation during the window you requested. Options:

- Pick a different window (the calendar makes this easy).
- Cancel the conflicting reservation if you own it.
- Pick a different device.

`PENDING_PROVISION` reservations also count as conflicts (this is the B2 race-close), so if someone else is mid-creating on the same device, you'll get 409 until their provisioning finishes or fails.

### `422 The following devices are not available`

A device's status is not compatible with the reservation:

- **Exclusive devices** must be `AVAILABLE`. If one shows `RESERVED`, it's held by another reservation. If it shows `OFFLINE` or `MAINTENANCE`, an admin marked it so.
- **Non-exclusive devices** can be `AVAILABLE` or `RESERVED`, but not `OFFLINE` or `MAINTENANCE`.

Check the device's current status in inventory. If `OFFLINE` or `MAINTENANCE` and it shouldn't be, ask an admin.

### `422 All devices must share the same topology type`

You mixed `PHYSICAL` and `CLOUD` devices in one reservation. Split into two reservations, one per topology type.

### Reservation stuck in `PENDING`

A `PENDING` reservation activates automatically when its start time passes. If it's past start time and still `PENDING`:

- The expiration task runs every 60 seconds; allow up to a minute of drift.
- If it's been much longer, the expiration task may have died. Check reservations service logs for the expiration loop; restart the service to recover.

## Topology editor

### Can't drop the device on the canvas

You're mixing `PHYSICAL` and `CLOUD` devices. Start a separate topology for the other topology type.

### L1 edge shows red "no path"

No physical cabling path between the two DUTs through known L1 switches. Either:

- Physical cabling really doesn't connect them (use a different path).
- The relevant L1 switches and cabling entries are missing from inventory; ask an admin to add them.

### L1/L2 edge shows "uncabled port"

One or both chosen ports have no recorded physical cabling. Pick a different port or ask an admin to record the cable.

### L3 validation or fork save fails with `503 l3_config_unavailable`

The L3 validation pass (ADR 0014) fetches each L3-carrying switch's latest
inventory config version, up to 8 concurrently, and must finish the whole pass
within a 12 second deadline. At a typical 4 second inventory response time,
that budget covers roughly three concurrent rounds, about 24 L3 switches, before
the deadline trips and the pass fails closed with `l3_config_unavailable`. A
topology with more L3 switches than that, or against a slower inventory,
refuses validation and save on this timeout rather than validating a partial
result. Retry once inventory latency recovers; there is no per-request
workaround, since the deadline exists so a slow inventory never lets an
unverified route land.

## AI topology generation

### `409 Inventory shifted during generation`

Between the LLM's proposal and the device resolver's fetch, a device the LLM wanted became unavailable (reserved, status changed, or missing). Regenerate.

### `422 topology_unconnectable` during AI generation

After the repair budget (`AI_GENERATE_MAX_REPAIRS`, default 2) the proposal still names role pairs that no available devices can connect (issue #828). The body lists each pair by role and template. The lab's cabling has no path between any available device of the two templates: add the missing cables or devices, free a reserved device, or regenerate with a prompt that steers the model to other templates. See [AI_GENERATE.md](AI_GENERATE.md).

### `422 topology_unwireable` or `503` during AI commit

Commit re-validates the saved canvas against cabling before the reservation is created (issue #827). A `422 topology_unwireable` names each bad edge by role; the new topology is deleted, so nothing is left behind. A `503` means cabling could not answer, and the commit fails closed; check cabling's health and retry. See [AI_GENERATE.md](AI_GENERATE.md#commit-time-wireability-check).

### `422` during AI commit with a config-validation error

The LLM produced a `config` key that isn't on the allowlist (`vlan`, `ip`, `hostname`, `description`), or put a config on a non-`Management` connection type. Either accept the proposal without `apply_configs`, or regenerate with a prompt that steers the LLM away from that config.

See [AI_GENERATE.md](AI_GENERATE.md#device-configs-the-allowlist).

### `config_results` rows show `status: failed, error: admin required`

The `/execute` endpoint requires admin for most actions, but allows a non-admin to run the `configure` action when they hold an ACL `manage` grant on the device. A non-admin commit with `apply_configs=true` that lacks `manage` on a device will see a failed config row for it. This is by design: the topology and reservation are still created successfully; only the config step failed.

Fix: either uncheck **Apply device configs** in the commit dialog, or have an admin run the configs separately via the execution service.

### AI button is missing from the topology editor

By design: when `AI_API_KEY` is blank, `GET /api/ai/status` returns `{"enabled": false}` and the frontend hides the **Use AI** button entirely. To re-enable the feature, set the key in `.env` or via the config UI and restart the ai-orchestrator container (`make restart`). You can check the current state by hitting `/api/ai/status` directly; it is unauthenticated.

## Drivers and execution

### Driver upload fails with `422`

File type or size issue. Allowed: `.zip` or `.tar.gz`, max 10 MB. Check filename and size.

### Driver upload fails with `409`

The driver `name` is already taken. Pick a different name; names are unique.

### Execution run status `TIMEOUT`

The driver method took longer than the configured timeout (`execution_timeout_seconds`, default 30s for non-status methods; `status_check_timeout_seconds` for status). Either optimize the driver or raise the timeout.

### Execution run status `FAILED` with `Driver class not found`

The driver package validation failed. Confirm `driver.py` exists in the package root and defines a class named `Driver` with the required methods for its connection type. See [DRIVERS.md](DRIVERS.md).

### Log action `dynamic_instance_keyed_destroy_failed`

Meaning: a reservation with a dynamic instance ended, and the instance's ledger row had
no `instance_ref` (its `create_instance` failed, timed out, or lost its process before it
reported one, so the create may still have left a VM behind). Teardown ran the KEYED
destroy, `destroy_instance(instance_ref=None)` with `HERD_request_id` in the context, and
it did not succeed. The ledger row stays `CREATING` (or `ACTIVE`) as a "may still exist"
record instead of being retired, and the event is acknowledged. The log line carries
`request_id`, `reservation_id`, `ledger_status`, and a fixed `reason`:

- `destroy_failed`: the recipe returned `{"success": false}` or raised. Most often a
  recipe written before issue #937 that requires an `instance_ref`; the destroy
  ExecutionRun row for the reservation (`GET /api/execution/runs?reservation_id=...`)
  shows the exception class, e.g. `driver raised AttributeError`, and records
  `method_kwargs` `{"instance_ref": null}`.
- `login_failed`: the recipe could not log in to the hypervisor.
- `recipe_load_failed`: the recipe package would not load.
- `recipe_config_missing`: the template, hypervisor, or secret is gone (404).

Find the affected rows and log lines:

```bash
docker compose logs execution | grep dynamic_instance_keyed_destroy_failed
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT request_id, reservation_id, status, hypervisor_id, created_at
     FROM execution.dynamic_instances
    WHERE instance_ref IS NULL AND status <> 'DESTROYED';"
```

Find the instance: the Hypervisor contract requires `create_instance` to name the
instance from `HERD_request_id` (see [DRIVERS.md](DRIVERS.md), Determinism contract), so
search the hypervisor named by `hypervisor_id` for the name the recipe derives from the
row's `request_id` (the recipe source shows the derivation; the checked-in mock uses
`mock-vm-<request_id>`). Delete it by hand if it exists.

Then fix the cause (upgrade the recipe so it accepts `instance_ref=None` and resolves the
instance by its derived name, or restore the missing config) and retry the teardown by
re-publishing the reservation's terminal event. The original payload stays in the
reservations outbox for 7 days after publish. Two ids matter, and they are handled
differently:

- Keep the payload's `event_id` unchanged. Notifications and integration's webhook
  consumer key their dedupe on it, so the replay adds no second bell entry and no second
  webhook delivery. Execution does not skip a terminal event it has seen before (its
  dedupe key only skips driver actions that already succeeded, and the dynamic teardown
  records none for a failed destroy), so it runs teardown, and the keyed destroy, again.
  This is the opposite of the issue #611 advice for test publishes, which want a fresh
  `event_id` precisely so the consumers do NOT dedupe them.
- Give the message a FRESH `Nats-Msg-Id` header. JetStream drops a publish whose
  `Nats-Msg-Id` it saw within the stream's duplicate window (the default 2 minutes; HERD
  sets none), and the outbox relay used the `event_id` as the original message id, so
  reusing it can be swallowed by the broker. The `{{ID}}` template below generates a
  unique id per publish.

Before replaying, check that no newer reservation holds the reservation's instance
devices. The reservations service releases an instance device to `AVAILABLE` when the
reservation ends, independently of execution's teardown, so the device can be booked
again in the meantime, and the teardown a replay drives deletes the device through
inventory's internal delete, which checks no reservation. For each ledger row of the
reservation that has a `device_id`, list the live reservations holding that device:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT r.id, r.status FROM reservations.reservations r
     JOIN reservations.reservation_devices d ON d.reservation_id = r.id
    WHERE d.device_id = '<device_id>'
      AND r.status IN ('PENDING', 'PENDING_PROVISION', 'ACTIVE');"
```

If any row comes back, do not replay; destroy the instance by hand instead.

The `nats:2.10-alpine` service image ships only `nats-server`, no `nats` CLI, so publish
from a one-off `natsio/nats-box` container on the stack's network
(`<compose project>_herd-net`, for example `herd-public_herd-net`):

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -c \
  "SELECT subject FROM reservations.outbox
    WHERE payload->>'reservation_id' = '<reservation_id>'
      AND subject IN ('herd.reservations.cancelled', 'herd.reservations.completed',
                      'herd.reservations.failed')
    ORDER BY created_at DESC LIMIT 1;"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -c \
  "SELECT payload::text FROM reservations.outbox
    WHERE payload->>'reservation_id' = '<reservation_id>' AND subject = '<subject>'
    ORDER BY created_at DESC LIMIT 1;" > event.json
docker run -i --rm --network <compose project>_herd-net natsio/nats-box:0.14.5 \
  nats --server nats://nats:4222 pub -H 'Nats-Msg-Id:{{ID}}' '<subject>' --force-stdin \
  < event.json
```

If the outbox row has been pruned, there is no faithful payload to replay; delete the
instance by hand as above and leave the row as the record that it existed. A row the keyed
destroy retires shows `status = 'DESTROYED'`. Nothing retries a failed keyed destroy on a
timer: only a redelivery of the terminal event (the consumer NAKed it for a transient
error) or such a re-publish runs teardown again.

### Dynamic reservations fail without any `create_instance` run during a reservations outage

Since issue #937 the execution consumer corroborates `reservation.provision_requested`
against reservations (`GET /internal/{id}` must report `PENDING_PROVISION`) before it
creates anything. While reservations is unreachable or answering 5xx, that check NAKs the
event on the `NATS_NAK_BACKOFF_SECONDS` schedule; an outage that outlasts the five
deliveries (`max_deliver`; the four delays in between total about 81 seconds at the
default `1,5,15,60,120`) dead-letters the event on `herd.reservations.dlq.execution`, and
the best-effort failure callback is lost with reservations still down. The reservation
then sits in `PENDING_PROVISION` until the provision timeout (`PROVISION_TIMEOUT_SECONDS`,
default 900) fails it. No ledger row and
no instance exist, so there is nothing to tear down. Rebook after reservations recovers,
or replay the dead-lettered event before the timeout fires (`DLQ has messages` below).

## NATS and inter-service events

### Reservation created but L1/L2 operations didn't run

The execution service consumes `herd.reservations.*` events from NATS JetStream. If it missed your event:

- Check execution service logs for consumer errors.
- A poison message (invalid JSON) lands on the DLQ subject `herd.reservations.dlq.execution`; inspect with the `nats` CLI (see [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq)).
- A transient handler failure gets NAK'd with an explicit delay from the `NATS_NAK_BACKOFF_SECONDS` schedule (issue #895; `1,5,15,60,120` seconds by default) and retried up to `max_deliver=5`. After that it also goes to DLQ.
- A log line with `"action": "nats_event_unverified"` means the event was acked WITHOUT running the handler: execution's consumer corroborates `reservation.created`/`.updated`/`.cancelled`/`.completed`/`.failed`/`.wiring_changed` against reservations' own status for the row before acting on it (see ARCHITECTURE.md's Event-driven flows section), and this event's claim did not match. This is the expected, correct outcome for an event whose reservation moved on (or never existed) before the message was processed, not a bug; the log line's `reported_status` names what reservations actually had on file.

### Notifications container is stuck in a crash loop on a fresh stack

Symptom: `docker compose logs notifications` shows `sqlalchemy.exc.DBAPIError ... InvalidSchemaNameError: schema "notifications" does not exist` during `Base.metadata.create_all`, and the container keeps restarting.

Root cause: the postgres init script (`infra/postgres/init.sql`) didn't pre-create the `notifications` schema for older checkouts. Pull the latest `init.sql`, or patch the running database:

```bash
docker compose exec postgres psql -U herd -d herd -c \
  "CREATE SCHEMA IF NOT EXISTS notifications; GRANT ALL PRIVILEGES ON SCHEMA notifications TO herd;"
docker compose restart notifications
```

Verify with `curl -k https://localhost/api/notifications/health` returning `{"status":"ok"}`.

### Reservation created but the bell didn't tick up

The notifications service consumes the same `herd.reservations.*` events on its own durable consumer. If the bell stays at zero:

- Make sure you're logged in as the reservation **owner**; iteration 1 only notifies the owner (co-owners and ACL grantees are deferred).
- Check `Settings` and confirm the relevant event (`Reservation confirmed`, `updated`, `cancelled`, `completed`) is still checked and the in-app channel is on.
- The notifications consumer has its own DLQ subject at `herd.reservations.dlq.notifications` (independent from execution's DLQ). Poison messages or exhausted retries land there; see [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq).
- If user-profile is down or misconfigured (missing `INTERNAL_API_TOKEN` match), the consumer fails open and still delivers notifications with defaults; verify user-profile's health endpoint returns 200 if prefs aren't being respected.

### DLQ has messages

Inspect them, figure out why they failed, decide whether to replay or discard. Each DLQ message is a snapshot of the original event payload; replaying means publishing it back on `herd.reservations.<event-type>`. Check both `herd.reservations.dlq.execution` (execution) and `herd.reservations.dlq.notifications` so you don't miss the half of the system you weren't looking for. See [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq).

### DLQ messages disappeared after a rebuild

Expected under `make up` and the gate stack: JetStream state is ephemeral there by design and is lost on a container recreate. See [OPERATIONS.md](OPERATIONS.md#jetstream-durability) for what is and isn't durable, and what `make prod` does instead.

## Secrets service

### Secrets container restarts in a loop or exits at boot

By design: the secrets service refuses to start on missing, malformed, or
mismatched key material rather than serve secrets it cannot encrypt or
decrypt. `docker compose logs secrets` shows the exact `KekError`:

- "SECRETS_KEK is not set": add it to `.env` (base64-encoded 32 bytes; see
  `docs/ENV_VARS.md` for the generation one-liner). Under `make up` the dev
  override supplies a dev-only key, so this appears mostly under `make prod`.
- "not valid base64" or "must decode to exactly 32 bytes": the value is hex or
  truncated; regenerate with the documented python one-liner (not
  `openssl rand -hex`).
- "does not unwrap with SECRETS_KEK": the key changed since secrets were
  stored. Restore the original key, or set the old key as
  `SECRETS_KEK_PREVIOUS` and the new one as `SECRETS_KEK` to rotate (see
  OPERATIONS.md). If the original key is lost, stored secrets are
  unrecoverable by design; delete and recreate them.

### Reveal returns 403 or 404 for a non-admin

Expected gating, not an outage: no grant on the secret returns 404, a `view`
grant returns metadata but 403 on `/value` (plaintext needs `manage`). See the
secrets matrix in [ROLES.md](ROLES.md).

## Migrations and database volumes

### `make migrate` fails with "already exists", or a route 500s on a column that is in the code

Symptom: `make migrate` (or `make migrate-<svc>`) aborts almost immediately with a
`DuplicateObject` / `already exists` error such as `type "role" already exists` or
`relation "..." already exists`; and/or a route 500s referencing a column that is present
in the models and migrations but missing from the running database (the #32
`device_templates.hypervisor_id` case is the worked example).

Root cause: the dev stacks create each service's tables at startup with SQLAlchemy
`create_all` (a convenience so `make up` needs no migration step). On a long-lived dev
volume that predates this fix, `create_all` never wrote an `alembic_version` row, so the
schema is unmanaged. Two consequences: `create_all` cannot ALTER an existing table to add
a newly merged column, so the column is silently absent and the route 500s; and each
service's Alembic chain runs from base against objects that already exist, so
`make migrate` fails on the first `CREATE` it re-attempts and cannot repair the gap.

Going forward, a service that creates a genuinely fresh schema now stamps the Alembic head
at startup, so a later `make migrate` applies only new increments. A schema created by the
old unstamped `create_all` cannot be safely stamped after the fact (stamping head would
falsely claim every merged migration is applied and keep hiding the missing column), so on
such a volume the service logs a loud startup warning naming this fix. The fix for an
already-broken dev volume is to recreate it:

```bash
make down
docker compose down -v   # drops the postgres (and other) volumes; dev data is disposable
make up
```

The stack comes back with fresh, stamped schemas. This discards local dev data by design;
for a stack whose data you need, apply the missing migration by hand instead
(`docker compose exec <svc> alembic stamp <the revision before the new one>` then
`alembic upgrade head`, matching the service's own chain). Production runs migrations, not
`create_all`, so this does not apply to a `make prod` stack.

## E2E / testing

### An e2e test failed in a gate run and the output is gone

`pytest tests/e2e/` prints its usual failure output, but a gate run (`make everything`,
`make master`, or a standalone `make test-e2e`/`make test-e2e-seeded`) can end that
session before you get a chance to read the scrollback, and `.pytest_cache/v/cache/lastfailed`
records only the failing nodeid, nothing else.

`tests/e2e/conftest.py` writes durable failure artifacts to disk for any e2e test that
fails during its call phase, keyed by the test's sanitized nodeid, under
`HERD_E2E_ARTIFACT_DIR` (default `<tempdir>/herd-e2e-artifacts`, e.g.
`/tmp/herd-e2e-artifacts` on Linux; see `docs/ENV_VARS.md`). Each failing test gets its own
subdirectory containing:

- `screenshot.png`: full-page screenshot at the moment of failure.
- `page.html`: the DOM at the moment of failure.
- `console.log`: browser console messages (Playwright tests only reliably; Selenium's is
  best-effort and may be empty if the driver doesn't support it).
- `traceback.txt`: the pytest failure traceback.
- `meta.txt`: the page URL and a UTC timestamp, plus any capture-step failures (e.g. a
  screenshot that itself timed out).

A test that opens several browser contexts through the `pw_contexts` fixture (the
two-session port-conflict test) gets one directory per open page, suffixed `[page<n>]`.
A rerun of the same test overwrites its directory rather than accumulating stale copies.
Nothing is written for a passing test. The pytest output itself also gets an
"e2e failure artifacts" section naming the directory and files written, and the end of
run prints a summary block listing every artifact directory from that session (after the
existing `HERD_E2E_REQUIRE_NO_SKIP` skip-gate report, when that gate is also active; see
`Makefile`'s `_test-e2e-run` and `tests/e2e/conftest.py`). If artifacts are missing, check
that `HERD_E2E_ARTIFACT_DIR` (or its default) points somewhere writable and wasn't cleaned
up between the failing run and when you looked.

In the nightly GitHub Actions workflow, `HERD_E2E_ARTIFACT_DIR` is pointed into the job
workspace, so any files written there upload as part of the `nightly-failure-artifacts`
run artifact (7-day retention) alongside the compose diagnostics; a nightly e2e failure's
screenshots, page HTML, console logs, and tracebacks are downloadable from the run's
Actions page, not just the local `<tempdir>/herd-e2e-artifacts` default.

## Logs and where to look

- **Global tail**: `make logs` (all containers).
- **Per service shell**: `make shell-<service>` (auth, inventory, reservations, cabling, acl, execution, user-profile, notifications, ai-orchestrator).
- **Structured fields to grep for**:
  - `action=reservation_create` / `reservation_provision_failed` (reservations)
  - `action=nats_poison_message` / `nats_dlq_exhausted` / `nats_message_nak` (execution, notifications)
  - `action=notification_delivered` / `notification_opted_out` / `prefs_fetch_failed` (notifications)
  - `action=device_group_create` / `device_group_add_devices` (inventory)
  - `method=POST path=/api/...` / `status_code=5xx` (middleware access log)

All services emit JSON logs; pipe through `jq` for readable output:
```bash
docker compose logs reservations | jq 'select(.level=="ERROR")'
```

## Still stuck?

Open an issue with:

- The exact error message (and its HTTP status if it's an API error).
- A snippet of the relevant service's logs around the time of the failure.
- Which role the user has.
- What they were trying to do.

For feature questions, check the [USER_GUIDE.md](USER_GUIDE.md) glossary first.
