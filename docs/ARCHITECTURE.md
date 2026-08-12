# Architecture

Public-facing architecture overview for contributors. For installation, see [FRESH_SETUP.md](../FRESH_SETUP.md). For admin flows, see [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md).

HERD is a collection of FastAPI services behind Traefik, fronting a React SPA. Services are independent: each has its own Postgres schema, its own migrations, and communicates with peers over HTTP (+ NATS for events).

## Services at a glance

The React (Vite) frontend calls Traefik, which terminates TLS and routes each
`/api/<service>` prefix to one FastAPI service. The services share two backing stores:
PostgreSQL 16 (a schema per service, no cross-schema joins) and NATS JetStream for events.

| Service | Responsibility |
|---|---|
| config | Standalone configuration UI, no DB |
| auth | JWT auth (local or LDAP/AD), RBAC, user/group mgmt |
| inventory | Templates, devices, ports, drivers, device groups, hypervisor registry |
| reservations | Time-window reservations, conflict detection, auto-expire |
| cabling | Connections, topology persistence, pathfinding |
| acl | Resource-level grants (topology, reservation) |
| execution | Driver execution subprocess, NATS consumer (DLQ) |
| ai-orchestrator | LLM topology generation, reservation assistant, recipe drafting (feature-gated) |
| user-profile | Per-user preferences (saved filters, page sizes, extras) |
| notifications | NATS consumer + in-app notifications + prefs proxy |
| integration | Versioned `/api/v1` reservation facade + outbound webhooks (NATS consumer) |
| secrets | Encrypted-at-rest credential store (envelope encryption, ACL-gated) |

Each service has its own Docker container, its own Alembic migration chain, and its own `app/config.py` settings.

## Cross-cutting technology

- **Python 3.12** with async FastAPI + SQLAlchemy 2.x + asyncpg. Unit tests use SQLite in-memory via aiosqlite.
- **TypeScript + React 19** for the frontend; React Flow for the topology canvas; Zustand + TanStack Query for state.
- **uv workspace** manages Python deps; `herd-common` is the shared package with auth factory, logging, retry, and config-loader utilities.
- **Alembic** for migrations. Unit tests use `Base.metadata.create_all` and don't run migrations; integration/prod do.
- **Traefik v3** for TLS termination and path-prefix routing. File provider dynamic config.
- **Docker Compose** is the target; each service has a Dockerfile.

## Auth and RBAC

Three roles (`user`, `admin`, `superadmin`) encoded in a JWT signed with a shared `AUTH_SECRET_KEY`. Every service verifies tokens locally via `herd_common.auth.make_auth_dependencies`. No central gateway: each service enforces its own RBAC.

