# Decision: Connection-Driven Reconcile for Fork Wiring Deltas, Issue #345 P3b

Status: Accepted; P3b L1 delivered. Extends ADR 0006 Decision 7 (the P3b contract
sketch), resolves ADR 0006 open risk 1 (multi-hop path drift), and tracks issue
#345. Built on P3a, which shipped in ADR 0006 phases 1 to 5: the fork save, the
`fork_versions` rows, and the released/built set arithmetic are P3b's inputs. The
L1 half of this ADR shipped in phases 1 to 5 (PRs #364, #365, #367, #368, #371, and
the frontend-and-docs closeout): the `l1_connection_assignments` and
`reservation_wiring_state` tables, the `reservation.wiring_changed` outbox event
with the `fork_wiring_ledger` and the save-then-stage sweeper, the connection-driven
consumer with ordered apply and gap-reconcile, the per-connection wiring-status and
retry endpoints with the background auto-retry channel, and the reservation-detail
Wiring tab. Phase 2 (L2 VLAN and L3 route connection-driven reconcile, Decision 1)
remains. Two review-driven refinements landed during delivery without changing the
decision bodies: the fork save now groups per-hop wiring by a canvas `edge_key` so a
multi-hop edge round-trips as one logical connection (PR #367); and the L1 apply path
keys a connection's ACTIVE-versus-FAILED outcome on the driver-result payload the
sandbox returns rather than inferring success, so a driver-reported failure lands a
FAILED row instead of a spuriously-ACTIVE one. No code in this doc. Context verified
against the live HERD-public tree on 2026-07-17.

## Context

P3a made a fork save reconcile the intended wiring cabling-side, but hardware
provisioning stayed exactly as before: device-set-driven, never connection-driven.
A fork save changes `fork_connections` and appends a `fork_versions` row, and
nothing tells the execution service. P3b is the other half: make a save reconcile
physical device state connection-by-connection.

Two facts about execution bound the design, both unchanged by P3a:

- Provisioning is device-set-driven. The consumer branches on `update_ports` and
  acts on `added_device_ids`/`removed_device_ids`
  (`services/execution/app/services/nats_consumer.py:2387-2459`), or on the whole
  `device_ids` set for create/teardown (`nats_consumer.py:2460-2489`), and
  re-resolves L1 operations from the global `connections` graph per device
  (`_resolve_l1_switch_operations`, `nats_consumer.py:368-419`). No event carries a
  connection delta, and `fork_connections` is consumed by nothing.
- L1 applied state is not stored, it is inferred. There is no L1 assignment table;
  "did this pair's cross-connect succeed for this reservation" is answered by
  scanning `execution_runs` for a SUCCESS `connect_ports` row
  (`action_succeeded_for_reservation`,
  `services/execution/app/services/execution_service.py:103-135`). L2 and L3 do have
  assignment tables keyed to fabric and reservation-device, not connection
  (`services/execution/app/models/vlan_assignment.py:18-43`,
  `route_assignment.py:21-54`).

Driving hardware from a save is therefore a new provisioning model for execution,
not a wiring tweak: a new event, connection-addressable applied state, ordered
apply, and hardware-failure compensation that never rolls back the durable fork.

## Decision 1: L1 first; L2 and L3 stay device-set-driven

P3b phase 1 delivers connection-driven reconcile for L1 only: disconnect the
released physical pairs, then connect the built pairs, keyed by a new L1 assignment
table (Decision 4). L2 VLAN and L3 route provisioning stay device-set-driven until a
P3b phase 2, which this ADR bounds but does not fully design. (Superseded for L2:
ADR 0009 phase 4, issue #416, has since made L2 VLAN membership connection-driven on
the same wiring_changed apply; L3 remains device-set-driven pending its own ADR 0009
phase. The statements below describe the phase-1 boundary as it stood.)

Rationale: a fork's wiring is L1 by construction. `resolve_canvas_wiring`
(`services/cabling/app/services/fork_save_service.py:79-90`) resolves every canvas
edge to one L1 `WireSpec` per physical hop, so the released/built sets a save
produces are already L1 hops and nothing else. L2 membership and L3 route sets are
keyed to fabric and reservation-device
(`vlan_assignment.py:18-25`, `route_assignment.py:21-28`), so a pure re-wire that
keeps the device set does not change them; they correctly continue to follow the
device set. The reservation device-set PATCH and its `reservation.updated` event are
untouched, so they still drive device add/remove provisioning
(`nats_consumer.py:2387-2459`) and the issue #24 health-poll tier flips those events
carry (`nats_consumer.py:2365-2376`). Boundary to state plainly (open risk 5): a
fork edit that would change only L2/L3 intent is not applied to hardware in phase 1.

## Decision 2: Reservations stages the event; a ledger anchors it and a sweeper heals it

After cabling's save returns success, reservations stages a `reservation.wiring_changed`
outbox event. Reservations is already in the save-forwarding path
(`save_reservation_fork`, `services/reservations/app/routers/reservations.py:700-730`)
and already owns an outbox (`enqueue_event`, `herd_common/outbox.py:89-108`), so the
event is staged in the same transaction shape every other lifecycle event uses
(`reservation_service.py:1442-1455` for the cancel path). Cabling stays event-free.

The known at-least-once hole is the save-then-stage gap: cabling commits version N,
reservations crashes before staging. Close it with a ledger plus a sweeper, at the
reservations boundary:

1. Ledger. A `fork_wiring_ledger(reservation_id PK, last_staged_fork_version INT,
   updated_at)` row is upserted to the save's returned `version_number` in the SAME
   DB transaction as the `enqueue_event` outbox row, then one commit. This is what
   makes the anchor exact: the event exists if and only if the ledger advanced,
   because both are the same commit. The ledger alone cannot close the hole; its row
   is written in the same crash window as the event.
2. Sweeper. Extend the standing reconciler already in
   `services/reservations/app/tasks/expiration.py:431-495` (`_run_fork_archive_reconcile`,
   which already fetches cabling's per-reservation fork state each tick). For each
   ACTIVE reservation it compares cabling's latest `fork_version` against the
   ledger's `last_staged_fork_version`; a latest strictly greater than the ledger is
   a missed staging, so it stages a heal event carrying that version and advances the
   ledger, atomically, exactly as the save path does. The sweeper is the healer, the
   ledger is the anchor.

A heal event does not need to reconstruct a historical delta: it carries
`{reservation_id, fork_version}` with `released` and `built` ABSENT (null, not empty
lists), and the consumer routes any delta-less event to the full-reconcile path
(Decision 4) regardless of version contiguity. The distinction is load-bearing: a
single missed save produces a heal whose `fork_version` is exactly
`last_applied + 1`, and if that landed on the contiguous apply-the-carried-delta
path it would apply nothing and still advance the version marker, silently dropping
the missed save's changes. Absent delta always means reconcile against the full
intended set fetched from cabling. This is also why cabling need never replay old
diffs. The sweeper's comparison needs cabling to report each ACTIVE fork's latest
`fork_version`; the P3a internal list endpoint returns only reservation_ids, so
phase 2 extends its response (or the fork GET) with the version, an additive change.

## Decision 3: Event schema, subject, dedup

Subject `herd.reservations.wiring_changed`, three tokens, so the existing durable
pull consumer's `herd.reservations.*` filter binds it with no new stream or consumer
(the same reason `reservation.provision_requested` rides the stream today). Payload:

    {
      "event": "reservation.wiring_changed",
      "reservation_id": "<uuid>",
      "fork_version": <int>,
      "released": [ <wire>, ... ],
      "built":    [ <wire>, ... ],
      "event_id": "<uuid, stamped by enqueue_event>"
    }

Each `<wire>` uses cabling's canonical connection identity (ADR 0006 Decision 3,
`connection_identity`, `fork_save_service.py:178-195`): `device_a_id`, `port_a`,
`device_b_id`, `port_b`, `layer`, and the nullable `physical_connection_id` backing
the hop. Cabling normalizes endpoints to canonical order before it hands them to
reservations (it is the shape of `WireSpec`, `fork_save_service.py:40-49`), so the
consumer compares identities without re-normalizing. In phase 1 every `layer` is L1.

Dedup is the standard two-layer scheme (`herd_common/outbox.py:50-58`): the relay
sets `Nats-Msg-Id` to the outbox row id for broker-side dedup within the stream
window, and the consumer keys idempotency on the payload `event_id` via
`event_dedupe_key` (`outbox.py:260-272`) for a republish outside that window. This
event adds a second, semantic idempotency key: the per-reservation last-applied
`fork_version` (Decision 4), which also gives ordering.

## Decision 4: The L1 assignment table, ordered apply, and gap-reconcile

New table `l1_connection_assignments`, mirroring the vlan/route assignment idiom
(`vlan_assignment.py:18-43`): `id`, `reservation_id` (indexed), `switch_device_id`
(indexed), `port_a`, `port_b` (the cross-connected switch port pair),
`physical_connection_id` (nullable), `status` (ACTIVE/RELEASED/FAILED), `attempts`,
`last_error`, `created_at`, `released_at`. A partial-unique index on
`(switch_device_id, port_a, port_b) WHERE status = 'ACTIVE'` enforces one live
cross-connect per switch port pair, exactly as `uq_vlan_active_per_fabric` enforces
one VLAN per fabric; the index is declared on the model, not only the migration, so
`create_all` builds it for the SQLite unit DB (the vlan/route rationale). This table
IS the connection-addressable applied state that replaces the `execution_runs`
inference at `execution_service.py:103-135`; `execution_runs` remains the per-action
audit log.

Ordering and idempotency, per reservation, in a companion
`reservation_wiring_state(reservation_id PK, last_applied_fork_version INT, frozen
BOOL)` row:

1. `fork_version <= last_applied_fork_version`: stale or duplicate delivery; ack as a
   no-op. JetStream does not order redelivered or NAK'd messages, so the consumer,
   not the broker, enforces monotonicity.
2. `fork_version == last_applied_fork_version + 1` AND the event carries a delta:
   the contiguous normal case (each save appends exactly one `fork_versions` row and
   stages exactly one event, so versions are contiguous absent loss). Apply the
   carried `released`/`built` delta.
3. `fork_version > last_applied_fork_version + 1`, OR the event carries no delta (a
   sweeper heal, Decision 2, whatever its version): do not trust or expect a carried
   delta; fetch the fork's full intended L1 set from cabling and reconcile
   convergently, releasing the ACTIVE rows not in the desired set and building the
   desired rows not yet ACTIVE, then set `last_applied_fork_version = fork_version`.

Release-before-build in one pass, mirroring the cabling save
(`reconcile_connection_sets`, `fork_save_service.py:197-218`): a moved cable frees
its old port before the new claim, sidestepping the active-unique index.

Migration and backfill: a new Alembic revision under
`services/execution/migrations/versions/` creates the table and index. Reservations
already ACTIVE at upgrade have live cross-connects but no rows, so a one-time backfill
(a data step in the revision, or a startup reconcile) reconstructs ACTIVE rows from
the existing SUCCESS `connect_ports` `execution_runs`, the same inference
`action_succeeded_for_reservation` performs, and stamps their
`reservation_wiring_state.last_applied_fork_version` with a pre-P3b baseline read
from cabling. The first `wiring_changed` for such a reservation then lands on the gap
path and converges from that baseline. `action_succeeded_for_reservation` stays as
the transition fallback and the backfill source, and the failed-teardown path keeps
using it until phase 2 migrates it to the table.

## Decision 5: Path drift; apply the recorded hops verbatim

The save records the physical hops it resolved at save time, with each hop's backing
`physical_connection_id` (`resolve_canvas_wiring:79-90`). Execution applies those
recorded hops verbatim: it cross-connects exactly the switch ports the save chose and
does NOT re-run pathfinding at apply time. A recorded hop that is no longer resolvable
against inventory (the backing connection or switch port is gone) fails that one
connection (Decision 6); it does not re-route.

This chooses determinism and as-built fidelity over freshness, the explicit
resolution of ADR 0006 open risk 1. The as-built record is the intended wiring as the
human reviewed and saved it (ADR 0006 Decision 5); hardware must match that record,
not a path recomputed later that could differ from what was reviewed. Re-resolving at
apply time would let the physical path silently diverge and make failures
non-reproducible. Accepted failure mode: if the graph changed under the reservation
between save and apply, the affected connection lands FAILED with a "recorded hop
unresolvable" reason, and the human's recovery is to re-save (which re-resolves
against the current graph) then retry.

