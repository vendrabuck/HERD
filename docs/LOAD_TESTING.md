# Load Testing

HERD's load tests drive the HTTP API with [Locust](https://locust.io/), a Python load-generation framework. A single locustfile spawns multiple virtual user classes, each logging in and exercising a different slice of the product.

## What the tests do

The locustfile lives at `tests/load/locustfile.py`. It defines one abstract base class and six concrete user classes:

### `HerdUser` (base)

Shared behavior for every virtual user:

- Logs in against `/api/auth/login` during `on_start`, stashes the access token, and attaches it as a Bearer header.
- Provides `_auth_get`, `_auth_post`, `_auth_put`, `_auth_delete` helpers that inject the token and disable TLS verification (the stack uses a self-signed chain behind Traefik).
- Marked `abstract = True`, so Locust does not spawn it directly.

### `ReservationUser` (weight 3)

Exercises the reservation flow as an admin.

- `on_start`: logs in, caches IDs of devices that are `AVAILABLE` and `exclusive=True`.
- Tasks:
  - `list_reservations` (weight 3): `GET /api/reservations/`
  - `query_calendar` (weight 2): `GET /api/reservations/calendar` with a 24-hour window
  - `create_and_release` (weight 1): `POST /api/reservations/` then `PUT /api/reservations/{id}/release`
- Think time: 1 to 3 seconds between tasks.

### `InventoryBrowser` (weight 5)

Simulates read-heavy inventory browsing.

- `on_start`: logs in as admin, caches all device IDs.
- Tasks:
  - `list_devices` (weight 5): `GET /api/inventory/devices`
  - `get_device_detail` (weight 3): `GET /api/inventory/devices/{id}`
  - `list_templates` (weight 2): `GET /api/inventory/templates`
- Think time: 1 to 2 seconds.

### `ACLChecker` (weight 2)

Hits the permission-check endpoints as a non-admin user.

- `on_start`: logs in with `SEED_USER_EMAIL` / `SEED_USER_PASSWORD`, pulls its own user id from `/api/auth/me`, caches the first four device ids.
- Tasks:
  - `batch_check` (weight 3): `POST /api/acl/check/batch` for all cached devices
  - `single_check` (weight 2): `POST /api/acl/check` for one device
- Think time: 1 to 3 seconds.

### `BulkExporter` (weight 1)

Exercises the bulk export/import surface as an admin.

- Tasks:
  - `export_devices_json` (weight 3): `GET /api/inventory/devices/export?format=json`
  - `export_devices_csv` (weight 2): `GET /api/inventory/devices/export?format=csv`
  - `export_templates_json` (weight 2): `GET /api/inventory/templates/export?format=json`
  - `export_topologies_json` (weight 2): `GET /api/cabling/topologies/export?format=json`
  - `dry_run_device_import` (weight 1): `POST /api/inventory/devices/import` with `dry_run=true`

### `NotificationUser` (weight 2)

Simulates a user polling and tuning notifications.

- Tasks:
  - `unread_count` (weight 5): `GET /api/notifications/notifications/unread-count`
  - `list_notifications` (weight 3): `GET /api/notifications/notifications`
  - `update_preferences` (weight 1): updates the user's notification preferences

### `BulkConnectionAdmin` (weight 1)

Exercises `POST /connections/bulk`, the admin-only bulk cable-create path.

