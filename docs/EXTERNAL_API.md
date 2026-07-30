# External Integration API and Webhooks

HERD exposes a stable, versioned external surface for automation, separate from
the internal endpoints the web UI calls. It has two halves:

- An inbound HTTP API under `/api/v1` for reserving and releasing devices from
  CI/CD pipelines and test-automation systems.
- Outbound webhooks that POST reservation lifecycle events to endpoints you
  register, so external systems react to HERD instead of polling it.

The `/api/v1` surface is a thin facade owned by the `integration` service. It
forwards the caller's identity to the internal services, so RBAC, device-group
visibility, and ACL grants apply to an automation client exactly as they do to an
interactive user. The facade is decoupled from the internal UI endpoints on
purpose: internal refactors must not break v1 clients. See
[ARCHITECTURE.md](ARCHITECTURE.md) for where the `integration` service sits.

For the machine-readable contract, see
[api/v1-openapi.json](api/v1-openapi.json).

## Authentication for automation

Interactive users log in with a username and password and receive a JWT plus a
refresh token. A machine should not hold a user password or a refresh token.
Instead, an administrator mints a long-lived API token bound to a principal user,
and the machine exchanges that token for a short-lived access JWT whenever it
needs one. There is no refresh token on this path: when the access JWT expires,
the machine re-exchanges its API token.

Machine-token management lives in the auth service under `/api/auth/tokens`.

### 1. An admin mints an API token

`POST /api/auth/tokens` (admin or superadmin).

Request fields:

- `name` (string, required): a human label for the token.
- `principal_id` (UUID, required): the user the token acts as.
- `role` (one of `user`, `admin`, `superadmin`, required): the role the token
  grants. A token's role can never exceed the principal's own role; a request for
  a higher role returns `400`.
- `expires_at` (ISO 8601 datetime, optional): when the token stops working. Omit
  for a non-expiring token.

The response carries the raw token in the `token` field. It is shown exactly once
and is stored only as a hash, so it can never be retrieved again. Capture it at
creation time and store it as a secret in your CI/CD system.

```bash
curl -sS -X POST https://herd.example.com/api/auth/tokens \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "ci-pipeline",
        "principal_id": "00000000-0000-0000-0000-000000000001",
        "role": "user"
      }'
# 201 Created
# {
#   "id": "11111111-1111-1111-1111-111111111111",
#   "name": "ci-pipeline",
#   "principal_id": "00000000-0000-0000-0000-000000000001",
#   "role": "user",
#   "expires_at": null,
#   "token": "raw-token-shown-exactly-once"
# }
```

Admins can list tokens with `GET /api/auth/tokens` (metadata only: `id`, `name`,
`principal_id`, `role`, `is_active`, `expires_at`, `last_used_at`, `created_at`,
never the raw value or its hash) and revoke one with
`DELETE /api/auth/tokens/{id}` (idempotent: revoking a missing or already-revoked
token is a no-op).

### 2. A machine exchanges the token for an access JWT

`POST /api/auth/tokens/exchange` (public, no authentication required).

```bash
ACCESS_JWT=$(curl -sS -X POST https://herd.example.com/api/auth/tokens/exchange \
  -H "Content-Type: application/json" \
  -d '{"token": "raw-token-shown-exactly-once"}' \
  | jq -r .access_token)
# 200 OK
# { "access_token": "<jwt>", "token_type": "bearer", "expires_in": 1800 }
```

The exchange mints a short-lived access JWT (`auth_source=api_token`) carrying the
token's role, with no refresh token. `expires_in` is the JWT lifetime in seconds.
Any failure (unknown, revoked, or expired token, or an inactive principal)
returns the same generic `401`, so the response never reveals which condition
failed.

### 3. The machine calls `/api/v1` with the access JWT

Send the access JWT as a bearer token on every `/api/v1` request:

```
Authorization: Bearer <access_jwt>
```

Because the role lives in the JWT, revoking the API token (or letting it expire)
stops new exchanges; already-issued access JWTs remain valid until they expire on
their own, which is why the access lifetime is kept short.

## Reserve and release

All reservation endpoints are under `/api/v1` and take the access JWT as a bearer
token. The facade forwards your JWT to the reservations service, so RBAC,
device-group visibility, and ACL grants are enforced downstream exactly as for an
interactive user. Upstream status codes are propagated unchanged (for example a
`403`, `404`, `409`, or `422` from reservations reaches you as the same status),
and an unreachable reservations service surfaces as `503`.

### POST /api/v1/reservations

Reserve one or more devices. Returns `201`.

Request fields:

- `device_ids` (list of UUID, required, 1 to 200 entries).
- `start_time` (ISO 8601 datetime, required).
- `end_time` (ISO 8601 datetime, required).
- `purpose` (string, optional, up to 2000 characters).
- `topology_id` (UUID, optional).