## Decision 6: Partial failure is per-connection; auto-retry, then manual

A hardware apply failure never rolls back the fork save: the intent is already
durable in cabling's `fork_connections` and `fork_versions`. Failure is compensated
per connection, mirroring the existing provisioning retry discipline rather than
inventing a new state machine:

1. In-message, each release/build connection is applied independently. A transient
   driver error on a connection is retried in-line with bounded backoff (the same
   discipline as `run_driver_action`). A connection still failing after the in-line
   cap is written FAILED with its `attempts` and `last_error`; the pass continues
   with the rest, and `last_applied_fork_version` still advances (the version was
   processed). A partial failure must not abort the surviving connections or roll
   back the save.
2. A background per-connection retry channel (a bounded sweep, batch-capped like the
   issue #24 health scheduler) reattempts FAILED rows with backoff up to a cap. This
   is the auto-retry.
3. After the cap the row is parked FAILED and surfaced to the UI with a manual retry
   action. This is the manual fallback.

Per-connection status lives in execution, the service that owns the L1 assignment
table and the hardware apply. Execution exposes internal
`GET /internal/reservations/{id}/wiring-status` (per-connection identity, status,
attempts, last_error) and `POST /internal/reservations/{id}/wiring/retry` (reattempt
FAILED rows). Reservations proxies both as owner-or-admin gated user endpoints,
`GET /{id}/wiring-status` and `POST /{id}/wiring/retry`, reusing the ownership gate
the save handler already applies (`routers/reservations.py:714-720`). The frontend
reservation detail surfaces a wiring-status panel with the retry button; the full
frontend design is a later phase, bounded here.

## Decision 7: Teardown no-op guard, and DLQ posture

A `wiring_changed` that arrives for a reservation that already ended (fork ARCHIVED,
teardown already ran) must be a safe no-op. Guard: when the consumer processes a
terminal event for a reservation (`reservation.cancelled`/`completed`/`failed`,
`nats_consumer.py:2460-2489`) it sets `reservation_wiring_state.frozen = true`. A
`wiring_changed` whose reservation is frozen is acked as a no-op before any driver
call. This is a local check with no cross-service fetch, and it is ordering-robust:
a `wiring_changed` reordered after a terminal teardown cannot re-establish a
cross-connect. Idempotency is belt-and-suspenders even without the flag, since a
build against an already-RELEASED reservation would re-claim ports the teardown
freed; the frozen flag makes it an explicit no-op rather than relying on that.

`wiring_changed` rides the same durable pull consumer as every reservation event, so
it inherits the issue #317 per-message `in_progress` ack heartbeat
(`nats_consumer.py:35-42`), keeping a slow multi-hop apply from tripping ack-timeout
redelivery, and the DLQ posture of `process_reservation_message`
(`nats_consumer.py:2516-2614`). The split that keeps DLQ semantics meaningful: a
transient UPSTREAM error (cabling or inventory 5xx while resolving the desired set)
raises `TransientUpstreamError` and NAKs the whole message for JetStream backoff; a
per-connection DRIVER failure does not NAK, it lands a FAILED row and acks, moving
retry to the Decision 6 channel. This keeps one bad cable from poison-looping a
message to the DLQ. A `wiring_changed` that does exhaust `max_deliver` is routed to
`herd.reservations.dlq.execution`, retained by the `HERD_DLQ` stream for inspection
and replay; on replay the last-applied guard and gap-reconcile (Decision 4) make it
convergent, not double-applied.

## Phased delivery

1. Execution: the `l1_connection_assignments` table, its migration and backfill, and
   `reservation_wiring_state`; the consumer writes the assignment rows for the
   existing L1 create/teardown paths (making applied state connection-addressable)
   with no behavior change yet. Unit tests for the table, the index race, and the
   backfill.
2. Reservations: stage `reservation.wiring_changed` on save with the
   `fork_wiring_ledger` upsert in the outbox transaction; extend the standing
   reconciler into the save-then-stage sweeper. Unit tests for the atomic
   ledger-plus-event and the sweeper heal.
3. Execution: consume `wiring_changed`, ordered apply with last-applied and
   gap-reconcile, release-before-build, verbatim-hop apply with per-connection FAILED
   rows and in-line bounded retry, and the frozen no-op guard. The heart; heaviest
   tests.
4. Execution and reservations: the per-connection wiring-status and retry endpoints,
   the reservations owner-gated proxies, and the background auto-retry channel.
5. Frontend and docs: the reservation-detail wiring-status panel and manual retry;
   docs sweep (ADR 0006 status pointer to this doc, EXTERNAL_API if the facade
   exposes wiring status, FEATURES/PLANNED_FEATURES status flip).

Each phase is independently mergeable, matching the #32 and P3a delivery pattern.
P3b phase 2 (L2 VLAN and L3 route connection-driven reconcile) is bounded by
Decision 1 and designed separately.

## Test plan

- Unit (execution): release-before-build set arithmetic against the assignment table
  including the move-a-wire case; ordering (stale skip, contiguous apply, gap-triggered
  full reconcile, and REQUIRED: a delta-less heal at exactly last_applied + 1 takes the
  full-reconcile path and applies the missed save's changes, never the empty carried
  delta); verbatim-hop apply and the unresolvable-hop FAILED row; the frozen
  no-op; partial failure landing a FAILED row without aborting siblings or advancing
  wrongly; the migration backfill reconstructing ACTIVE rows from `execution_runs`
  (mirror `test_vlan`/route-assignment tests, including the active-unique IntegrityError
  race pinned as the vlan tests pin it).
- Unit (reservations): the ledger upsert and the outbox enqueue commit in one
  transaction (event exists iff ledger advanced); the sweeper staging a heal when
  cabling's latest exceeds the ledger, and staging nothing when in sync.
- Functional: the reservations wiring-status and retry proxies (owner yes, other-user
  403, admin yes), and the save handler staging the event with the correct payload.
- Integration (live stack, mock drivers): reserve and activate with `mock_l1`, edit
  the fork, save a moved connection, assert `wiring_changed` is consumed,
  disconnect-then-connect driver calls fire, assignment rows flip, per-connection
  ACTIVE. Force a `connect_ports` failure via `HERD_mock_fail_actions` /
  `HERD_mock_raise_actions` and assert a FAILED row plus a successful manual retry
  after the knob clears; use `HERD_mock_sleep_ms` to exercise the ack heartbeat under
  a slow apply. Replay an event and assert no double connect (last-applied skip).
  Complete the reservation, replay a stale `wiring_changed`, assert no reconnect
  (frozen). Exhaust `max_deliver` on an upstream error and assert the event lands in
  `HERD_DLQ`.
- Stress/load: `wiring_changed` is user-save-driven (low rate), but a save storm
  across many ACTIVE reservations plus the sweeper is the new load path; a soak
  scenario saving forks on N reservations asserts the consumer keeps up,
  `last_applied_fork_version` stays monotone, and no assignment rows leak. The sweeper
  and the auto-retry channel are per-tick batch-capped like the health scheduler.
- e2e (selenium): open a running reservation, re-wire, save, watch per-connection
  status reach applied; force a seeded failure and use the manual retry button; verify
  the as-built view is read-only after completion.

## Open risks

1. Verbatim-hop apply (Decision 5) means a graph change between save and apply strands
   a connection FAILED, needing a re-save then retry; the re-resolve alternative was
   rejected for as-built fidelity. The recovery path must be documented for operators.
2. The gap-reconcile fetches the full intended set from cabling; a cabling outage
   during a heal defers convergence to the next tick. Acceptable, the fork intent is
   durable and the sweeper re-stages.
3. Two durability healers (the reservations ledger-plus-sweeper and the execution
   last-applied-plus-gap-reconcile) sit at the two boundaries; they must never
   double-apply. Idempotency rests on the last-applied guard and the active-unique
   index. A direct test of the crash window (staging missed, sweeper heals, execution
   converges once) is required, not optional.
4. The backfill stamps pre-P3b reservations with a baseline `fork_version`, so their
   first `wiring_changed` after upgrade triggers a full gap-reconcile (correct but
   heavier). A one-time cost, accepted.
5. L2 and L3 stay device-set-driven in phase 1 (Decision 1), so a fork edit changing
   only L2/L3 intent is not applied to hardware until P3b phase 2. Call it out in the
   release notes so operators know the phase-1 boundary.