- `on_start`: logs in as admin, caches up to 50 device ids.
- Tasks:
  - `bulk_create_and_cleanup` (the class's only task): posts a small batch (2 to
    5 pairs) built from the cached device ids, then deletes every row the
    batch actually created so the stack's connection count stays flat across
    a run. A rejected row (the self-loop guard, or a 503 batch-wide abort if
    inventory's device-group check is momentarily unreachable) is a
    legitimate outcome under concurrency, not a load-test failure.
- Think time: 2 to 5 seconds.

### Class weighting

Locust picks which class to spawn using the `weight` attribute. With the defaults (`ReservationUser` 3, `InventoryBrowser` 5, `BulkExporter` 1, `NotificationUser` 2, `ACLChecker` 2, `BulkConnectionAdmin` 1; total 14), out of every 14 virtual users you get roughly 3 reservation users, 5 inventory browsers, 1 bulk exporter, 2 notification users, 2 ACL checkers, and 1 bulk-connection admin. Increase `-u` to scale all six proportionally.

## Prerequisites

- A running HERD stack (`make up`), reachable via HTTPS.
- Seeded data so the user classes have devices and users to exercise. The repo ships `seed_devices_public.py`, run via `make seed`, which creates admin and regular users, templates, devices, switches, cabling, groups, and demo topologies. It resolves its login from `SEED_EMAIL`/`SEED_PASSWORD`, then `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD`, then a generic `admin@example.com` default.
- `locust` installed; it ships as a dev dependency in the workspace, so `make install` (or `uv sync --all-extras`) pulls it in.

The admin credentials default to `SEED_EMAIL=admin@example.com` / `SEED_PASSWORD=<your-admin-password>`; the regular user defaults to `SEED_USER_EMAIL=user1@herd.dev` / `SEED_USER_PASSWORD=<your-user-password>`. Override via environment to match your seed. If the regular user does not exist the `ACLChecker` class will emit 401s; either seed that account or unset the class by reducing its weight.

## Running

### Headless (CI-friendly)

```bash
make test-load
```

Equivalent to:

```bash
cd tests/load && uv run locust -f locustfile.py \
  --host ${HERD_BASE_URL:-https://localhost} \
  --headless -u 20 -r 5 --run-time 1m
```

- `-u 20`: total virtual users
- `-r 5`: spawn rate (users per second)
- `--run-time 1m`: test duration

### Interactive web UI

```bash
make test-load-ui
```

Opens Locust's web console at `http://localhost:8089`. Set users, spawn rate, and host in the browser, then start and stop runs interactively. Useful for exploratory runs and for watching the live charts.

### Custom parameters

```bash
cd tests/load
uv run locust -f locustfile.py \
  --host https://my-stack.example.com \
  --headless \
  -u 50 \
  -r 10 \
  --run-time 5m
```

### Targeting a non-default host

Set `HERD_BASE_URL` to point the Makefile targets at a different stack:

```bash
HERD_BASE_URL=https://localhost make test-load
```

## Reading the output

Locust prints per-endpoint stats and a final percentile table. The columns that matter most:

- `# reqs` and `# fails`: total count and failure rate per endpoint; aim for under 1% failures under normal load.
- `Avg`, `Med`, `Max`: response-time stats in milliseconds.
- `req/s`: realized throughput.
- Percentile table (`50%`, `95%`, `99%`): use p95/p99 as the SLO signal; averages hide tail latency.

An "Error report" at the bottom lists exception messages for any failed requests.

## What to watch for

- **Auth endpoint throughput.** `/api/auth/login` runs bcrypt; it is CPU-bound and sets an upper bound on how fast you can ramp users.
- **Reservation creation contention.** `POST /api/reservations/` takes an advisory lock per device; concurrent writes to the same device serialize.
- **Calendar query latency.** `GET /api/reservations/calendar` scales with the number of reservations in the window; latency grows as the fixture ages.
- **Pagination.** List endpoints (`/api/inventory/devices`, `/api/inventory/templates`, `/api/reservations/`) return `{"items": [...], "total": N, "skip": N, "limit": N}`. The locustfile unwraps `["items"]` in each `on_start`; keep that in mind if you add new tasks that parse list responses.

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `HERD_BASE_URL` | Target stack URL | `https://localhost` |
| `SEED_EMAIL` | Admin login for `ReservationUser` and `InventoryBrowser` | `admin@example.com` |
| `SEED_PASSWORD` | Admin password | `admin123!` |
| `SEED_USER_EMAIL` | Regular user login for `ACLChecker` | `user1@herd.dev` |
| `SEED_USER_PASSWORD` | Regular user password | `user1user1xx` |

## Extending the tests

To add a new scenario:

1. Subclass `HerdUser` in `locustfile.py`.
2. Set `weight` and `wait_time`.
3. Implement `on_start` to log in (call `self._login(...)`) and cache any ids you need.
4. Add `@task(n)` methods; use the `_auth_*` helpers so the Bearer token and TLS bypass are applied.
5. When parsing a list response, unwrap the paginated envelope: `resp.json()["items"]`.

Run a short smoke before committing:

```bash
cd tests/load && uv run locust -f locustfile.py \
  --host https://localhost --headless -u 3 -r 3 --run-time 5s
```

Every user class should appear in the "All users spawned" line and produce at least one request of each task.

## Troubleshooting

- **`locust: not found`.** `locust` is a dev dependency; run `make install` (or `uv sync --all-extras`), or invoke it via `uv run locust` rather than the bare binary.
- **Every user 401s immediately.** Login failed; check `SEED_*` env vars match the actual seed and that `/api/auth/login` is reachable.
- **`TypeError: string indices must be integers`.** A task is treating a paginated response as a list. Unwrap `.json()["items"]`.
- **`ACLChecker` fails 401 while other classes pass.** The regular user (`SEED_USER_EMAIL`) is not seeded. Create it or reduce the class weight to 0.
- **Self-signed TLS warnings.** Expected; the helpers pass `verify=False`. The one-time `InsecureRequestWarning` is harmless noise.
