# Decision: Editable Reservation Topologies (Fork-on-Reserve), Issue #25

Status: Accepted; P1-P2 shipped (fork models, tables, and creation in the
cabling service). P3 reconcile-on-save is still open under issue #25. No code in
this doc.
Context verified against the live HERD-public tree on 2026-06-09.

## Context

HERD is 12 FastAPI microservices under `services/`, with no cross-schema FKs or
JOINs: services reference each other by UUID over HTTP (see `CLAUDE.md`,
`docs/ARCHITECTURE.md`). Issue #25 is the fork-on-reserve model: an immutable
parent topology, an editable per-reservation child fork created at activation, a
release-before-build reconcile on save, and a retained immutable as-built record
on teardown.

This is distinct from the already-shipped lock feature, which must not be
redesigned:

- `update_reservation` re-validates topology connectivity when the device set
  changes (`services/reservations/app/services/reservation_service.py:631-640`).
- The reservation-scoped edit lock with owner carve-out lives in
  `services/cabling/app/routes/topologies.py:100-131`, using
  `find_blocking_reservations`
  (`services/cabling/app/services/reservation_guard.py:12-56`).
- Frontend live-edit mode reads `?reservationId=`
  (`frontend/src/pages/TopologyEditorPage.tsx:83-89`).

Prior design decisions are accepted as the frame: parent immutable; fork created
at activation; ownership is the concurrency lock; edit-loosely and
reconcile-on-save with set-arithmetic release-before-build; transactional save;
teardown retains the fork as an immutable as-built record.

Three open design questions block #25. They are resolved below.

### Key facts verified against live code

- `Connection` is global: `device_a_id:port_a` to `device_b_id:port_b` plus
  `connection_type`, `notes/created_by/created_at`. There is no `topology_id`
  and no `reservation_id` (`services/cabling/app/models/connection.py:13-29`).
  Connections today represent the physical cabling graph that pathfinding walks
  (`_run_topology_validation` builds the adjacency graph from connections at
  `services/cabling/app/routes/topologies.py:200-268`).
- `Topology.canvas_data` is JSON; `TopologyVersion` is an immutable snapshot
  with `version_number`, `canvas_data`, `restored_from_id`, under unique
  constraint `uq_topology_versions_topology_version` on
  `(topology_id, version_number)` (`services/cabling/app/models/topology.py:23-75`).
- `Reservation.topology_id` is nullable
  (`services/reservations/app/models/reservation.py:54-56`). Devices live in
  `reservation_devices` with replace-the-set semantics via association proxy
  (`reservation.py:84-127`).
- Activation: a reservation is created `PENDING_PROVISION` for exclusive
  devices, then flipped to `ACTIVE` after inventory RESERVED succeeds
  (`reservation_service.py:339-434`). Non-exclusive-only reservations are born
  `ACTIVE` (`reservation_service.py:343-345`).
- Teardown: `cancel_reservation` to `CANCELLED`
  (`reservation_service.py:738-819`); `release_reservation` to `COMPLETED`
  (`reservation_service.py:822-837`).
- Reuse targets confirmed: `commit_with_new_version` retry loop on
  `IntegrityError` (`services/cabling/app/services/version_service.py:33-85`);
  VLAN-TOCTOU recompute-and-retry
  (`services/execution/app/services/vlan_service.py:124-131`).

## Decision 1: Connection scoping (the core schema problem)

Problem: today every `Connection` row is a physical fact in the global cabling
graph (`connection.py:18-22`). A fork needs to hold the logical wiring of one
lease, which ports are wired for that reservation and how (L1 vs L2), without
polluting the physical graph or other reservations' forks.

### Options

- Option A: add `reservation_id` directly to `connections` (nullable; `NULL` =
  physical, non-null = fork-owned). Smallest delta, but conflates physical
  inventory with lease wiring in one table, and every existing physical-graph
  query (`list_connections`, `build_adjacency_graph`) must now filter
  `reservation_id IS NULL` or a fork's wires leak into everyone's
  pathfinding/validation. High blast radius on shipped read paths.
- Option B: a parent `reservation_fork` row plus a separate `fork_connections`
  table. Physical `connections` stays exactly as is (zero change to the global
  graph). Mirrors how the codebase already separates the live `Topology` row
  from the immutable `TopologyVersion` snapshot.
