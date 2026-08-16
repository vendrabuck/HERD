# Admin Handbook

Operational playbook for administrators. For the permission matrix, see [ROLES.md](ROLES.md). For first-install steps, see [FRESH_SETUP.md](../FRESH_SETUP.md). For day-2 ops, see [OPERATIONS.md](OPERATIONS.md).

## Who does what

| Task | User | Admin | Superadmin |
|---|:---:|:---:|:---:|
| Register, log in, change own password | yes | yes | yes |
| Browse inventory (within visibility) | yes | yes | yes |
| Create/cancel own reservations | yes | yes | yes |
| List every user's reservations (`GET /reservations/?all=true`) | - | yes | yes |
| Cancel any user's reservation | - | yes | yes |
| Upload drivers, create templates | - | yes | yes |
| Create/delete devices and ports | - | yes | yes |
| Create/delete device groups and assign permissions | - | yes | yes |
| Create/delete user groups and manage membership | - | yes | yes |
| Execute drivers (`POST /execute`) | configure-only, with device manage grant | yes | yes |
| Grant and revoke ACL grants (devices, topologies, reservations, secrets) | - | yes | yes |
| Promote a user to admin or demote | - | - | yes |

A non-admin may call `POST /api/execution/execute` when `action == "configure"` and the
caller holds an ACL `manage` grant on the target device; every other non-admin call to
that endpoint is rejected 403. Full API-level matrix with every endpoint:
[ROLES.md](ROLES.md).

## First-day setup (after FRESH_SETUP.md)

The superadmin is seeded from env vars on first startup. After that:

