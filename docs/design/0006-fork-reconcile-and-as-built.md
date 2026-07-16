# Decision: Fork Reconcile-on-Save and As-Built Archive, Issue #25 P3

Status: Accepted; P3a delivered. Extends ADR 0001 (accepted), whose P1-P2 (fork
schema and fork-on-activation) shipped in migration `0007_reservation_forks.py`.
Resolves ADR 0001's open risks 1, 2, 4, and 5, and phases risk 3. P3a shipped in
phases 1 to 5 (PRs #349, #353, #359, #360, and this docs sweep): the cabling
internal fork surface, the reservations user-facing fork endpoints with teardown
archive and the standing reconciler, and the frontend fork-editing switch. P3b
(execution consumes fork wiring deltas, Decision 7) remains, tracked as issue
#345. No code in this doc. Context verified against the live HERD-public tree on
2026-07-09; ADR 0001's `reservation_service.py` line references predate the
file's growth and are stale, so this doc re-cites current lines.

## Context

The fork exists but is orphaned. The only fork endpoint is the internal,
idempotent `POST /internal/forks` (`services/cabling/app/routes/forks.py:29-57`),
called best-effort after activation
(`services/reservations/app/services/reservation_service.py:774-781`) and by the
expiry activation path (`tasks/expiration.py:196-199`). After creation nothing
reads, edits, or archives a fork: there is no GET, no save, no archive, and
`ForkStatus_ARCHIVED` (`services/cabling/app/models/fork.py:39`) is never
assigned. Reservations does not store a `fork_id`.

Meanwhile the shipped live-edit flow still mutates the shared parent: the
frontend's commit-to-reservation performs a `PUT /cabling/topologies/{id}` on
the parent topology and then a reservation PATCH for the device set
(`frontend/src/pages/TopologyEditorPage.tsx:440-469`). Every live edit appends a
real `TopologyVersion` to the master. This is the disease issue #25 exists to
cure, and P3a cures it.

Two facts about provisioning bound the design:

- Provisioning is device-set-driven, never connection-driven. The execution
  consumer acts on `added_device_ids`/`removed_device_ids`
  (`services/execution/app/services/nats_consumer.py:2345-2417`) and re-resolves
  L1 operations from the global physical `connections` graph per device
  (`nats_consumer.py:322-405`); `fork_connections` is consumed by nothing.
- Provisioned state is keyed to reservation, device, and fabric, not to
  connections: `route_assignments (reservation_id, device_id)`
  (`services/execution/app/models/route_assignment.py:21-28`),
  `vlan_assignments (fabric_id, vlan_id)` (`vlan_assignment.py:18-25`), and L1
  has no assignment table at all.

Making a fork save drive hardware is therefore not a wiring change; it is a new
provisioning model for the execution service. That observation forces the
phasing decision below.

## Decision 1: Two phases; P3a is cabling-complete, P3b is execution-aware

P3a (this ADR's implementable scope) ships the full cabling-side reconcile and
the frontend switch, with hardware provisioning left exactly as today
(device-set-driven via the reservation PATCH):

- Fork read, loose edit, save-reconcile, and archive endpoints (Decision 2).
- Release-before-build set arithmetic over `fork_connections` with a
  transactional save and a new `fork_versions` row per save (Decision 3).
- Cross-reservation port-claim enforcement at save (Decision 4).
- Teardown archive producing the immutable as-built record (Decision 5).
- Frontend live-edit mode edits the fork and stops mutating the parent
  (Decision 6).

P3b (follow-up issue, contract sketched in Decision 7, designed and shipped
separately) makes execution consume fork wiring deltas so a save reconciles
physical device state connection-by-connection.

Rationale: the user-visible wins (master stays clean, per-reservation editing
with history, as-built records) require none of the execution rework, and the
execution rework (new event, connection-keyed applied state, an L1 assignment
table, hardware-failure compensation) is a full design of its own. Interplay
with issue #24 is also preserved for free: device-set changes remain
PATCH-driven in P3a, so `reservation.updated` events and the health-poll tier
transitions they drive (`nats_consumer.py:2321-2337`) are untouched.

## Decision 2: API surface and routing

All fork endpoints stay cabling-internal (`X-Internal-Token`), with reservations
as the user-facing authority that enforces ownership before forwarding, exactly
as ADR 0001 Decision 2 prescribed. The booking owner does not necessarily own
the parent topology, so user JWTs never hit cabling's fork routes directly.

New cabling internal endpoints (extending `routes/forks.py`):

1. `GET /internal/forks/{reservation_id}`: fork metadata, current canvas,
   current `fork_connections`, and version list. 404 when no fork exists.
2. `PUT /internal/forks/{reservation_id}/canvas`: loose edit. Stores
   `canvas_data` on the fork row only; validates route shape (reuse
   `_run_topology_validation`, `services/cabling/app/routes/topologies.py:200-268`)
   but runs no reconcile and appends no version. Drafts are cheap.
3. `POST /internal/forks/{reservation_id}/save`: the reconcile (Decision 3).
   Returns `{fork_id, version_number, released: [...], built: [...],
   unchanged_count}`.
4. `POST /internal/forks/{reservation_id}/archive`: teardown freeze
   (Decision 5). Idempotent.

New reservations user-facing endpoints, owner-or-admin gated like the PATCH
(`routers/reservations.py:429-437`), each forwarding to the matching cabling
internal endpoint after the ownership check: `GET /{id}/fork`,
`PUT /{id}/fork/canvas`, `POST /{id}/fork/save`. Archive has no user-facing
route; it is invoked only by the lifecycle transitions (Decision 5). A fork that
does not exist yet on first edit is created lazily through the existing
idempotent `POST /internal/forks` (ADR 0001 Decision 3 Case A), which also
covers the activation-time fork-create failure fallback (ADR 0001 open risk 4:
the lazy path IS the sweeper).

## Decision 3: Reconcile semantics

Identity and arithmetic are as ADR 0001 committed
(`docs/design/0001-editable-reservation-topologies.md:233-239`): connection
identity is `(device_a_id, port_a, device_b_id, port_b, layer)` with endpoints
normalized to a canonical order before comparison, `to_release = old MINUS new`,
`to_build = new MINUS old`, `unchanged` untouched. The save:

1. Parses the submitted canvas's committed (non-proposal) edges to the new
   intended set, resolving multi-hop physical paths exactly like fork creation
   does (`fork_service.py:93-194`), so saved rows carry
   `physical_connection_id` backing where resolvable.
2. In one transaction: deletes `to_release` rows first, inserts `to_build` rows
   second (release-before-build, which also sidesteps unique-constraint
   collisions when a wire moves between ports), updates `fork.canvas_data`, and
   appends one `fork_versions` row.
3. On any failure the transaction rolls back wholesale: the fork, its
   connections, and its version history are unchanged, and the caller gets a
   409 or 422 with a structured detail. Never a half-applied save.

Version allocation generalizes the existing retry discipline rather than
duplicating it: `commit_with_new_version`
(`services/cabling/app/services/version_service.py:33-85`) is refactored to
parametrize the version model and scope column so both `TopologyVersion`
(topology_id) and `ForkVersion` (fork_id) allocate `max+1` under their unique
constraints with the same rollback-recompute-retry loop. Fork creation's direct
v1 insert (`fork_service.py:236-242`) migrates to the same helper. The existing
`diff_canvas` (`version_diff.py:30-48`) is exposed for forks via the GET
endpoint's version list so the frontend History/diff UI can point at fork
versions unchanged.

Concurrent saves of the same fork are serialized by the database: the second
save's version allocation retries onto `max+2` and its set arithmetic recomputes
against the first save's committed rows inside the retry loop, mirroring the
VLAN TOCTOU discipline (`services/execution/app/services/vlan_service.py:124-131`).

## Decision 4: Port claims are enforced at save, 409 on conflict

A physical port wired by one ACTIVE reservation must not be claimable by
another. Inside the save transaction, after computing `to_build`, cabling
queries `fork_connections` rows of OTHER forks whose parent `reservation_fork`
has `status = ACTIVE` for any overlapping `(device_id, port)` endpoint pair in
`to_build`. Any hit fails the save with 409 and a structured detail listing the
conflicting `reservation_id`(s) and ports; the fork is unchanged.

Notes and boundaries:

- The check runs inside the save transaction, so two simultaneous saves cannot
  both pass it against each other's uncommitted rows on Postgres read-committed
  only if one commits first; the loser's version-allocation retry (Decision 3)
  re-runs the check against the winner's now-committed rows. The residual
  window (both commit before either retries) is closed by the retry loop
  re-executing the conflict query, not by a cross-fork unique constraint, which
  the schema cannot express while `status` lives on the parent fork row.
- ARCHIVED forks never block: as-built records are history, not claims.
- The parent topology's own wiring is not a claim either; only other ACTIVE
  forks contend. Physical-graph capacity modeling (ADR 0001 open risk 2) stays
  out of scope; this decision enforces exclusivity between reservations, which
  is the collision a lab tool must catch.

## Decision 5: Archive and the as-built record

The as-built record is the intended wiring as last reconciled, cabling-side
only. `POST /internal/forks/{reservation_id}/archive`:

1. Flips `reservation_fork.status` to `ARCHIVED` (first actual use of
   `ForkStatus_ARCHIVED`, `fork.py:39`).
2. Retains `fork_connections` and all `fork_versions` rows untouched and
   read-only: every mutating fork endpoint (canvas, save) refuses ARCHIVED
   forks with 409.
3. Discards nothing physically but semantically drops unsaved drafts: a
   `canvas_data` edited via loose PUT but never saved is NOT the as-built; the
   as-built truth is the last saved version plus the reconciled
   `fork_connections` set. Archive therefore appends no new version. What was
   never reconciled was never built, and the record must not claim otherwise.
4. Is idempotent: archiving an ARCHIVED fork returns 200 with the existing
   state; archiving a nonexistent fork returns 204 (nothing to freeze).

Reservations calls archive best-effort with `retry_with_backoff` from the same
three teardown paths that release devices: `release_reservation`
(`reservation_service.py:1302-1332`), `cancel_reservation` (`:1207-1246`), and
the expiry sweep's auto-complete (`tasks/expiration.py:263-277`), mirroring the
fork-create failure posture (log-and-continue; an archive failure must never
strand a teardown). A FAILED transition also archives: the fork records what
was intended even when provisioning failed. Execution-side provisioning
outcomes (what was actually configured) stay out of the as-built record; they
remain queryable in `execution_runs` and the assignment tables, and an enriched
as-built is explicitly future work if audit needs it.

## Decision 6: Frontend edits the fork; the master stays clean

Live-edit mode (`?reservationId=`, `TopologyEditorPage.tsx:83-89`) is repointed
at the fork through the new reservations endpoints: load from `GET /{id}/fork`
(lazy-creating on first edit), draft-save via `PUT /{id}/fork/canvas`, commit
via `POST /{id}/fork/save`, surfacing the `{released, built}` result and the
409 port-conflict detail. The parent-topology `PUT` is removed from the
commit-to-reservation path (`TopologyEditorPage.tsx:440-469`); the device-set
PATCH remains, and with it provisioning and the reservation lock's owner
carve-out semantics. This is a deliberate behavior change and the acceptance
test for the epic's core promise: after P3a, a reservation owner's edits create
fork versions and the parent's `TopologyVersion` history stays byte-for-byte
unchanged. The reservation detail modal navigates to the fork view instead of
`topology_id` (`ReservationDetailModal.tsx:91`), satisfying ADR 0001 open risk
5 with the dedicated GET.

## Decision 7: P3b contract sketch (execution-aware reconcile, follow-up)

Deferred to its own issue and design pass, bounded here so P3a builds toward
it:

- Trigger: after a successful cabling save, reservations (already in the
  forwarding path, already owning an outbox) stages a
  `reservation.wiring_changed` event on the existing `HERD_RESERVATIONS`
  stream carrying `{reservation_id, fork_version, released: [...],
  built: [...]}`. Cabling stays event-free. The save-then-stage gap (cabling
  committed, reservations crashed before staging) is the known at-least-once
  hole; P3b's design must close it (candidates: reservations-side ledger keyed
  by fork_version, or a reconcile sweeper comparing latest fork_version against
  last-provisioned version).
- Execution: consumes the event, performs release-before-build against
  hardware (L1 disconnect released pairs, then connect built pairs; L2/L3
  adjusted analogously), keyed by a new L1 assignment table so applied state
  becomes connection-addressable (today it is inferred from `execution_runs`,
  `execution_service.py:103-137`).
- Failure posture: partial hardware failure must not roll back the fork save
  (the intent is already durable); it lands in a per-connection status the UI
  surfaces, with retry, mirroring the provisioning state machine rather than
  inventing a new one.

## Phased delivery (P3a)

1. Cabling: fork GET + loose canvas PUT; `version_service` generalization; unit
   tests for both.
2. Cabling: save-reconcile (set arithmetic, release-before-build transaction,
   port-claim 409, version append) plus archive. The heart; heaviest tests.
3. Reservations: user-facing fork endpoints (ownership forwarding), archive
   wiring into the three teardown paths, lazy-create on first edit.
4. Frontend: fork editing flow, history/diff against fork versions, save-result
   and conflict UX, detail-modal navigation.
5. Docs sweep: USER_GUIDE, TOPOLOGY_EDITOR, EXTERNAL_API if the integration
   facade exposes fork reads, FEATURES/PLANNED_FEATURES status flip, ADR 0001
   status line pointed at this doc.

Each phase is independently mergeable, matching the #32 delivery pattern.

## Test plan

- Unit (cabling): set-arithmetic pure functions including the move-a-wire case
  (release and build touch the same port pair across layers); transactional
  rollback on injected failure between release and build (no half-apply, no
  orphan version); port-conflict 409 detail shape; archive idempotency and the
  ARCHIVED write-refusal; generalized `commit_with_new_version` against both
  version models (mirroring `test_topology_versions.py`, including the
  concurrent-save winner/loser interleaving pinned the same way
  `test_forks.py` pins create_fork's IntegrityError race).
- Unit (reservations): ownership forwarding (owner yes, other-user 403, admin
  yes); lazy fork creation on first edit; archive called on all three teardown
  paths with retry-and-continue on failure.
- Integration (live stack): reserve and activate, edit loosely, save a moved
  connection (assert released-then-built rows, version 2, parent
  `TopologyVersion` history unchanged); conflicting save from a second
  reservation (409 names the first reservation); complete the reservation
  (fork ARCHIVED, mutations refused, versions readable); FAILED reservation
  still archives.
- e2e (selenium): open a running reservation, re-wire, save, read the
  released/built toast; verify parent topology history unchanged in the UI;
  complete and verify the as-built view is read-only.
- Load: no new load path in P3a (saves are user-driven, low rate); revisit at
  P3b when events fan out.

## Open risks

1. Multi-hop path re-resolution at save time can produce a different physical
   path than activation resolved (graph changed underneath). The save records
   what it resolves; P3b must treat path drift explicitly when it starts
   driving hardware.
2. The port-claim check's contention story relies on the version-allocation
   retry loop re-running the conflict query; a direct test of two concurrent
   conflicting saves is required, not optional.
3. `version_service` generalization touches the shipped topology-version path;
   its existing tests must pass unchanged, and the refactor lands in phase 1
   where the blast radius is smallest.
4. Frontend removal of the parent PUT changes live-edit behavior users may
   have internalized; the release notes and USER_GUIDE must call it out.
