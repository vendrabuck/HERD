# Load Testing with Locust

## Prerequisites

```bash
pip install locust
# or
uv pip install locust
```

Requires a running HERD stack with seed data (`uv run python seed_devices_public.py`).

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
| InventoryBrowser | 5 | List devices, get device detail, list templates |
| ACLChecker | 2 | Single and batch permission checks |

## Key Metrics

- **Auth endpoint throughput**: bcrypt hashing is CPU-bound; watch for login latency
- **Reservation creation**: advisory lock contention under concurrent writes
- **Calendar query latency**: grows with reservation count in the time window
- **Error rate**: should stay below 1% under normal load

## Environment Variables

- `SEED_EMAIL` / `SEED_PASSWORD`: admin credentials (default: seed script values)
- `SEED_USER_EMAIL` / `SEED_USER_PASSWORD`: regular user credentials
- `HERD_BASE_URL`: stack URL (default: https://localhost)
