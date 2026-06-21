# Troubleshooting

Common failure modes and how to diagnose them. Organized by what the user sees.

## Login and setup

### Login form inputs are disabled with a "not configured" banner

`config.json` has not been written to the `herd-config` volume, so the login page is gating behind the setup flow. Two fixes:

- Put every required value in `.env` (see `.env.example`) and `make restart`. The config service's first-start auto-bootstrap will write `config.json` from the env on boot, and the login form will enable. Check `docker compose logs config` for a message like `Config bootstrapped from environment` or a warning naming the missing var.
- Or click the wrench icon on the login page, log in with `admin123!`, fill in required settings, and click **Save and Restart**.

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

## AI topology generation

### `409 Inventory shifted during generation`

Between the LLM's proposal and the device resolver's fetch, a device the LLM wanted became unavailable (reserved, status changed, or missing). Regenerate.

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

## NATS and inter-service events

### Reservation created but L1/L2 operations didn't run

The execution service consumes `herd.reservations.*` events from NATS JetStream. If it missed your event:

- Check execution service logs for consumer errors.
- A poison message (invalid JSON) lands on the DLQ subject `herd.reservations.dlq.execution`; inspect with the `nats` CLI (see [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq)).
- A transient handler failure gets NAK'd and retried up to `max_deliver=5` with configured backoff. After that it also goes to DLQ.

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

## Logs and where to look

- **Global tail**: `make logs` (all containers).
- **Per service shell**: `make shell-<service>` (auth, inventory, reservations, cabling, acl, execution, user-profile, notifications).
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