Credential verification is pluggable via `AUTH_METHOD` (see [ENV_VARS.md](ENV_VARS.md#auth)):

- `local` (default): bcrypt-hashed passwords in the `auth.users` table. `/register` is open.
- `ldap`: the auth service binds against a configured LDAP / Active Directory server on every login. Accounts are provisioned just-in-time (`auth_source='ldap'`, no local hash) on first successful bind; `/register` returns 409. HERD role and group assignment still happen inside HERD; LDAP groups are not mirrored today (directory group mapping and sync is designed in ADR 0011, `docs/design/0011-ldap-group-sync.md`, and in delivery: phase 1, the directory group client, has landed).

See [ROLES.md](ROLES.md) for the endpoint-level matrix and [USER_GUIDE.md](USER_GUIDE.md) for the user-facing view.

## Inter-service communication

Two auth modes, picked per call site based on whether per-user RBAC needs to apply downstream:

- **Forwarded user JWT** (`Authorization: Bearer {token}`): the downstream re-decodes and enforces RBAC / visibility. Example: inventory forwards the caller JWT to auth to resolve group memberships.
- **Internal token** (`X-Internal-Token: {INTERNAL_API_TOKEN}`): shared secret, used for service-to-service calls with no user in the flow or for follow-up writes where the originating service already authenticated the user. Example: reservations flips device status in inventory via `POST /devices/{id}/status`.

Endpoints that accept internal tokens are named with suffixes like `/internal` or `/internal-download` so the mode is obvious at the call site.

### Failure-handling policy

Three patterns are in active use:

1. **Strict (raise 503):** user-facing reads that feed the UI. Current sites: inventory's `_fetch_user_group_ids` and `_fetch_user_group_names`, execution's `fetch_device` and `fetch_template`, cabling's bulk-import device-name resolution (`resolve_device_names`), and notifications' user-profile preferences read/write all raise `HTTPException(503)` on non-200/connection errors, the cross-service convention for an unreachable dependency (matching the 503 reservations raises for an unreachable inventory). Endpoints that want fail-open layer their own try/except over these.
2. **Fail-open (return None, callers treat as "don't restrict"):** visibility filters where blocking on an outage is worse than allowing through. Sites: `reservations._fetch_visible_device_ids`, `reservations.expiration` (which resolves exclusive device IDs via the best-effort `_fetch_devices_best_effort` and filters inline), `acl.auth_client.fetch_user_groups`.
3. **Best-effort (log + swallow):** side-effect writes where the local state already moved forward. Sites: `reservations._update_device_statuses` (default), also called by `reservations.expiration`.

A fourth opt-in **retry+raise** mode uses `herd_common.retry.retry_with_backoff`. Currently wrapped around `_update_device_statuses` on the reservation create path so a failed reservation lands in `FAILED` instead of silently drifting.

## Event-driven flows (NATS JetStream)

Two source streams carry live work, plus a dedicated `HERD_DLQ` stream that retains dead-lettered messages (see below).

**`HERD_RESERVATIONS`** carries `herd.reservations.*` subjects. The reservations service publishes `reservation.created`, `reservation.updated`, `reservation.cancelled`, `reservation.completed`, `reservation.failed`, `reservation.expiring_soon`, and `reservation.provision_requested`. Three services consume them with independent durable consumers:

- **execution** (`execution-consumer`) triggers driver actions:
  - `reservation.created`: no longer drives any driver action (ADR 0009 phase 7 retired the L1/L2/L3 device-set resolvers); execution consumes it only to flip the reservation's devices to the `in_use` health-poll tier (issue #24). Initial wiring is fork-driven instead: on activation, reservations stages a `reservation.wiring_changed` for the fork's initial version, and the same layered reconcile a fork save drives (L1 cross-connects, L2 VLAN membership, L3 route adjacency) provisions it.
  - `reservation.cancelled / completed`: ledger-driven teardown (ADR 0009 phase 6): disconnect every ACTIVE `l1_connection_assignments` pair, remove every ACTIVE `l2_port_assignments` membership (releasing its VLAN allocation once the last membership leaves), and remove every cleanly-removable `route_assignments` pin, each released only after the driver ran. Any dynamic instances the reservation booked are torn down from the `dynamic_instances` ledger the same way (see Dynamic resources below).
  - `reservation.updated`: no longer touches L1/L2/L3 wiring (ADR 0009 phase 7); an added device is conflict-checked and flipped to `RESERVED` but wires nothing until a fork save draws its connections, and a removed device's wiring is released by the reservations PATCH handler itself: it calls cabling's internal prune-devices operation, which releases the device's rows from the fork's saved intended set (fork_connections; the draft canvas is only scrubbed of the removed device, never treated as wiring intent, so unsaved draft edits are neither built nor released), bumps the fork version, and frees the port claims, and reservations stages the released delta so the release rides the normal wiring_changed apply (the sweep's delta-less heal covers a missed staging). The PATCH also records the removed ids in a pending-prune marker in the same transaction as the edit, and the expiration sweep retries a failed prune each tick until it converges (or drops the marker once the reservation is terminal, where ledger teardown owns the release). The event still drives dynamic instances behind removed devices tearing down and the issue #24 health-poll tier bookkeeping.
  - `reservation.failed`: best-effort teardown of whatever provisioning landed before the failure (issue #244), read from the ledgers rather than `action_succeeded_for_reservation` (ADR 0009 phase 6): L1 disconnect_ports for ACTIVE `l1_connection_assignments` pairs, L2 remove_from_vlan for ACTIVE `l2_port_assignments` memberships (the VLAN allocation is freed in the database, no delete_vlan driver call), L3 remove_route for cleanly-removable `route_assignments` pins, each released only after the driver ran; dynamic instances tear down from the ledger, same as cancelled/completed. A reservation that failed before any provisioning tears down nothing.
  - `reservation.provision_requested`: a separate branch, entirely apart from the L1/L2/L3 flows above, that materializes a dynamic-carrying reservation's requested instances; see Dynamic resources below.
  - `reservation.wiring_changed`: connection-driven L1 reconcile (ADR 0007, issue #345 P3b). Staged by reservations when a fork save reconciles the intended wiring, it carries the released/built L1 wire delta so execution re-wires the hardware connection-by-connection: disconnect the released switch port pairs, then connect the built ones, applying each recorded hop verbatim (no re-pathfinding at apply time). The applied state is now connection-addressable in an `l1_connection_assignments` ledger (per switch port pair, status `ACTIVE`/`RELEASED`/`FAILED` with `attempts` and `last_error`) and a per-reservation `reservation_wiring_state` row (`last_applied_fork_version` for ordering plus idempotency, and a `frozen` flag set on teardown so a late `wiring_changed` is a safe no-op). Ordering is enforced consumer-side: a stale or duplicate version is a no-op, a contiguous version applies the carried delta, and a gap or a delta-less sweeper heal fetches the full intended L1 set from cabling and reconciles convergently. A hardware apply failure never rolls back the durable save; it lands a `FAILED` row that a background auto-retry channel reattempts with backoff (the `WIRING_RETRY_*` knobs), and a hardware-retryable row can also be retried manually; since issue #491 both channels first revalidate a build-direction row against cabling's current intended set and settle a stale build (its wiring re-saved away) without driving hardware, failing closed when the intended set cannot be read. L2 VLAN membership is now connection-driven too (ADR 0009 phase 4, issue #416): the same `wiring_changed` apply, after the L1 pass, derives each reservation's intended per-port memberships from the recorded hops (a hop terminating on a Layer 2 Switch joins that switch port to the reservation's VLAN; an inter-switch trunk contributes none) and reconciles them against an `l2_port_assignments` membership ledger (split from the per-fabric `vlan_assignments` VLAN-number allocation), with the same result gating, direction-aware retry, and freeze semantics as L1. L3 route adjacency is connection-driven the same way (ADR 0009 phase 5): the same apply, after the L2 pass, derives switch adjacency from the fork's L3 rows and reconciles it against `route_assignments`, releasing adjacency-aware (a switch still serving another intended L3 edge keeps its routes). Since ADR 0009 phase 7, this same reconcile also drives initial provisioning: on activation, reservations stages a `reservation.wiring_changed` for the fork's initial version instead of running the retired device-set resolvers, so an added device wires nothing until a fork save draws its connections and a removed device's wiring releases via a staged heal.
  - Idempotent on redelivery: each mutating driver action records a stable `dedupe_key` on its `execution_runs` row, and the consumer skips any action whose `SUCCESS` run already carries that key. The key is the producer-stamped payload `event_id` (the outbox row id, issue #21), which survives a relay republish under a new stream sequence, falling back to the source message's NATS `stream:sequence` for events published before the outbox existed. A NAK retry re-runs only the action that failed, never the ones that already applied (`login`/`logout` are not deduped).
- **notifications** (`notifications-consumer`) turns the same events into per-user in-app notifications, gated by the user's `extras.notifications` preferences in user-profile. `reservation.provision_requested` is not one of the events it (or the integration webhooks consumer below) reacts to; it exists solely to trigger the execution-side create flow.
- **integration** (`integration-webhooks-consumer`) fans each event out to admin-registered outbound webhooks as HMAC-signed POSTs, with a delivery ledger and dead-letter record (issue #33); see the integration-service section below.

**`HERD_HEALTH`** carries `herd.health.status_changed`. The execution service's health-poll scheduler publishes a `device.health_transition` event when a polled device's `consecutive_failures` crosses the configured threshold (bad_news) or resets to zero (recovery). The notifications service consumes this stream with its own durable consumer (`notifications-health-consumer`) and fans the event out as in-app notifications to all admins plus any users with an active reservation on the device.

All consumers use `ConsumerConfig(max_deliver=5, ack_wait=30, backoff=[1,5,15,60,120])` and publish poison messages (JSON decode errors) and exhausted deliveries to service-scoped DLQ subjects: `herd.reservations.dlq.execution` (execution), `herd.reservations.dlq.notifications` (notifications, reservations stream), `herd.reservations.dlq.integration` (integration webhooks), and `herd.health.dlq.notifications` (notifications, health stream). See [OPERATIONS.md](OPERATIONS.md#inspecting-the-nats-dlq) for inspection.

### Transactional outbox (durable event delivery, issue #21)

The lifecycle and health events above were once published to JetStream after the database transaction committed. That is a dual-write: if the process died or NATS was unreachable in the window after the commit, the event was lost with no record that one was owed, so a reservation could reach `ACTIVE` (or a device could change health state) while the provisioning or notification event never fired. The transactional outbox closes that gap.

Each producing service (reservations and execution) owns its own `outbox` table (reservations migration `0010`, execution migration `0010`); schema-per-service is preserved, there is no shared outbox table. The event row is staged in the same transaction as the state change it describes via `enqueue_event` (`herd_common.outbox`), so the event exists if and only if that transition committed. A background relay (`run_outbox_relay`) then drains it: it claims unpublished rows with `FOR UPDATE SKIP LOCKED` so multiple relay instances never double-publish, publishes each to JetStream with a `Nats-Msg-Id` header set to the outbox row id for publisher-side dedup within the stream's dedup window, marks the row published, and periodically prunes published rows past the retention window. A publish failure leaves the row unpublished and backs the relay off exponentially, so a NATS outage delays delivery instead of dropping it; the relay drains the backlog on the first healthy tick after the outage, which is what makes restart-recovery work.

Consumers stay idempotent on a stable key resolved by `event_dedupe_key`: the producer stamps an `event_id` into each payload, and consumers key their existing dedupe ledgers on it (execution's `execution_runs` dedupe index, notifications' `(user_id, dedupe_key)` rows and `outbound_deliveries` ledger). Because `event_id` is stable, a relay republish under a new stream sequence is still recognized as a duplicate; the key falls back to `<stream>:<sequence>` for events published before the outbox existed, so a rolling deploy stays correct.

A third consumer on `HERD_RESERVATIONS` lives in the `integration` service (durable `integration-webhooks-consumer`, DLQ subject `herd.reservations.dlq.integration`): it fans each reservation event out to every registered outbound webhook (see below).

## Integration service: external API and webhooks (issue #33)

The `integration` service is the stable external surface for automation, decoupled from the internal UI endpoints. It has two halves.

**Versioned `/api/v1` reservation facade.** Traefik routes `/api/v1` to the integration service (stripping the prefix). The facade is a thin, synchronous hop that forwards the caller's bearer JWT to the reservations service, so RBAC, device-group visibility, and ACL grants are enforced downstream as the real user; the facade only validates that a JWT is present, freezes the v1 request/response contract (`app/schemas/reservation.py`, decoupled from reservations' internal schemas), and propagates upstream status codes (a slow or unreachable upstream surfaces as `503`). Endpoints: `POST /reservations` (reserve), `GET /reservations`, `GET /reservations/{id}`, `DELETE /reservations/{id}` (cancel), `PUT /reservations/{id}/release`.

**Machine-token exchange.** Long-lived API tokens are minted by an admin in the auth service (`POST /api/auth/tokens`, role-capped at the principal's own role) and stored only as a hash. A machine exchanges its raw token at the public `POST /api/auth/tokens/exchange` for a short-lived access JWT (`auth_source=api_token`, no refresh token), then sends that JWT to `/api/v1`. Revocation is `DELETE /api/auth/tokens/{id}`.

**Outbound webhook consumer and delivery.** Admins register subscriptions (`POST /api/v1/webhooks`: target URL, subscribed event types, shared HMAC secret). The durable consumer above loads every active subscription matching an event and POSTs a signed copy to each concurrently. Each delivery is signed `X-HERD-Signature: sha256=<hex>` (HMAC-SHA256 over the raw body bytes, keyed by the subscription secret; `herd_common.webhooks.sign_body`), retried with backoff, and recorded in a `webhook_deliveries` ledger that doubles as the at-least-once dedup record (unique on `(subscription_id, event_id)`) and the dead-letter record (`status="dead"` on retry exhaustion). A failing receiver lands a `dead` ledger row and never NAKs the NATS message, so one bad target cannot re-fan-out to the others or stall the stream. See [EXTERNAL_API.md](EXTERNAL_API.md).

## Secrets service: encrypted-at-rest credential store (issue #39)

The `secrets` service holds named secrets whose payloads are envelope-encrypted:
values are AES-GCM-encrypted by a data-encryption key (DEK), and each DEK is
stored only wrapped by the key-encryption key supplied via `SECRETS_KEK`
(base64, 32 bytes). A database dump alone never yields plaintext. GCM associated
data binds every ciphertext to its (secret id, key version) pair and every
wrapped DEK to its version, so moving ciphertext between rows fails the auth tag
instead of decrypting. The service refuses to boot on missing, malformed, or
mismatched key material.

Authorization rides the ACL service's `secret` resource type: admins manage
secrets; a group `manage` grant reveals plaintext (`GET /secrets/{id}/value`),
a `view` grant sees metadata only. `GET /internal/secrets/{id}/value` (and
`/internal/secrets/by-name/{name}/value`) is the `X-Internal-Token` surface
that automated provisioning consumes: inventory calls it once at hypervisor
registration to confirm a referenced secret exists, and execution's recipe
runner calls it on every dynamic-instance create or teardown to resolve the
hypervisor's credential (see Dynamic resources below).
`POST /keys/rotate` introduces a new DEK version and re-encrypts every secret;
KEK rotation is `SECRETS_KEK_PREVIOUS` plus a restart, which re-wraps stored
DEKs without touching secret rows. Plaintext appears only in value responses,
never in metadata, logs, or events. See
[docs/design/0003-encrypted-credential-store.md](design/0003-encrypted-credential-store.md).

## Reservation state machine

Transitions (from state, to state, trigger):

| From | To | Trigger |
|---|---|---|
| PENDING | PENDING_PROVISION | expiration task claims a scheduled reservation at start_time |
| PENDING | CANCELLED | user cancels before activation |
| PENDING_PROVISION | ACTIVE | inventory status flip succeeds (physical-only reservations), or the execution service's provision-result callback reports success (dynamic-carrying reservations) |
| PENDING_PROVISION | FAILED | provisioning retries exhausted (physical-only reservations), or the provision-result callback reports failure, or the provisioning timeout backstop fires with no callback (dynamic-carrying reservations) |
| PENDING_PROVISION | PENDING | scheduled-path flip failure retries next tick, or the restart backstop reclaims a physical-only reservation stranded past `PROVISION_TIMEOUT_SECONDS` (issue #318) |
| ACTIVE | COMPLETED | end_time passes (expiration task) |
| ACTIVE | CANCELLED | user cancels an active hold |

- `PENDING` is the state for scheduled-future reservations. A booking whose `start_time` is more than `RESERVATION_START_GRACE_SECONDS` ahead is created `PENDING` with no provisioning (its row still holds the window for conflict detection); within the grace, a "start now" booking is provisioned immediately. The expiration task provisions a `PENDING` reservation at `start_time`: it claims the row to `PENDING_PROVISION`, flips inventory, enqueues `reservation.created` to the outbox in the same transaction, and creates the fork, the same work the immediate path does. A flip failure at activation reverts the claim to `PENDING` so a later tick retries (it does not `FAIL` the booking).
- `PENDING_PROVISION` is the transient provisioning state, entered either at create (immediate booking) or when the expiration task claims a scheduled `PENDING` reservation. For a physical-only reservation, the inventory status flip retries; success to `ACTIVE`, exhausted retries to `FAILED` (immediate path) or revert to `PENDING` (scheduled path). A reservation carrying any `dynamic_requests` (ADR 0004, issue #32) always books through `PENDING_PROVISION`, even with no exclusive devices to flip, and stays there until the execution service's `POST /internal/{reservation_id}/provision-result` callback (`X-Internal-Token`, idempotent per reservation) resolves it: success attaches the materialized device ids and activates exactly like the physical path (staging `reservation.created` for health-poll tiering and the fork, whose activation-staged `reservation.wiring_changed` provisions the initial wiring, once the dynamic devices exist); failure lands `FAILED` and releases any exclusive physical devices back to `AVAILABLE`. A duplicate or late callback is a no-op. Because a lost callback would otherwise strand the reservation, the expiration task's cycle also fails any `PENDING_PROVISION` reservation with dynamic requests whose row has not been touched in `PROVISION_TIMEOUT_SECONDS` (default 900s; `0` disables the backstop). A separate branch of the same cycle covers physical-only rows (issue #318): a physical-only reservation resolves its `PENDING_PROVISION` state inline (the immediate create path, or the scheduled claim's activation), never via a callback, so a process restart in the inventory-flip window would otherwise strand it. That branch reverts a physical-only `PENDING_PROVISION` row stranded past the same `PROVISION_TIMEOUT_SECONDS` back to `PENDING` (a compare-and-swap, so a late in-process activation stays the winner), and a later cycle re-runs the flip and activation; nothing is torn down because `reservation.created` commits atomically with the `ACTIVE` transition, so a stranded row never emitted it. The `NOT EXISTS`/`EXISTS` dynamic-request pair partitions `PENDING_PROVISION` so the two backstops never both touch one row. See Dynamic resources below.
- `FAILED` rows persist for audit but hold no devices.
- `CANCELLED` and `COMPLETED` are terminal states.
- `_check_conflicts` treats `PENDING`, `PENDING_PROVISION`, and `ACTIVE` as conflicting; two concurrent creates for the same exclusive device race safely.
- On reaching `ACTIVE`, if the reservation has a topology, the reservations service calls `POST /api/cabling/internal/forks` (guarded by `X-Internal-Token`) to create an editable per-reservation fork of the parent topology, pinned to the parent's current version (issue #25, partial). The fork is owned by the cabling service in three tables: `reservation_fork` (fork identity and canvas, keyed by a bare `reservation_id` UUID, no cross-schema FK), `fork_connections` (the wiring snapshot for the lease, separate from the physical `connections` table), and `fork_versions` (immutable saves numbered sequentially per fork). The endpoint is idempotent on `reservation_id`, so a retried activation returns the existing fork rather than creating a duplicate. This is best-effort: a fork-create failure is logged and does not strand the activated reservation. Cabling also owns the internal fork surface (ADR 0006 P3a, delivered): read the fork (`GET /internal/forks/{reservation_id}`), edit it loosely with no reconcile (`PUT .../canvas`), save it with set-arithmetic reconcile and cross-reservation port-claim 409s (`POST .../save`), and freeze it to an idempotent as-built `ARCHIVED` state (`POST .../archive`). The reservations service now drives that surface for users through `GET /reservations/{id}/fork` (any status; lazy-creates the fork on first read of an ACTIVE reservation), `PUT .../fork/canvas`, and `POST .../fork/save` (both owner-or-admin and ACTIVE-only), and archives the fork from every teardown path with a standing reconciler in the expiration sweep archiving forks of already-ended reservations. The frontend's live-edit mode edits the fork, not the parent, so live edits no longer mutate the shared parent topology or append parent `TopologyVersion` rows; after the reservation ends the fork renders read-only as the immutable as-built record. Editing is available only while the reservation is `ACTIVE`; a `PENDING` reservation has no fork yet and offers no topology editing. L1 provisioning is now connection-driven: a fork save stages `reservation.wiring_changed`, and execution re-wires the affected L1 cross-connects from that delta against its `l1_connection_assignments` ledger, exposing per-connection status and a manual retry through the reservation detail's Wiring tab (ADR 0007, issue #345 P3b, delivered for L1). The device-set PATCH no longer drives wiring (ADR 0009 phase 7): it still drives dynamic-instance add/remove and the issue #24 health-poll tier flips, but an added device wires nothing until a fork save draws its connections, and removing a device releases its wiring from the fork's saved intended set via cabling's internal prune-devices operation, staging the released delta (a durable pending-prune marker written with the edit plus an expiration-sweep reconciler retry a failed release until it converges), so its wiring and port claims release without a manual save and an unsaved draft is never built or released as a side effect. L2 VLAN membership is fork-driven after any save (ADR 0009 phase 4, issue #416): a fork edit that moves a DUT to a different switch port moves its VLAN membership, reconciled against the `l2_port_assignments` ledger. L3 route provisioning is fork-driven the same way (ADR 0009 phase 5), and initial L1/L2/L3 provisioning is now fork-driven too (ADR 0009 phase 7): activation stages a `reservation.wiring_changed` for the fork's initial version instead of running the retired device-set resolvers.

## Topology separation

Physical and cloud devices cannot be mixed in a single reservation or topology. Enforced at three levels:

1. **DB**: `topology_type` enum on devices and reservations.
2. **Service**: `reservation_service` validates all devices match before creating.
3. **UI**: the topology editor blocks cross-type edges with a toast.

## Topology connectivity validation

HERD has no notion of "region" or "location"; the physical cabling graph (the
`Connection` rows owned by the cabling service) is the only authority on whether
two devices can talk. Every edge a user draws in the topology editor is a claim
that a path exists between its endpoints. The same validation runs in two places:

1. **Editor (UX)**: `usePathfindPairs` resolves every edge on canvas load and on
   change. `LayerEdge` renders any edge with `pathValid === false` or
   `portsCabled === false` in red regardless of layer; reachable edges go green.
   The Reserve button is disabled when any edge is invalid.
2. **Reservations service (authority)**: when a reservation references a
   topology, `reservation_service` calls
   `POST /cabling/topologies/{id}/validate/internal` before checking device
   availability. Any unreachable edge produces a 422. Bypassing the UI is
   therefore not sufficient to create a reservation against a disconnected
   topology.

The validator is exposed via two endpoints sharing one implementation:

- `POST /cabling/topologies/{id}/validate` is the user-facing endpoint used by
  the topology editor. JWT-authenticated; restricted to the topology creator
  or admins (validation reveals which device pairs lack physical paths, so it
  carries the same RBAC as topology edit).
- `POST /cabling/topologies/{id}/validate/internal` is the service-to-service
  endpoint used by reservations during create. Guarded by `X-Internal-Token`,
  no JWT. The booking user does not necessarily own the topology being
  reserved, so JWT-forward against the public endpoint would 403.

Both endpoints rebuild the adjacency graph per request via
`build_adjacency_graph`, then resolve every non-proposal, non-missing-device
edge's shortest paths in a single `find_all_shortest_paths_batch_async` call
rather than one BFS per edge (issue #313). When the
caller passes the topology's device set, the rebuild loads only the edges in
that connected component (iterative frontier expansion, so off-canvas
intermediates that realize a topology edge are still pulled in) rather than
scanning the whole connections table; passing no device set falls back to a
full load. Path enumeration is capped at `MAX_ENUMERATED_PATHS` (256). Serving
fresh state on every request avoids any cross-process cache-coherence problem
if cabling is ever scaled horizontally. To express overlay links between physically
isolated fabrics (e.g., MPLS between sites), users add a virtual device and
cable to it, which lets the same validator stay strict about physical
reachability.

## Device visibility

- **Admins** see every device.
- **Non-admin users** see only devices in device groups where their user groups have a permission.
- `GET /inventory/devices` filters by visibility inside the DB query (not post-filter) so counts and pagination stay accurate.

## Driver execution

The execution service runs admin-uploaded driver code in a separate subprocess with a
wall-clock timeout and POSIX resource limits, but without OS-level isolation (see the
security note below):

- Package format: `.zip` or `.tar.gz` (<=10 MB) containing a `driver.py` with a `Driver` class.
- Required methods per connection type (from `driver_loader.REQUIRED_METHODS`):
  - `Management`: login, logout, configure, backup, status
  - `Layer 1 Switch`: login, logout, connect_ports, disconnect_ports, status
  - `Layer 2 Switch`: login, logout, create_vlan, add_to_vlan, remove_from_vlan, delete_vlan, status
  - `Layer 3 Switch`: login, logout, configure_route, remove_route, status
  - `Hypervisor`: login, logout, create_instance, destroy_instance, status. This is a dynamic-resources recipe (ADR 0004, issue #32), not a physical-device driver; see Dynamic resources below.
- Runtime: device context is passed via a temp JSON file. Non-secret context values are also exposed as `HERD_`-prefixed env vars for observability; password-typed fields are stripped from the env and reach the driver only through the temp file. Method kwargs go via argv. Timeout defaults to 30s (10s for `status`); a `Hypervisor` recipe's actions instead run under `RECIPE_TIMEOUT_SECONDS` (default 300s), since a hypervisor create or destroy can take minutes.
- Resource limits: the child gets POSIX `setrlimit` caps for address space (256 MB), CPU time (60s), open files, and processes, each configurable (0 disables a limit). A driver killed by a limit is recorded as failed with the signal.
- Dependencies: a package `requirements.txt` is installed at runtime only when `ALLOW_DRIVER_PIP_INSTALL` is set; otherwise drivers vendor deps into `_deps/`. Off by default because a runtime `pip install` pulls network code as the service user.
- Driver cache: local disk at `/data/driver-cache/`, keyed by SHA256. Stale caches auto-invalidate.

Security note: this is process separation with resource caps, not a security sandbox. There is no namespace, seccomp, filesystem, or network isolation, and the child runs as the execution service's own user, so a driver can reach the network and read files that user can read. Driver upload is admin-only for this reason; driver packages are trusted code. The resource limits are POSIX-only.

See [DRIVERS.md](DRIVERS.md) for the developer-facing contract and packaging guide.

## Dynamic resources: hypervisor-backed templates (ADR 0004, issue #32)

A `dynamic` template type materializes an instance per reservation from a registered
hypervisor, instead of referencing a pre-existing device row.

**Hypervisor registry (inventory).** Admins register hypervisors at `POST /hypervisors`
(name, endpoint, a free-string `hypervisor_type` such as `proxmox`/`vsphere`/`libvirt`,
and a `secret_id` referencing the secrets service, validated once at registration and
re-validated only when a later `PUT` actually changes `secret_id`); `GET
/hypervisors/{id}/internal` (`X-Internal-Token`) is the execution-side read. Deleting a
hypervisor is blocked (409) while any template still references it. A dynamic template
(`template_type="dynamic"`) requires both `driver_id` (a `Hypervisor`-connection-type
recipe package) and `hypervisor_id`; its field sections describe instance parameters
(image, cpu, memory) exactly like a device template's fields. `POST /devices` continues
to reject any non-`"device"` template; dynamic instances enter inventory only through
the internal path below.

**Booking.** `ReservationCreate.dynamic_requests` lists the dynamic templates to
materialize (one entry per instance requested, no dedup, capped at 50 per reservation).
A reservation carrying any dynamic request always books through `PENDING_PROVISION` and
never straight to `ACTIVE` (see Reservation state machine above), whether created
immediately or claimed by the expiration task's scheduled path; once any exclusive
physical devices have flipped to `RESERVED`, reservations stages a
`herd.reservations.provision_requested` outbox event.

**Provisioning (execution).** The execution consumer's `reservation.provision_requested`
handler, a branch entirely separate from the L1/L2/L3 flows, processes each requested
instance: fetch the template, its hypervisor, and the hypervisor's secret value; load
the recipe package; run `login`, `create_instance`, `logout` in the sandbox under
`RECIPE_TIMEOUT_SECONDS`. On a successful `create_instance`, `POST /devices/internal`
materializes the instance as a real inventory device (status `RESERVED`, joined to the
"No Pool" device group like every device, `field_data` merged from the request
parameters and the recipe's result), and the outcome is recorded in execution's
`dynamic_instances` ledger (id, reservation_id, template_id, hypervisor_id, device_id,
instance_ref, status `CREATING`/`ACTIVE`/`DESTROYED`, error, timestamps): the direct peer
of the L2 `VlanAssignment` and L3 `RouteAssignment` applied-state ledgers. A recipe must
derive its hypervisor-side resource name deterministically from the request's
`HERD_request_id` context key, so a NATS redelivery of the same create converges on the
same instance rather than leaking a duplicate (see [DRIVERS.md](DRIVERS.md) for the full
context contract). Once every requested instance for the reservation reaches `ACTIVE`,
execution posts the result to reservations; the ledger row insert happens before any
hypervisor action runs, so a NAK mid-create leaves a row an idempotent retry can resume
against.

**Activation callback and timeout backstop (reservations).** This closes a gap the
design explicitly called out: before this feature, execution had no way to report a
provisioning outcome back to reservations. `POST
/internal/{reservation_id}/provision-result` fills it; see Reservation state machine
above for the transition semantics.

**Teardown.** `reservation.completed`, `.cancelled`, and `.failed` drive execution to
tear down every `CREATING`/`ACTIVE` ledger row for that reservation (`reservation.updated`
tears down only the rows behind removed devices): `login`, `destroy_instance`, `logout`,
then delete the materialized device (`DELETE /devices/{id}/internal`, where a 404 counts
as already gone), then mark the row `DESTROYED`. Mirroring the L3 applied-state
discipline (issue #244): a driver-result failure from `destroy_instance` leaves the row
`ACTIVE` as an accurate "instance may still exist" record rather than raising, while a
transient upstream error NAKs the message for redelivery; `destroy_instance` must
therefore be idempotent, since a redelivered teardown (or one that finds the instance
already gone) must still return success. A row with no `instance_ref` (nothing was ever
created hypervisor-side) is retired directly.

See [docs/design/0004-dynamic-resources.md](design/0004-dynamic-resources.md) for the
accepted design and [DRIVERS.md](DRIVERS.md) for the Hypervisor driver contract.

## AI orchestrator

Calls a configurable LLM provider via tool-use with a strict JSON schema. The provider is selected by `AI_PROVIDER`: `anthropic` (AsyncAnthropic SDK) or `openai_compat` (AsyncOpenAI SDK against any compatible chat-completions endpoint, including vLLM, Ollama, LM Studio, OpenAI, and Azure OpenAI). The orchestrator code sits above an `LLMProvider` Protocol with neutral `Message`, `ContentBlock`, `ToolSchema`, and `ProviderResponse` types; SDK-specific translation is isolated to one file per provider under `services/ai-orchestrator/app/services/providers/`. Three user-facing surfaces:

**Topology generation** (`/api/ai/generate` -> `/api/ai/commit`):

1. `/api/ai/generate` (multipart POST with `prompt` + optional `files[]`) returns a validated proposal with resolved device UUIDs.
2. User reviews as ghost nodes, accepts or rejects.
3. `/api/ai/commit` creates the topology, saves canvas, creates reservation, optionally calls `/execute` per device with allowlisted configs.

Per-device `config` is validated against a registry (`Management` only today; allowlist of `vlan`/`ip`/`hostname`/`description`) before any upstream write. The LLM's tool schema is locked down to the same keys so the model can't emit others at generation time. The tool is built per request: `template_name` carries a JSON Schema enum of the caller's visible templates, so an enum-honoring provider cannot name a template outside the live inventory. The orchestrator still validates the proposal and, on a repairable failure (unknown template, over-count, duplicate role, dangling edge), re-prompts the model once with the allow-list before returning a 502.

**Reservation assistant** (`/api/ai/reservations/{id}/assistant`):

A multi-turn chat. The orchestrator renders a thin seed (reservation metadata + flat device list) once per conversation and persists it as the position-0 user message; subsequent turns append messages and the route enforces (user_id, reservation_id) ownership on every request. The model then calls into a `ToolDispatcher` exposing seven curated read-only tools (`get_device`, `get_device_ports`, `get_device_current_config`, `get_device_config_schema`, `list_device_config_history`, `find_path`, `list_executions_for_reservation`); when `AI_WRITE_TOOLS_ENABLED=true`, the dispatcher additionally exposes `propose_config_change` and `schedule_config_apply` (dry-run by default, user confirms via the UI before any real apply). Each tool resolves to an existing HERD endpoint via the caller's forwarded JWT, so existing RBAC applies. The dispatcher captures the reservation id at construction time and injects it server-side into the executions tool, so the model cannot peek across reservations. Password-typed `field_data` keys are stripped via a per-template cache before any device or port payload reaches the model. Per-turn loop bounds: 8 iterations, 90s overall, 20s per model call, 8000-char cap per tool result. Per-conversation bounds: 40 messages and 60k input-token budget (chars/4 estimate); when either is exceeded the repository evicts the oldest user+assistant pair with the seed pinned. Conversations idle past `ASSISTANT_CONVERSATION_TTL_HOURS` (default 24) are removed by an hourly background sweeper. A streaming twin, `POST /api/ai/reservations/{id}/assistant/stream`, runs the same loop and persistence but returns a `text/event-stream` of `status`/`token`/`done` events so the answer renders as it is produced; see [AI_ASSISTANT.md](AI_ASSISTANT.md).

**Recipe authoring** (`/api/ai/recipes/draft`, then admin review, then the existing `POST /api/inventory/drivers` upload):

Admin-only and dark by default behind `AI_RECIPE_AUTHORING_ENABLED` (403 at the route boundary when off). The orchestrator forces a single structured `draft_recipe` tool call, injects the owned metadata fields (`connection_type`, `supports_dry_run`, provenance) the model is never trusted with, and validates the assembled package through the execution service's internal `POST /internal/validate-package` (X-Internal-Token; the orchestrator's only internal-token call), feeding a red report back to the model for up to `AI_RECIPE_MAX_ATTEMPTS` total attempts. Drafts persist in a `recipe_drafts` table; the drivers-page panel reviews the code, report, and dry-run transcripts, and approval submits the archive through inventory's existing admin upload endpoint under the admin's own JWT. The orchestrator cannot upload a driver. See [AI_RECIPES.md](AI_RECIPES.md) and ADR 0005.

Persistence lives in a new `ai_orchestrator` Postgres schema: `assistant_conversations` and `assistant_messages` tables, an `ai_usage` table (per-user daily AI token accounting for the optional `AI_DAILY_TOKEN_QUOTA`), and the `recipe_drafts` table, managed by Alembic at `services/ai-orchestrator/migrations/`. The assistant was the first DB-backed feature in the orchestrator; before it the service ran fully stateless.

Feature-gated on `ai_is_configured()`: `/api/ai/generate`, `/api/ai/templates/suggest-identity`, `/api/ai/reservations/{id}/assistant`, and `/api/ai/reservations/{id}/assistant/stream` all return 503 when the active provider is unconfigured. Anthropic is configured when either `AI_API_KEY` or `AI_BASE_URL` is set, so it works against the hosted API with a key or a local Anthropic-compatible endpoint (e.g. a vLLM serving `/v1/messages`) with no key; openai_compat needs `AI_BASE_URL`. The unauthenticated `GET /api/ai/status` endpoint returns `{enabled, provider, model, recipe_authoring}` so the frontend can hide the **Use AI** button, the **AI Assistant** tab, and the **Draft with AI** panel when the corresponding features are off. For an on-prem endpoint behind a self-signed certificate, set `AI_CA_CERT` to the in-container path of a mounted CA bundle so verification stays on and fails closed; the orchestrator builds an `ssl.SSLContext` from the bundle and hands it to the SDK. `AI_TLS_VERIFY=false` is also supported but disables verification process-wide and should be avoided when a CA bundle is available.

See [AI_GENERATE.md](AI_GENERATE.md) and [AI_ASSISTANT.md](AI_ASSISTANT.md) for the user-facing flows.

## Per-user preferences and notifications

The **user-profile** service stores a single `user_preferences` row per caller (PK = JWT `sub`) with three JSONB fields: `saved_filters`, `page_sizes`, and a forward-compatible `extras` blob. Endpoints: `GET` (auto-creates), `PUT` (replace), `PATCH` (shallow merge), `DELETE` (reset). A service-to-service `GET /preferences/internal?user_id=...` endpoint, guarded by `INTERNAL_API_TOKEN`, lets peers (currently only notifications) read a user's prefs without a caller JWT.

The **notifications** service consumes two NATS streams with distinct durable consumers: `herd.reservations.*` (`notifications-consumer`) for reservation lifecycle events, and `herd.health.*` (`notifications-health-consumer`) for device health transitions. Both streams write per-user rows into the same `notifications.notifications` table; the consumer pair is split so a stuck health-event subscription cannot stall reservation events and vice versa. Delivery is gated by the user's `extras.notifications` preferences: a pluggable `Dispatcher` protocol wraps each channel, and four peer dispatchers ship today, `InAppDispatcher` (the `notifications` table) plus `EmailDispatcher` (SMTP), `ChatDispatcher` (Slack-style incoming webhook), and `WebhookDispatcher` (HMAC-signed POST). Outbound channels (email, chat, webhook) default off per user; the in-app channel is on by default. Outbound sends are deduped on the stable event key (the producer-stamped payload `event_id`, issue #21, falling back to the source NATS stream and sequence for pre-outbox events) via an `outbound_deliveries` ledger so a redelivery or relay republish never resends, and a failure on one channel is isolated so it does not block the others or in-app. Email and chat resolve the recipient's address or username from auth's `/internal/users/{id}/contact` endpoint. The `PreferencesClient` caches reads in-process for 30s by default (`PREFERENCES_CACHE_TTL_SECONDS`) and fails open on user-profile outages so notifications still fire. For `device.health_transition` events the recipient resolver fans out to a deduped union of all admins (fetched from auth's `/internal/admins`, cached for `HEALTH_NOTIFY_ADMIN_CACHE_TTL_SECONDS`) and any users with an active reservation on the device (fetched per event from reservations' `/internal/active-users`, uncached). A `reservation.expiring_soon` reminder is published by the reservations expiration task within a configurable lead window of a reservation's `end_time` (`EXPIRY_REMINDER_LEAD_SECONDS`), deduped per reservation, and consumed through the existing reservations consumer. REST API at `/api/notifications/notifications` exposes list/unread-count/mark-read/read-all/delete and a GET/PUT `/preferences` proxy that forwards the caller's JWT to user-profile. Frontend surfaces a bell icon with unread badge in the header (30s polling) and a Notifications section on the new `/settings` page.

## Frontend

- **State**: Zustand stores (`authStore`, `topologyStore`, `configStore`, `preferencesStore`). TanStack Query for server state.
- **Forms**: mostly uncontrolled inputs with submit handlers; React Hook Form is not used.
- **Modals**: native `<dialog>` with two primitives (`Modal`, `ConfirmDialog`). Both use a callback ref so the native `cancel` listener survives parent re-renders.
- **Canvas**: React Flow (`@xyflow/react`) with a custom `DeviceNode` and `LayerEdge`.
- **Query keys**: factory pattern in `src/api/*.ts` (e.g. `deviceKeys.lists()` / `.detail(id)`). Mutations use `setQueryData` on detail caches and invalidate list subtrees.

## Testing strategy

- Unit tests: per-service pytest suites against SQLite in-memory. Target 85%+ coverage.
- Integration tests: cross-service via httpx against a running stack. Self-seeding fixtures.
- Frontend tests: vitest + testing-library + MSW.
- E2E: Docker Selenium + Chrome for the existing suite, plus Playwright's Python sync API for new tests (issue #388), against the full running stack.
- Manual: docs/MANUAL_TESTING.md covers cases deliberately excluded from automation (real hardware, nondeterministic model output, wall-clock timing, browser matrix), with the exclusion reason per case.
- CI: three-job GitHub Action (backend, frontend, plus an advisory integration job), runs on push/PR to main.

## Deployment

Docker Compose for local dev and small prod deployments. Traefik is the only TLS-terminating component; backend services speak plain HTTP on the Docker network. PostgreSQL and NATS are single-node in the default compose; cluster them manually for HA.

Container health: `docker compose ps` shows per-service `(healthy)` state. Each backend has `GET /health` returning 200 when the DB is reachable and essential startup tasks completed.

## Known patterns and gotchas

- **Schema-per-service, no cross-schema foreign keys**: every service owns its schema; references to other services' resources are by UUID only and validated at service boundaries.
- **Generic SQLAlchemy types** (`Uuid`, `JSON`) are used in models so unit tests can run against SQLite; migrations add Postgres-specific variants (`JSONB`, GIN indexes) on top for prod.
- **Dialect branching** for advisory locks, JSONB containment, and enum-value migrations: `db.bind.dialect.name` gates Postgres-only code so SQLite test paths work.
- **Internal secret rotation** (`INTERNAL_API_TOKEN`): changing requires restarting every service; mid-flight service-to-service calls will 403 until all sides have the new value.

## Trade-offs accepted

HERD is built as 12 independent services rather than a modular monolith. This section documents the trade-offs accepted by that choice so future contributors and adopters understand the design rationale; engineering choices are stronger when the costs are explicit.

### Why microservices

- **Distinct scaling and security boundaries.** The `ai-orchestrator` is LLM-I/O-bound with cost considerations distinct from the rest of the system. The `execution` service runs admin-uploaded driver code that handles device credentials and is the most defensible candidate to keep on its own deployment boundary, so that if real OS-level isolation (namespaces, seccomp, a dedicated low-privilege user) is added later, it lands in one place. Splitting these out by default avoids needing to refactor under pressure later.
- **Independent deployment.** A future operator can deploy `notifications` or `user-profile` separately, scale them, restart them, or run them in different environments without touching the auth or inventory critical path.
- **Schema isolation as a coupling guard.** Each service owns its Postgres schema. There are no cross-schema foreign keys, only UUID references validated at service boundaries. This prevents a feature in service A from accidentally taking a hard dependency on the internal table layout of service B.
- **Demonstrating distributed-system patterns end-to-end.** JWT forwarding for inter-service RBAC, NATS JetStream for async events, durable consumers with service-scoped DLQ subjects, and per-service Alembic migration chains are all in active use.

### What that costs

- **Cross-service queries are HTTP, not SQL.** The reservations service's reporting `by_group` rollup is N+1 (one call per distinct user) where a modular monolith would JOIN a single `auth.users` table once. The cross-service cost is paid in latency and code complexity.
- **Eleven Alembic migration trees, twelve Dockerfiles, eleven service configurations.** The eleven DB-backed services each own a migration tree and an `app/config.py`; `config` ships a Dockerfile but has no database or Alembic tree, and `common` is a shared library with neither a Dockerfile nor a config. Cross-cutting changes (a new common field, a logging format change, a dependency bump) touch multiple services. Integration tests are the only end-to-end validation path for cross-service flows.
- **Operational surface.** A first-time deployer brings up a Postgres with 11 schemas, NATS with the `HERD_RESERVATIONS` and `HERD_HEALTH` streams plus four DLQ subjects, and 12 service containers behind Traefik. The `config` service mitigates this by handling first-start setup through the UI, but the surface itself is real.
- **For a single-deployer adoption, a modular monolith would have lower operational overhead.** That is a fair criticism and acknowledged here.

### When the microservice trade-off pays off

- The AI orchestrator and execution service win regardless. Their boundaries are not architecture-relative.
- Multi-team adoption (lab ops, automation, AI/ML) where separate teams own and scale separate services.
- Long-term feature growth where schema isolation prevents cross-feature coupling at the data layer from becoming load-bearing.

### What we would split out first if reconsolidating

If a single-deployer adoption ever made consolidation worthwhile, the two services to keep separate are:

1. `ai-orchestrator`: distinct dependency surface (Anthropic and OpenAI SDKs, pdfplumber, jsonschema), distinct rate-limit and cost profile, often the candidate for feature-gating in adopter environments. The provider abstraction lets the same service target either Anthropic SaaS or self-hosted inference without code changes.
2. `execution`: driver-code execution handles device credentials and is the natural home for stronger isolation if it is added, so it belongs on its own deployment boundary.

The other nine services are candidates to merge into a single FastAPI app with module boundaries preserved (separate routers, separate Pydantic models, separate test suites, single shared database with table prefixes). That refactor would not change the user-facing functionality; it is a deployment-shape change only, and it is not on the active roadmap.

## Deeper references

- [OPERATIONS.md](OPERATIONS.md) for running the stack, env vars, and ops-level concerns.
- [ROLES.md](ROLES.md) for the endpoint-level permission matrix.
- [AI_GENERATE.md](AI_GENERATE.md) and [AI_ASSISTANT.md](AI_ASSISTANT.md) for the two AI surfaces.
- [DRIVERS.md](DRIVERS.md) for the driver execution model.
- [TOPOLOGY_EDITOR.md](TOPOLOGY_EDITOR.md) for the canvas and connectivity validation.
- [BULK_IMPORT_EXPORT.md](BULK_IMPORT_EXPORT.md) for the CSV/JSON file schemas and cross-instance reference resolution.
- [design/0004-dynamic-resources.md](design/0004-dynamic-resources.md) for the hypervisor-backed dynamic-resources design (issue #32).