- Option C: store fork wiring only inside the fork's `canvas_data` JSON. Zero
  new connection table, but the set-arithmetic reconcile needs to diff
  connections as relational rows with a stable identity; set arithmetic inside
  JSON is error-prone and unindexed. Rejected for the reconcile path (JSON
  canvas is still kept for the drawing).

### Recommendation: Option B, a `reservation_fork` table plus a `fork_connections` table

Keep the physical `connections` table byte-for-byte so none of the shipped
pathfinding/validation/list code changes meaning. Introduce:

1. `reservation_fork`: the fork's identity and lifecycle, stored cabling-side
   (see Decision 2), keyed by `reservation_id` (bare UUID, no FK, exactly like
   `reservation_devices.device_id`, `reservation.py:102-124`). Holds
   `parent_topology_id`, `parent_version_id` (the parent `TopologyVersion` it
   was forked from, see Decision 3), `canvas_data` JSON (the editable drawing),
   `status` (`ACTIVE` or `ARCHIVED`), timestamps.
2. `fork_connections`: the lease's real wiring, the unit of set arithmetic.
   Columns: `id`, `fork_id` (FK to `reservation_fork.id`, `ondelete CASCADE`),
   `device_a_id`, `port_a`, `device_b_id`, `port_b`, `layer` (L1/L2, the "how"
   the global `Connection` lacks), `physical_connection_id` (nullable bare UUID
   back-reference to the global `connections` row a lease wire is realized over
   for L1; null for pure-L2), `created_by/created_at`. Unique constraint on
   `(fork_id, device_a_id, port_a, device_b_id, port_b, layer)`: this makes the
   save reconcile a clean set operation and gives the same `IntegrityError`-on-
   collision arbiter the VLAN and version code already lean on.
3. `fork_versions`: modeled exactly on `TopologyVersion` (`topology.py:45-75`):
   `fork_id`, `version_number`, `canvas_data`, `restored_from_id`, unique
   `(fork_id, version_number)`. Each successful save appends one, allocated
   through the same `commit_with_new_version` retry discipline (see Reuse).

### Migration shape

Cabling-side Alembic revision under `services/cabling/migrations/versions/`:
create `reservation_fork` (index on `reservation_id`), `fork_connections` (the
unique constraint, index on `fork_id`, indexes on `device_a_id`/`device_b_id` to
match the physical table's per-device indexing at `connection.py:18,20`), and
`fork_versions` (unique `(fork_id, version_number)`). No change to
`connections`. Reversibility is trivial: `downgrade` drops the three new tables;
the physical graph is untouched, so there is nothing to backfill or un-backfill.

Tradeoff accepted: a second connection-shaped table, in exchange for zero risk to
the shipped physical-graph read paths and a relationally-diffable fork-wiring
table.

## Decision 2: Where the fork lives and the cross-service contract

Problem: parent topology and the physical connection graph are owned by cabling;
the reservation is owned by reservations; no cross-schema join.

### Recommendation: fork lives cabling-side, keyed by `reservation_id`

Cabling already owns every primitive the fork needs (canvas, versions,
pathfinding, connection rows). Storing the fork reservations-side would force
reservations to re-implement all of it. Reservations stays the lifecycle
authority and calls cabling at the four lifecycle moments. All calls are
internal, authed with `X-Internal-Token` exactly like
`validate_topology_internal` (`topologies.py:271-289`) and the reservations
internal lookups used by `find_blocking_reservations`
(`reservation_guard.py:30-31`), because the booking/owner user does not
necessarily own the parent topology.

### Inter-service contract (new internal endpoints on cabling, called by reservations)

All under an internal prefix, `X-Internal-Token` guarded:

1. Create-fork-on-activation: `POST /internal/forks`. Body
   `{ reservation_id, parent_topology_id | null, parent_version_id | null }`.
   Cabling deep-copies the parent's current `canvas_data` (mirroring
   `clone_topology`, `topologies.py:163-197`) and snapshots the parent's
   relevant physical connections into `fork_connections`, creates
   `reservation_fork` plus `fork_versions` v1. Idempotent on `reservation_id`
   (re-POST returns the existing fork) so a retried activation cannot create two
   forks. Returns `{ fork_id, version_number }`.
2. Edit (loose): `PUT /internal/forks/{reservation_id}/canvas`. Body
   `{ canvas_data }`. Validates routes only (reuse `_run_topology_validation`,
   `topologies.py:200-268`); does not run port-availability/reconcile. No
   `fork_versions` row yet (drafts are cheap). Ownership is enforced by
   reservations before it forwards (owner or admin), consistent with the shipped
   owner carve-out (`topologies.py:111-113`).