Response (`V1ReservationResponse`): `id`, `status`, `device_ids`, `topology_id`,
`start_time`, `end_time`, `created_at`. `status` is a plain string so the v1
contract does not couple to the internal status enum.

### GET /api/v1/reservations

List the caller's reservations. Query parameters `skip` (default 0) and `limit`
(default 50, max 500). Returns `{ items, total, skip, limit }`.

### GET /api/v1/reservations/{id}

Fetch one reservation's current status. Ownership and visibility are enforced
downstream.

### DELETE /api/v1/reservations/{id}

Cancel a reservation. Returns `204`.

### PUT /api/v1/reservations/{id}/release

Release a reservation early, freeing its devices before the scheduled end time.
Returns the updated `V1ReservationResponse`.

### GET /api/v1/reservations/{id}/wiring-status

Read the reservation's layered per-connection wiring status: what HERD actually
applied to the hardware for this reservation, row by row, across all three
layers (ADR 0009). Read-only; allowed for any reservation status, so an ended
reservation's response is its as-built record. Ownership and visibility are
enforced downstream exactly as for the other endpoints.

Response:

- `reservation_id` (UUID).
- `last_applied_fork_version` (integer or null): the topology-fork version the
  hardware state was last reconciled to; null before the first apply.
- `frozen` (boolean): true once the reservation has ended and its wiring is
  torn down (no further builds will be applied).
- `connections`: one row per wiring-ledger entry. Common fields on every row:
  `id`, `switch_device_id`, `layer`, `status` (`ACTIVE` applied, `RELEASED`
  removed, `FAILED` the driver call failed), `intended` (`ACTIVE` the row's
  last write was a build, `RELEASED` it was a release), `attempts`,
  `last_error` (string or null), `retryable` (boolean: whether the failure is
  a transient driver error; false means the recorded intent can no longer be
  applied and recovery is a topology re-save), `created_at`, `released_at`.
  Layer-specific fields:
  - `layer: "l1"` (switch cross-connect): `port_a`, `port_b`,
    `physical_connection_id` (UUID or null).
  - `layer: "l2"` (VLAN membership): `port`, `vlan_assignment_id`, `vlan`
    (integer, or null while the allocation is unresolved).
  - `layer: "l3"` (per-switch route pin): `route_count`.

The payload is relayed verbatim from the internal wiring surface and evolves
additively: treat unknown fields (and unknown `layer` values) as
forward-compatible extensions. The manual wiring retry channel is not exposed
through this API; retry is an interactive/operator action.

### Worked example: reserve, poll status, release

```bash
BASE=https://herd.example.com/api/v1
AUTH="Authorization: Bearer $ACCESS_JWT"

# Reserve
RID=$(curl -sS -X POST "$BASE/reservations" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
        "device_ids": ["22222222-2222-2222-2222-222222222222"],
        "start_time": "2026-07-01T09:00:00Z",
        "end_time":   "2026-07-01T17:00:00Z",
        "purpose":    "nightly regression run"
      }' | jq -r .id)

# Poll status until it is ACTIVE
curl -sS "$BASE/reservations/$RID" -H "$AUTH" | jq .status

# Inspect the applied wiring, layer by layer
curl -sS "$BASE/reservations/$RID/wiring-status" -H "$AUTH" \
  | jq '.connections[] | {layer, status, last_error}'

# Release early when the job finishes
curl -sS -X PUT "$BASE/reservations/$RID/release" -H "$AUTH" | jq .status
```

## Webhooks

Register an HTTP endpoint and HERD will POST a signed JSON payload to it whenever
a subscribed reservation event occurs. Subscription management is admin-only and
lives under `/api/v1/webhooks`.

### Registering an endpoint

`POST /api/v1/webhooks` (admin).

Request fields:

- `target_url` (string, required): an `http://` or `https://` URL.
- `event_types` (list of string, required, at least one): the events to deliver.
- `secret` (string, optional): the shared HMAC secret. If omitted, HERD generates
  one and returns it in the creation response.
- `description` (string, optional).

The creation response includes the `secret` exactly once. It is never echoed
again on list or get, so capture it when you register.

```bash
curl -sS -X POST https://herd.example.com/api/v1/webhooks \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
        "target_url": "https://ci.example.com/hooks/herd",
        "event_types": ["reservation.created", "reservation.failed"],
        "description": "notify the CI orchestrator"
      }'
# 201 Created; response includes the generated "secret" once.
```

Other subscription endpoints (all admin):

- `GET /api/v1/webhooks`: list subscriptions (secret omitted).
- `GET /api/v1/webhooks/{id}`: one subscription (secret omitted).
- `DELETE /api/v1/webhooks/{id}`: delete a subscription.
- `GET /api/v1/webhooks/{id}/deliveries`: the delivery ledger for a subscription
  (see Delivery semantics).