1. **Log in as superadmin**, change the password if you used a default.
2. **Promote anyone else who needs admin**: `PUT /api/auth/users/{id}/role` with body `{"role": "admin"}` (or use the admin UI if exposed).
3. **Create a few user groups** to start (e.g. `networking`, `security`, `qa`). New users land in `Not Grouped` automatically.
4. **Upload one or more drivers** (see [Seeding drivers](#seeding-drivers) below).
5. **Create a template per device class**, pointing at the matching driver. Mark DUT templates `Management`; infrastructure templates use the relevant switch type.
6. **Create your devices** from those templates. They auto-assign to the `No Pool` device group.
7. **Create device groups** and bulk-move devices out of `No Pool` into the right groups.
8. **Grant each user group permissions** on the device groups it should see.

At this point regular users can log in, see the equipment they have access to, and start reserving.

## Seeding drivers

A driver is a Python package that teaches HERD how to log into and configure a real piece of hardware.

1. Get the driver package (`.zip` or `.tar.gz`, ≤10 MB). Driver authors: see [DRIVERS.md](DRIVERS.md) and its packaging-quickstart section.
2. Go to **Drivers** (under the Administration menu).
3. Click **Upload**. Fill in:
   - **Name** (unique; e.g. `juniper-fw-mgmt`).
   - **Description** (what it drives).
   - **Connection type** (`Management` for DUTs, `Layer 1 Switch` / `Layer 2 Switch` / `Layer 3 Switch` for infrastructure, or `Hypervisor` for a dynamic-resources recipe driver).
   - **File** (the `.zip` or `.tar.gz`).
4. Upload. The driver is stored locally under `/data/drivers/` by default (or in MinIO if configured) and validated on first use.

Drivers are reference-counted by templates: you can't delete a driver that a template points at. Remove the template first or update it to a different driver.

## Creating templates and devices

### Templates

Templates define the fields your devices carry. Three template types:

- `device` templates define a class of physical or cloud Device. Must reference a driver.
- `port` templates define a class of Port. Ports are children of devices; no driver.
- `dynamic` templates define a hypervisor-backed instance type (ADR 0004, issue #32):
  they pair a registered hypervisor with a `Hypervisor`-connection-type recipe driver so
  a reservation can materialize an instance rather than claim a physical device.

Typical workflow:

1. **Templates > New template** (Templates is its own top-level nav item).
2. Pick `device`, `port`, or `dynamic`.
3. If `device`, pick the driver and mark `exclusive` (default true). Exclusive devices enforce one-reservation-at-a-time; non-exclusive are shared infrastructure. If `dynamic`, pick a `Hypervisor`-connection-type recipe driver and a registered hypervisor (see Hypervisors below and [docs/design/0004-dynamic-resources.md](design/0004-dynamic-resources.md)).
4. Fill in **Vendor** and **Model** (e.g. `Juniper Networks` / `EX2300`). **Part number** is optional and only used when the template represents a specific orderable SKU. The **Suggest with AI** button (admin only, requires an AI provider to be configured) infers vendor/model from the template name; review and edit before saving. Identity fields enable the AI reservation assistant to ground responses in actual hardware context.
5. Add field sections with typed fields (`string`, `number`, `boolean`, `password`, `dropdown`). Add per-field defaults where useful. Password fields are masked in the UI and excluded from search.
6. Save.

Changing a template mid-life is fine, but: new required fields without defaults will break existing devices' validation. Prefer adding optional fields.

### Devices

**Inventory > Add device**. Pick the template, fill in field data, optionally pick an initial device group. Devices auto-join `No Pool` if you don't pick one.

### Ports

From an existing device's page, **Add ports**. Pick a port template, fill in field data, submit. Bulk port creation is supported.

## Device groups (visibility)

Device groups control which devices non-admin users can see.

- A device group is a named collection of devices.
- A device group can have user-group permissions attached. If user group `networking` has access to device group `lab-a`, then every user in `networking` can see and reserve every device in `lab-a`.
- A device can be in multiple device groups.
- Admins always see all devices regardless of group memberships.
- The `No Pool` group is seeded on startup; new devices auto-join. When you bulk-add a device to any other group, it's auto-removed from `No Pool`.

### Setup

1. **Device Groups > New group**. Name + description.
2. **Add devices**: TransferList UI. Move from "Available Devices" to "Group Devices".
3. **Add user-group permissions**: pick which user groups get access.
4. Save.

To restrict a user group: remove its permission from the device group, not the user from the user group.

## User groups

User groups do the opposite: they collect users so you can grant many of them access at once.

- A user can be in multiple user groups.
- The `Not Grouped` group is seeded; new registrations auto-join. When a user is added to any other group, they're auto-removed from `Not Grouped`.
- Admins and superadmins can CRUD groups and bulk-manage members.

Setup is symmetrical to device groups: **User Groups > New group**, add members, save.

## Promoting and demoting users

Only superadmins. From the Users admin page, pick a user, use the role picker. Under the hood this is `PUT /api/auth/users/{id}/role` with the new role. Only `user` and `admin` are assignable; a request with `role: "superadmin"` is refused with HTTP 400, since the superadmin role can only be set outside the API (see [ROLES.md](ROLES.md#superadmin)).

## Reservations administration

### Viewing anyone's reservations

The calendar is cross-user for every role, filtered to device visibility. The
reservations list (`/reservations`) is scoped to your own reservations by default for
every role. Admins and superadmins get an "All reservations" toggle on that page that
switches the list to every user's reservations; it is wired to the admin-only
`GET /reservations/?all=true` query param (issue #340). A non-admin who passes
`all=true` is rejected with 403 `Only admins can list all reservations`.

### Cancelling someone else's reservation

Admins and superadmins can cancel any reservation, not just their own: turn on the
"All reservations" toggle, then use the Cancel action on the target row (or call
`DELETE /reservations/{id}` directly). This frees a stuck or abandoned lab without
database surgery. The cancel is audited: when an admin cancels a reservation they do
not own, the reservation's `cancelled_by` field records the acting admin's user id; an
owner cancelling their own reservation leaves `cancelled_by` null. The cancel emits the
same `reservation.cancelled` event as an owner self-cancel, so the owner is still
notified and the devices are released. A non-admin cancelling a reservation they do not
own still gets 404, unchanged.

### Recovering a FAILED reservation

The reservation row is audit-only: no devices were actually reserved. If you need to investigate:

- Check reservations service logs for `action=reservation_provision_failed`. The message includes the reservation id.
- The most common cause is the inventory service being unreachable during the provisioning retry window (3 attempts, ~1.5s total backoff).
- Create a new reservation for the same devices and window; the retry should succeed once inventory is healthy.

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#reservation-is-failed).

### Utilization report

Admin-only page at `/reporting`. Answers "who used what, for how long" across completed reservations in a selected window.

- Pick a window: **7 days**, **30 days**, or **Custom** (two date pickers; UTC-anchored so what the picker shows matches what the backend filters on regardless of browser timezone). Default is 30 days.
- Headline cards show **total reservation-hours**, **reservations counted**, and **execution runs** for the window. The runs count is sourced from the execution service; if that service is unreachable the card shows `n/a` and the rest of the report still renders.
- **Daily trend chart**: SVG bar chart of reservation-hours per UTC day in the window. A reservation spanning midnight contributes to both days correctly.
- Five tables:
  - **By User** (owner name, hours, reservation count)
  - **By Device** (device name with its template, hours, count)
  - **By Group (cost center)** (user-group name, hours, count; users in multiple groups count against each, users with no group land in `Ungrouped`)
  - **By Topology Type** (`PHYSICAL` / `CLOUD`, hours, count)
  - **By Template** (template name, hours, count)
- **Fleet Utilization** card: every inventory device joined against reserved hours in the window, with a per-device utilization rate (`reserved hours / window hours`), the device's current status, and an "Idle only" toggle that filters to devices with zero bookings in the window. Summary numbers show the fleet-wide rate, device count, and idle count. Unlike the tables above, this slice counts ACTIVE plus COMPLETED reservations by default, so in-flight usage is visible in a window that includes the present; an explicit `status` query applies to every section. The denominator is always the full window: device status has no history, so a box that sat in MAINTENANCE shows a low rate with its current status next to it rather than a silently adjusted denominator. If the inventory service is unreachable the card degrades to an "unavailable" notice and the rest of the report still renders. The rate is not clamped at 100 percent; overlapping rows (possible when the filter includes FAILED) show through rather than being hidden.
- Reservations are clamped to the window: a 30-day reservation seen through a 7-day window contributes 7 days, not 30.
- Cancelled and failed reservations are excluded by default. The underlying endpoint accepts an optional `status` query param if you want a different slice.
- **CSV export**: "Download CSV" button on the By User, By Device, By Template, and Fleet Utilization tables. User/device/fleet CSVs come from the server; the template CSV is serialized in the browser from the same rollup the table uses.

Under the hood:

- `GET /api/reservations/reports/utilization?start=&end=&status=` returns `UtilizationReport{total_hours, total_reservations, by_user, by_device, by_topology_type, by_day, by_group, execution_run_count, fleet}` (admin-only). `fleet` is `FleetSection{device_count, idle_device_count, window_hours, total_reserved_hours, utilization_pct, devices}` or `null` when inventory is unreachable.
- `GET /api/reservations/reports/utilization.csv?start=&end=&section=user|device|fleet&status=` returns a `text/csv` download with `Content-Disposition: attachment`. `section=template` is rejected with 422 because the template rollup depends on the inventory service and is computed client-side. `section=fleet` returns 503 `Inventory service is unreachable` when the inventory join cannot be made (the CSV has no way to degrade partially).
- For the fleet section the reservations service pages through inventory's `GET /api/inventory/devices` (500 per page, forwarding the admin JWT); devices deleted from inventory keep their hours in `by_device` but are omitted from `fleet`.
- The reservations service calls the execution service's `GET /api/execution/runs?created_after=&created_before=&limit=1` endpoint, forwarding the admin JWT, and reads `.total` for the run-count card.
- The reservations service calls the auth service's batch endpoint `POST /api/auth/groups/users/groups` once with the full set of distinct report users (request body `{user_ids: [...]}`, response `{user_id: [group, ...]}`) to build the By Group slice, so the rollup is a single round-trip rather than one call per user.
- The **By Template** slice is still computed on the client by joining `by_device` with the inventory device list, so devices deleted since the reservation was created show up under template `Unknown`.
- Postgres has indexes on `reservations.start_time`, `end_time`, and `status` so the window scan stays cheap as data grows.

## Driver execution

The execution service runs driver code on infrastructure at reservation lifecycle events. Admins can:

- **View execution runs** at `/api/execution/runs` (paginated). Each run records the driver, action, status (`SUCCESS`, `FAILED`, `TIMEOUT`), output, error, duration, and input params.
- **Manually execute** a driver method: `POST /api/execution/execute` with device id, action, user id, reservation id, and method_kwargs.
- **Retry a failed run**: `POST /api/execution/runs/{id}/retry`. Only runs with status `FAILED` or `TIMEOUT` can be retried; successful runs are immutable.

Execution is mostly event-driven via NATS: reservation lifecycle events trigger L1 port connect/disconnect and L2 VLAN provision/deprovision automatically. Manual `/execute` is for AI commits with configs and for ad-hoc admin actions.

## ACL grants (devices, topologies, reservations, secrets)

Device visibility for non-admin users is primarily via device groups (above); ACL
device grants layer narrower per-device carve-outs on top (e.g. the execute-drivers
carve-out above, and the reservation-owner widening on device-config writes). ACL also
covers topology, reservation, and secret resources.

- `POST /api/acl/grants` creates a grant: `{resource_type: "device|topology|reservation|secret", resource_id, group_id, permission: "view|manage"}` (`group_id` is the user-group UUID).
- `manage` implies `view`.
- `POST /api/acl/check` checks if a user has a given permission on a resource.
- `GET /api/acl/resources?user_id=&resource_type=&permission=` lists resources accessible to a user.

In practice you rarely need manual ACL grants for daily use; the defaults (topologies are visible to admins; reservations are visible to their owner and admins) cover most cases.

## Secrets (encrypted credential store)

Admins manage named secrets in the secrets service; values are encrypted at rest and a database dump alone never yields plaintext (see `docs/ARCHITECTURE.md`).

- `POST /api/secrets/secrets` creates a secret: `{name, type, description, data: {key: value, ...}}`. The response carries metadata only; plaintext is never echoed on create, list, or get.
- `GET /api/secrets/secrets/{id}/value` reveals the plaintext. Non-admin reveal requires a `manage` grant on the secret (`view` sees metadata only, and gets 403 on reveal).
- `PUT /api/secrets/secrets/{id}` replaces the payload (re-encrypted wholesale); `DELETE` removes it, refused with 409 (naming the blockers) while any hypervisor still references the secret: delete or re-point the hypervisor first. The check fails closed with 503 if inventory is unreachable.
- `POST /api/secrets/keys/rotate` (admin) introduces a new encryption-key version and re-encrypts every secret.
- `SECRETS_KEK` in `.env` is the key-encryption key; the service refuses to boot without it. See `docs/ENV_VARS.md` for generation and the `SECRETS_KEK_PREVIOUS` rotation flow.

## Routine checks

- **`make logs`** (or container logs) for error spikes.
- **Reservations dashboard**: watch for unusually high `FAILED` rates (symptoms: inventory or internal token trouble).
- **NATS DLQs**: two consumers each have their own DLQ subject on the `HERD_RESERVATIONS` stream. Any messages on `herd.reservations.dlq.execution` (execution consumer) or `herd.reservations.dlq.notifications` (notifications consumer) are poisoned events that the respective service couldn't handle. Check both during incident triage since one can fill up without the other. See [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq).
- **Config service state**: if services crash-loop on startup, `config.json` probably hasn't been written yet. Either populate every required var in `.env` and restart (the config service auto-bootstraps from env on first start) or go through the wrench-icon flow. See [OPERATIONS.md](OPERATIONS.md#config-service-first-run).
- **Disk usage**: the execution service caches driver packages at `/data/driver-cache/`; size grows with driver churn.

## AI topology generation

The AI feature is opt-in and gated by `ai_is_configured()`:

- For the default `AI_PROVIDER=anthropic`, either `AI_API_KEY` (hosted API) or `AI_BASE_URL` (a local Anthropic-compatible endpoint, e.g. vLLM) being set is enough. For `AI_PROVIDER=openai_compat`, `AI_BASE_URL` must be set. Set the relevant variable(s) in `.env` (or via the config UI's AI Integration section) and `make restart`. The frontend checks `GET /api/ai/status` on load, so the **Use AI** button appears on the topology editor only when the provider is configured.
- Optional `AI_MODEL` (default `claude-sonnet-4-6`) lets you bias for quality (`claude-opus-4-7`) or cost (`claude-haiku-4-5`).
- To turn the feature off cleanly, blank the relevant variable(s) and `make restart`; users will see the button disappear on reload.
- `/api/ai/status` is unauthenticated by design (no secret content) so the frontend can decide whether to render the button before any user logs in. It returns `{enabled, provider, model, recipe_authoring}`.

See [AI_GENERATE.md](AI_GENERATE.md) for the user-facing flow and [TROUBLESHOOTING.md](TROUBLESHOOTING.md#ai-topology-generation) for failure modes.

## Notifications

Notifications is a pair of durable NATS consumers plus a small REST API; there's no admin UI for it today. What you need to know:

- Two consumers join two streams on startup. `notifications-consumer` on `HERD_RESERVATIONS` (DLQ `herd.reservations.dlq.notifications`) handles reservation lifecycle events. `notifications-health-consumer` on `HERD_HEALTH` (DLQ `herd.health.dlq.notifications`) handles `device.health_transition` events published by the execution-service health-poll scheduler. Distinct durables so a stuck health-event consumer cannot block reservation events.
- Per-user preferences live under `user_preferences.extras.notifications` in the user-profile service. Users manage them at `/settings`; admins don't need to touch prefs directly, but if a user reports silence the fastest check is their Settings page (per-channel toggles + per-event checkboxes; the `device.health_transition` toggle is fleet-wide). In-app is on by default; the outbound channels (email, chat, webhook) default off, so a user hears nothing on a new channel until they opt in.
- The consumer reads prefs via user-profile's internal endpoint and resolves a recipient's email and username via auth's `/internal/users/{id}/contact` endpoint, so `INTERNAL_API_TOKEN` must be identical on the notifications, user-profile, auth, and reservations containers. If you rotate the token, restart all four so none is stuck on the old value.
- NATS absence is non-fatal: the notifications REST API (list, mark-read, preferences) stays up even if JetStream is unreachable, and the consumers reconnect when NATS returns. Reservation and health events produced during the outage are not lost: the reservations and execution services stage them in a transactional outbox (issue #21) and a relay delivers them once NATS is back.
- Iteration 1 of notifications notified only the reservation owner. Iteration 2 (ROADMAP #13) added health-transition fan-out to admins + active reservation holders, deduped, with emit-on-Nth-failure suppression.
- ROADMAP #40 shipped the three outbound dispatchers as peers of the in-app one: `EmailDispatcher` (SMTP), `ChatDispatcher` (Slack-style incoming webhook), and `WebhookDispatcher` (HMAC-signed POST). Channel transport is instance-level config on the notifications service (see [ENV_VARS.md](ENV_VARS.md#notifications-service)); per-user opt-in is the channel toggle. A channel that a user has enabled but whose transport you have not configured is a logged no-op, not an error, so opt-in can precede wiring. A send failure on one channel is logged and isolated: it never blocks the other channels or in-app delivery. Outbound sends are deduped on the stable event id (the payload `event_id`, falling back to the source NATS stream+sequence for pre-outbox events) via the `outbound_deliveries` ledger, so a JetStream redelivery or a relay republish does not resend an email or re-POST a webhook.
- Outbound webhooks are signed with HMAC-SHA256 over the exact request body, sent as `X-HERD-Signature: sha256=<hex>` keyed by `WEBHOOK_SIGNING_SECRET`. A receiver authenticates a payload by recomputing the HMAC over the received bytes.
- ROADMAP #40 also added an upcoming-expiry reminder. The reservations expiration task publishes a `reservation.expiring_soon` event onto `HERD_RESERVATIONS` for each ACTIVE reservation whose `end_time` falls within `EXPIRY_REMINDER_LEAD_SECONDS` of now; notifications consumes it through the existing consumer (no new stream). The reminder fires exactly once per reservation, deduped via the reservation's `expiry_reminder_sent_at` column. Set the lead window to 0 to disable reminders.

## Device health monitoring

Each device template and each device can carry a `poll_interval_seconds` field (minimum 30, NULL means no polling). Device-level value wins; falls back to the template value; both NULL means the device is not polled. Set this in the device-edit form or template editor.

The execution service runs an asyncio background scheduler that scans `device_health_status` every `HEALTH_POLL_SCHEDULER_TICK_SECONDS` (default 30) for rows due to poll. Each due device runs the existing `login` / `status` / `logout` driver sequence; the outcome lands in `device_health_status` (HEALTHY, DEGRADED, UNREACHABLE, UNKNOWN) and three rows go into `execution_runs` for audit. The frontend renders the snapshot as a colored badge on the device-detail page.

What admins typically check:

- **A device flipped UNREACHABLE.** Inspect `GET /api/execution/device-health` (admin-only list endpoint) to see the snapshot. Then look at the most recent `execution_runs` for that device to see which step failed: a login failure means credentials or reachability; a status failure means the device responded but reported a problem; both succeeded with logout failing means the driver's cleanup is buggy but the device is fine.
- **A device should be polled but isn't.** Check that `poll_interval_seconds` is set on either the device or its template; the resolved value appears in the device-detail response under `resolved_poll_interval_seconds`. The scheduler refreshes its in-memory registry every `HEALTH_POLL_REGISTRY_REFRESH_SECONDS` (default 300), so newly-opted-in devices may take up to five minutes to be picked up.
- **Notifications fire too often or not at all.** The bad-news event fires only when `consecutive_failures` crosses `HEALTH_POLL_MAX_CONSECUTIVE_FAILURES` (default 3). Below the threshold, polls are silent; past it, the next poll triggers exponential backoff with jitter capped at `HEALTH_POLL_BACKOFF_CAP_SECONDS` (default 3600). Recovery fires on the first successful poll that resets `consecutive_failures` to zero. If transitions need to be quieter, raise the threshold; if they need to fire sooner, lower it (devices already past the new threshold do NOT re-fire, which is correct dedupe behavior).
- **Silence the publisher entirely.** Set `HEALTH_POLL_NOTIFY_ENABLED=false` and restart execution. The scheduler still polls and updates snapshots; only the NATS publish is skipped.

## Backup and restore

See [OPERATIONS.md](OPERATIONS.md#backup-and-restore). In short, preserve:

- Postgres volumes (one per service schema).
- The `herd-config` Docker volume (has `config.json` and password hash).
- `driver-storage` volume (or MinIO bucket) for uploaded driver packages.

## Wiring up LDAP / Active Directory

By default HERD authenticates against its own bcrypt-hashed password store. To point the stack at a corporate directory instead:

1. In the config editor (wrench icon), set `AUTH_METHOD=ldap` and fill in the `LDAP` group: server URL (prefer `ldaps://...:636`), service-account bind DN + password, user search base DN, and optionally the filter / attributes / TLS toggle. Full list with an AD-flavored example: [ENV_VARS.md](ENV_VARS.md#ldap--active-directory).
2. Click **Apply** so the auth service picks up the new values. Local registration (`POST /api/auth/register`) now returns 409; new HERD accounts are provisioned lazily on the first successful LDAP bind.
3. Have an admin log in with their directory credentials so their account is created as an LDAP user (`auth_source='ldap'`, no local hash). Promote them via `PUT /api/auth/users/{id}/role` if they need `admin` or `superadmin`.
4. Role still lives inside HERD; promote admins by hand as in step 3. `UserGroup` membership can now mirror directory groups: map a directory group to a HERD group at **Admin > LDAP Sync** (`/admin/ldap-sync`), then either trigger a one-off **Sync now** or turn on `LDAP_GROUP_SYNC_ENABLED` for the background interval loop. Unmapped groups still need HERD group membership assigned by hand.

Rolling back: flip `AUTH_METHOD` back to `local` and apply. Pre-existing local accounts resume working; LDAP-sourced rows stay in the table but cannot log in until you switch back to `ldap` (they have no password hash).

Bootstrap gotcha: the seeded `SUPERADMIN_*` account is always `auth_source='local'`. Keep `AUTH_METHOD=local` until at least one directory-backed admin has logged in once, or keep a known-good local superadmin around as a break-glass.

## Things you cannot do from the admin UI today

- Password reset for a locked-out user. You'd need to update the auth DB directly or delete and re-register the user.
- Schema migration across services in one click. Use `make migrate`; see [OPERATIONS.md](OPERATIONS.md#upgrade-path).
- Cert rotation from the UI. Replace files under `infra/traefik/certs/` and restart Traefik.
- Inspect or replay NATS DLQ from the UI. Use the `nats` CLI (see [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq)).