3. Save-reconcile: `POST /internal/forks/{reservation_id}/save`. Body
   `{ canvas_data }`. Cabling computes the connection delta from the new canvas
   against the fork's current `fork_connections`, applies release-before-build
   set arithmetic transactionally (see Reuse), and on success appends one
   `fork_versions` row via `commit_with_new_version`. On a genuine build
   conflict (a port truly consumed) it rolls back wholesale and returns `409`
   with a "port already consumed" detail, mirroring the lock-conflict 409 shape
   at `topologies.py:115-131`. Returns
   `{ fork_id, version_number, released: [...], built: [...] }`.
4. Teardown: `POST /internal/forks/{reservation_id}/archive`. Releases the
   fork's held ports/wiring back to the pool but retains `reservation_fork`
   (status `ARCHIVED`) plus all `fork_versions` read-only as the as-built
   record. Idempotent. Called by both `cancel_reservation` and
   `release_reservation`.

Call sequence (happy path): `create_reservation` provisions devices and flips to
ACTIVE (`reservation_service.py:432`), reservations calls `POST /internal/forks`,
user edits via `PUT .../canvas` (loose), user saves via `POST .../save`
(reconcile, new version), on complete/cancel `release_reservation`/
`cancel_reservation` calls `POST .../archive`.

Failure posture: fork creation failing at activation must not strand a
successfully-provisioned reservation. Activation proceeds even if fork-create
fails transiently; reconcile with a bounded retry (`retry_with_backoff`, already
used at `reservation_service.py:386-394`) and, on exhaustion, log structured and
leave the reservation ACTIVE with `fork_id = null`. The reservation is still
usable; the editable bench is simply unavailable until a sweeper or
lazy-create-on-first-edit creates it. This matches the codebase's fail-open
discipline (`reservation_guard.py:42-45`, `reservation_service.py:792-803`).

## Decision 3: Fork-timing edge cases

### Case A: reservation with no topology (`topology_id IS NULL`)

Recommendation: create an empty fork lazily, on first edit, not at activation.
At activation, if `topology_id IS NULL`, skip `POST /internal/forks`. The first
`PUT .../canvas` (or an explicit "start wiring this reservation" action)
triggers fork creation with `parent_topology_id = null`. This avoids
manufacturing empty forks for the many no-topology reservations that will never
be wired, while still letting a user who wants to build wiring do so. The
save/reconcile/teardown paths are identical once a fork exists.

### Case B: parent topology edited between reservation create and activation

Recommendation: the fork pins the parent `TopologyVersion` at activation, not at
create. `create_reservation` does not pin a version; at activation, reservations
sends `parent_version_id = <parent's current max version at activation>` (or
cabling resolves "current" itself) and cabling forks from that snapshot, storing
it in `reservation_fork.parent_version_id`. Rationale: provisioning already
happens at activation against current inventory, so wiring should match the same
instant; `TopologyVersion` gives a stable, immutable handle to fork from; and it
sidesteps a stale-snapshot race with no extra locking. This is consistent with
the shipped lock: an active reservation already blocks parent-version restore
(`versions.py:131-147`) and blocks non-owner parent wiring edits
(`topologies.py:108-131`), so the parent cannot be yanked out from under the fork
after activation. The only window is create to activation, which this pin closes.

## Reuse of existing machinery

- Fork snapshots reuse the `TopologyVersion` pattern (`topology.py:45-75`); the
  frontend revision/History UI and `diff_canvas` (`versions.py:86-98`) can point
  at fork versions with minimal change.
- Version allocation reuses `commit_with_new_version`
  (`version_service.py:58-85`): every save appends a fork version numbered
  `max+1` under a unique constraint with the existing
  rollback-recompute-retry loop.
- Build-conflict handling reuses the VLAN-TOCTOU discipline
  (`vlan_service.py:124-131`): catch `IntegrityError`, roll back, recompute the
  in-use set, retry under a bounded cap. The `fork_connections` unique
  constraint makes the database the arbiter.
- Release-before-build set arithmetic. With `old = current fork_connections` and
  `new = connections parsed from the saved canvas` (identity =
  `(device_a, port_a, device_b, port_b, layer)`): `unchanged = old INTERSECT new`
  left untouched; `to_release = old MINUS new` deleted first, returning capacity;
  `to_build = new MINUS old` built after releases. The whole release+build runs
  in one transaction: either all commits or the fork is unchanged and the caller
  gets a 409, never a half-applied state.