### Event types

A subscription may subscribe to any of:

- `reservation.created`
- `reservation.updated`
- `reservation.cancelled`
- `reservation.completed`
- `reservation.failed`
- `reservation.expiring_soon`

An unknown event type in `event_types` is rejected at registration.

### Delivery payload

The body is the reservation lifecycle event as JSON. Every payload carries an
`event` discriminator and an `event_id` (the stable per-event id used for
idempotency), plus event-specific fields.

Example `reservation.created`:

```json
{
  "event": "reservation.created",
  "event_id": "5f0b2c8e-1d4a-4b6e-9a3c-7e2f1d0c9b8a",
  "reservation_id": "33333333-3333-3333-3333-333333333333",
  "user_id": "00000000-0000-0000-0000-000000000001",
  "device_ids": ["22222222-2222-2222-2222-222222222222"],
  "topology_id": "44444444-4444-4444-4444-444444444444",
  "topology_type": "PHYSICAL",
  "start_time": "2026-07-01T09:00:00+00:00",
  "end_time": "2026-07-01T17:00:00+00:00"
}
```

Example `reservation.failed`:

```json
{
  "event": "reservation.failed",
  "event_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "reservation_id": "33333333-3333-3333-3333-333333333333",
  "user_id": "00000000-0000-0000-0000-000000000001",
  "device_ids": ["22222222-2222-2222-2222-222222222222"],
  "topology_id": "44444444-4444-4444-4444-444444444444",
  "topology_type": "PHYSICAL"
}
```

Field sets vary by event. For example `reservation.updated` adds
`added_device_ids` and `removed_device_ids`, and `reservation.expiring_soon`
carries `reservation_id`, `user_id`, `device_ids`, and `end_time`. Treat the
payload as additive: read the fields you need by name and ignore any you do not
recognize.

### Verifying the signature

Every delivery carries an `X-HERD-Signature` header of the form
`sha256=<hex>`, where `<hex>` is the lowercase hex HMAC-SHA256 of the exact
request body bytes, keyed by the subscription secret. Recompute it over the raw
body and compare with a constant-time check before trusting the payload.

```python
import hashlib
import hmac


def verify(raw_body: bytes, header_value: str, secret: str) -> bool:
    """Return True if X-HERD-Signature matches the body, else False.

    `raw_body` must be the exact bytes received, not a re-serialized dict:
    re-encoding can reorder keys or change whitespace and break the HMAC.
    """
    if not header_value.startswith("sha256="):
        return False
    sent = header_value[len("sha256="):]
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)
```

A Flask receiver would call it as:

```python
@app.post("/hooks/herd")
def receive():
    raw = request.get_data()  # raw bytes, before any JSON parse
    sig = request.headers.get("X-HERD-Signature", "")
    if not verify(raw, sig, WEBHOOK_SECRET):
        abort(401)
    event = json.loads(raw)
    handle(event)
    return "", 200
```

### Delivery semantics

- At-least-once. A delivery can arrive more than once (for example after a
  message redelivery), so your receiver must be idempotent.
- Idempotent on `event_id`. HERD itself dedupes per `(subscription, event_id)`:
  once an event is `delivered` to a subscription, a redelivery of that event is
  skipped. Use the payload `event_id` as your own idempotency key too.
- Retried with backoff. A timeout, connection error, or non-2xx response is
  retried a bounded number of times with exponential backoff.
- Dead-lettered on exhaustion. When the retries are exhausted, HERD records a
  ledger row with status `dead`. A failing endpoint never blocks delivery to
  other subscriptions and never stalls the event stream.
- Return 2xx promptly. Your endpoint should acknowledge quickly and do slow work
  asynchronously; a slow receiver is bounded by the delivery timeout and counts
  as a failed attempt.

Inspect what happened with `GET /api/v1/webhooks/{id}/deliveries` (admin), newest
first. Each ledger row carries `event_id`, `event_type`, `status` (`delivered`
or `dead`), `attempts`, `response_status`, `last_error`, `created_at`, and
`delivered_at`.

## Versioning and deprecation policy

`/api/v1` is a frozen contract. The v1 request and response schemas are declared
independently of the internal services and are guarded by a contract snapshot
test, so an internal refactor cannot silently change the external shape. Only
additive, backward-compatible changes ship within v1: new optional request
fields and new response fields. Renaming or removing a field, retyping one, or
changing required-ness is a breaking change and does not ship under v1.

Breaking changes ship under a new version prefix (`/api/v2`) that runs alongside
the existing version. When a successor version reaches general availability, the
superseded version stays supported for at least 6 months, with the deprecation
announced in the release notes and a removal date stated there. Clients should
pin to a version prefix and migrate within the window.

The published machine-readable contract for the current version is
[api/v1-openapi.json](api/v1-openapi.json).
