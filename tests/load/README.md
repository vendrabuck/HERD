# Load Testing with Locust

## Prerequisites

```bash
pip install locust
# or
uv pip install locust
```

Requires a running HERD stack with seed data (`make seed`, or `uv run python -m seedtools full`).

## Running

### Web UI (interactive)

```bash
make test-load-ui
# Opens http://localhost:8089
# Configure users, spawn rate, and run duration in the browser
```

### Headless (CI-friendly)

```bash
make test-load
# Runs 20 users, ramp-up 5/s, for 1 minute
```

### Custom parameters

```bash
cd tests/load
locust -f locustfile.py \
  --host https://localhost \
  --headless \
  -u 50 \        # total users
  -r 10 \        # spawn rate (users/second)
  --run-time 5m  # duration
```

## User Classes

| Class | Weight | Description |
|-------|--------|-------------|
| ReservationUser | 3 | Create, list, calendar query, release reservations |
| InventoryBrowser | 5 | List devices, get device detail, list templates, download a driver package |
| ACLChecker | 2 | Single and batch permission checks |
| LoginLimitProbe | exactly 1 user | After 15 s, floods wrong-password logins from this host until auth's per-source attempt limit answers 429; any answer but 401 or 429 fails the run |
| OversizedBodySender | exactly 1 user | Never signs in; sends bodies one byte over `MAX_REQUEST_BODY_BYTES` to one route of every service, with a Content-Length or chunked; any answer but the 413 `body_too_large` fails the run |
| LargeCanvasSaver | exactly 1 user | Signs in as the admin and saves one canvas over the default body limit (about 1.3 MiB) to `PUT /api/cabling/topologies/{id}`, a large-JSON route (issue #1484); any answer but 200 fails the run |

## Logins and the per-source attempt limit

Every locust user signs in from this host, and auth counts every password
attempt against the source address it comes from (60 at once by default,
refilling one a second; `AUTH_LOGIN_MAX_ATTEMPTS`, `AUTH_LOGIN_WINDOW_SECONDS`).
So a user that meets a 429 at sign-in waits the `Retry-After` and tries again;
that 429 is recorded as `/api/auth/login [429 attempt limit]`, a success, so the
`/api/auth/login` row holds only logins auth judged. With the default 20 users
none is expected. `LoginLimitProbe` then shows the limit under load: its flood is
cut to the allowance with 429s that cost auth nothing, while the other users'
requests carry on in the same report. Because it empties the host's allowance, a
suite started right after `make test-load` may meet a 429 until it refills.

## Key Metrics

- **Auth endpoint throughput**: bcrypt hashing is CPU-bound and runs on auth's
  hashing threads (`AUTH_PASSWORD_HASH_THREADS`); watch for login latency. A 503
  on login means every hashing thread was busy for 5 seconds
- **Reservation creation**: advisory lock contention under concurrent writes
- **Calendar query latency**: grows with reservation count in the time window
- **Error rate**: should stay below 1% under normal load

## Environment Variables

- `SEED_EMAIL` / `SEED_PASSWORD`: admin credentials (default: seed script values)
- `SEED_USER_EMAIL` / `SEED_USER_PASSWORD`: regular user credentials
- `HERD_BASE_URL`: stack URL; `make test-load` drops a trailing `/api` (default under make: the gateway URL `make gateway-url` prints, from `HERD_GATEWAY_BIND`; otherwise https://localhost)