- Route validation reuses `_run_topology_validation` (`topologies.py:200-268`).
- Internal-token contract reuses `validate_topology_internal`
  (`topologies.py:271-289`) and `reservation_guard.py:30-31`.
- Lifecycle hooks land in existing functions: fork-create after
  `reservation.status = ACTIVE` (`reservation_service.py:432`); archive into
  `cancel_reservation` (`reservation_service.py:750`) and `release_reservation`
  (`reservation_service.py:834`), reusing `retry_with_backoff`.

## Phased delivery

1. Schema. Cabling Alembic revision: `reservation_fork`, `fork_connections`
   (with the unique constraint), `fork_versions`. No change to `connections`.
   Ship and reverse on ephemeral Postgres. Pure additive, independently
   mergeable.
2. Fork-on-activation. `POST /internal/forks` (idempotent, deep-copy canvas plus
   snapshot relevant physical connections plus v1). Wire into `create_reservation`
   post-ACTIVE with `retry_with_backoff` plus log-and-continue on exhaustion.
   Implements Decision 3 pin and Case A skip.
3. Edit / save-reconcile API. `PUT .../canvas` (loose validate) and
   `POST .../save` (release-before-build, transactional, `commit_with_new_version`
   for the new `fork_versions` row, 409 on build conflict). The heart of the
   feature.
4. Teardown / as-built. `POST .../archive` (release ports, retain fork plus
   versions read-only as ARCHIVED). Wire into `cancel_reservation` and
   `release_reservation`.
5. Frontend. Extend the live-edit entry
   (`frontend/src/pages/TopologyEditorPage.tsx:83-89`) so a running reservation
   opens its fork; point the History/revision and diff UI at `fork_versions`.
   Surface the released/built save result and the 409 message.

## Test plan

- Unit (delta arithmetic plus rollback), cabling: pure-function tests for the
  three set operations including the move-port-on-same-device case;
  transactional-rollback test (a build that hits the unique constraint rolls back
  wholesale, no half-apply, no orphaned version); version-number allocation test
  mirroring `services/cabling/tests/test_topology_versions.py` against
  `fork_versions`.
- Migration reversibility: apply plus downgrade on ephemeral Postgres; assert
  `connections` is byte-for-byte unchanged and the three new tables are created
  then dropped cleanly.
- Integration (fork lifecycle across cabling plus reservations): reserve,
  activate (assert fork plus v1, `parent_version_id` pinned at activation even
  after a parent edit between create and activate), loose edit, save with a moved
  connection (unchanged ports untouched, released-then-built, new version), a
  save that must fail (port consumed) rolling back cleanly with 409,
  complete/cancel (fork retained ARCHIVED read-only, ports released, parent
  byte-for-byte unchanged). Plus the no-topology reservation: no fork at
  activation, fork created lazily on first edit.
- E2E: open a running reservation, re-cable an A to B link from L1 to L2, save;
  assert parent unchanged and a new fork revision; trigger a port-conflict save
  and assert the message; complete and assert the fork is read-only as the
  as-built record.

## Open risks

1. Physical-connection snapshot scope at fork time needs a precise rule for which
   physical `connections` rows seed `fork_connections`. Recommend seeding from
   the parent canvas edges resolved to physical paths via `find_all_shortest_paths`,
   not the whole global graph. Confirm before Phase 2.
2. Whether the fork consumes inventory port capacity or is advisory. If ports are
   not yet a tracked, contended resource, the build-conflict path is theoretical
   and the unique constraint is the only real arbiter. Confirm whether physical
   port capacity is modeled.
3. L2 wiring without a backing physical L1 path (`layer = L2`,
   `physical_connection_id = null`) is allowed by design, but execution
   provisioning (`vlan_service.py`) must accept a fork-sourced L2 intent.
   Cross-check before Phase 3.
4. Activation-time fork-create failure leaving `fork_id = null` needs a sweeper or
   lazy-create-on-first-edit fallback. Case A's lazy-create provides a natural
   fallback; confirm it covers the failed-create-with-topology case.
5. Frontend reservation lookup currently scans `useReservations()`
   (`TopologyEditorPage.tsx:84-88`); a fork-by-reservation fetch may want a
   dedicated endpoint. Minor, Phase 5.
