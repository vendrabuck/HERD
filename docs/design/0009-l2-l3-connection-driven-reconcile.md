# Decision: L2/L3 Connection-Driven Reconcile, P3b Phase 2

Status: Accepted 2026-07-19; delivery tracked by issue #416. Executes the
phase ADR 0007 Decision 1 bounded but deliberately did not design ("L2 VLAN and L3 route provisioning stay
device-set-driven until a P3b phase 2"). Absorbs issues #393 (L2/L3 driver
results transport-gated), #369 (retry direction missing on assignment rows),
and #366 (legacy L1 positional mispairing), and discharges ADR 0007 open
risk 5 (a fork edit changing only L2/L3 intent touches no hardware) and its
deferred migration of the failed-teardown path off
`action_succeeded_for_reservation`. No code in this doc. Context verified
against the live HERD-public tree at dd8e21de, 2026-07-19.

## Context

Phase 1 (ADR 0007, delivered) made L1 wiring connection-driven: a fork save
stages `reservation.wiring_changed`, and execution reconciles switch
cross-connects against the `l1_connection_assignments` ledger with ordered
apply, per-connection status, and retry. L2 and L3 stayed device-set-driven,
and the boundary shows:

- The fork already stores layered intent. `ForkConnection.layer` is part of
  the connection identity (`services/cabling/app/models/fork.py:75-105`), and
  cabling's intended-set endpoint serves all layers; the L1-only view is an
  execution-side filter (`_fetch_fork_intended_wires`,
  `services/execution/app/services/nats_consumer.py:373`). The intent model
  for phase 2 exists; nothing consumes it.
- ADR 0007's Decision 1 rationale ("a pure re-wire that keeps the device set
  does not change L2/L3 intent") holds for L3 but only half-holds for L2. L3
  provisioning is adjacency-and-config: routes come from the switch's latest
  config version, pinned per reservation (`route_service.py`, issue #20), and
  adjacency follows devices. L2 port membership follows the PORTS the wiring
  traverses (`_resolve_l2_switch_operations`, `nats_consumer.py:514-564`,
  reads the global connections graph), so a fork re-wire that moves a DUT to
  a different switch port leaves the old port in the VLAN and the new one out
  of it. Open risk 5 is not hypothetical for L2.
- Driver results are transport-gated at five L2/L3 sites
  (`nats_consumer.py:834, 883, 933, 977, 1352`): a driver that RETURNS
  success false is recorded SUCCESS (issue #393), the exact blind spot the
  L1 path closed in phase 1 and PR #387 closed for `run_driver_action`.
- The L2 ledger goes ACTIVE before hardware is touched: `vlan_assignments`
  rows are written by `find_or_assign_vlan` before the switch loop runs
  (`nats_consumer.py:703` vs `:729`), so a driver-reported failure leaves an
  ACTIVE row that claims a VLAN the hardware never took.
- Assignment rows carry no intended direction, so both retry channels
  rebuild every FAILED row as a connect, re-connecting pairs whose intent
  was release (issue #369; `wiring_retry_service.py:139` hard-codes an empty
  release set).
- The legacy L1 device-set resolver pairs switch ports positionally
  (`_resolve_l1_switch_operations`, `nats_consumer.py:496-504`), which
  mispairs any reservation wiring two or more cross-connects through one
  switch (issue #366). It still drives ALL initial provisioning and
  device add/remove; the correct chain-walk pairing exists but only serves
  the wiring_changed path.
- The two granularities differ by design and should keep differing:
  `l1_connection_assignments` is per connection; `vlan_assignments` is a
  per-fabric VLAN-number allocation and `route_assignments` a per-switch
  pinned route set. Phase 2 must not flatten allocation into membership.

## Decision 1: The fork is the wiring source of truth for every layer

Execution stops filtering the fork's intended set to L1. A
`reservation.wiring_changed` reconcile considers all three layers of the
fork's connections: L1 rows resolve to switch port pairs exactly as today;
L2 rows resolve to (fabric, switch port) memberships; L3 rows resolve to
switch adjacency. The device set stops being a wiring input (it remains the
input for health-poll tiers, dynamic instances, and reservation membership
itself). ADR 0007 open risk 5 closes: an L2/L3-only fork edit provisions.

## Decision 2: Split allocation from membership; every wiring ledger carries intent

- `vlan_assignments` stays what it is: the per-fabric VLAN-number allocation
  (find-or-assign, partial-unique on ACTIVE (fabric, vlan)). Allocation is a
  database decision, not a hardware outcome, so writing it before driver
  calls remains correct.
- A new `l2_port_assignments` ledger records membership per port:
  (reservation_id, vlan_assignment_id, switch_device_id, port, intended
  [ACTIVE/RELEASED], status [ACTIVE/RELEASED/FAILED], attempts, last_error,
  created_at, released_at), partial-unique on ACTIVE (switch_device_id,
  port, vlan_assignment_id), FAILED-partial index on created_at (the #390
  pattern). Status reflects DRIVER outcomes only, closing the
  ACTIVE-before-hardware gap for membership.
- `route_assignments` stays the per-switch pinned set (issue #20 semantics:
  pin at provision, remove exactly what was pinned) and gains FAILED status
  plus attempts/last_error/intended, additively.
- `l1_connection_assignments` gains the same `intended` column. Backfill:
  existing ACTIVE/RELEASED rows get intended = status; existing FAILED rows
  get intended reconstructed from the most recent connect/disconnect
  execution_run for the pair (the 0014 backfill pattern), defaulting to
  ACTIVE. Both retry channels honor intended (a FAILED row with intended
  RELEASED retries disconnect_ports), closing issue #369 across layers.

## Decision 3: Every L2/L3 driver call is result-gated

The five transport-gated sites route through the shared
`driver_result_failed` helper, and ledger writes key on the gated verdict:
the same conversion the L1 path received. `_recipe_reported_success`'s
stricter missing-key rule remains recipe-only. Closes issue #393. The mock
drivers' fail/raise knobs make every path integration-testable in the
`test_execution_result_gating.py` pattern, which today has no L2/L3
analogue.

## Decision 4: One ordering stream; layered apply within a version

L2/L3 reconciles join the existing per-reservation ordering:
`reservation_wiring_state.last_applied_fork_version` stamps a version only
after the full layered pass. Within one version the apply order is: L1
releases, L1 builds (unchanged), then L2 membership removes, L2 membership
adds (allocating the fabric VLAN on first membership, releasing the
allocation when its last membership releases), then L3 adjacency
deprovision (adjacency-aware: a switch still serving another intended L3
edge keeps its routes), L3 provision (pin-on-first-provision unchanged).
Stale skip, contiguous carried-delta, and the gap-or-heal full reconcile
apply to the whole layered pass; the carried delta gains per-layer released
and built sets (the save's set arithmetic is already layer-aware since
layer is part of the connection identity). Per-connection driver failures
land FAILED ledger rows without NAKing; upstream 5xx NAKs; frozen no-ops:
all Decision 6/7 semantics from ADR 0007, inherited unchanged.

## Decision 5: Teardown and retry become ledger-driven everywhere

Terminal transitions (cancelled, completed, failed) release from the
ledgers: disconnect every ACTIVE L1 pair, remove every ACTIVE L2 membership
and release its allocation, remove every cleanly-removable L3 pin (keeping
the L3 rule that a pin releases only after clean removal). The failed
teardown drops `action_succeeded_for_reservation` in favor of ledger reads,
discharging ADR 0007's deferred migration; the helper remains for the
pre-phase-2 transition fallback and backfill only. The retry channels
(manual and background) extend to L2 membership and L3 route rows with the
same retryable classification, honoring `intended` per Decision 2. The
wiring freeze is direction-scoped (phase 3, vendra-approved 2026-07-22): a
frozen or terminal reservation blocks only BUILD-direction retries;
RELEASE-direction rows stay retryable so a stuck disconnect can finish
after the reservation ends. Accordingly the reservations retry proxy
permits ACTIVE plus the terminal statuses (COMPLETED/CANCELLED/FAILED) and
refuses only PENDING/PENDING_PROVISION.

## Decision 6: Initial provisioning unifies through the fork; the legacy resolvers retire

The fork exists from activation (P3a creates it on the ACTIVE transition
with retry-and-continue, lazy-create as fallback). Activation therefore
stages a wiring_changed for the fork's initial version instead of driving
the legacy device-set resolvers, and reservation.created's L1/L2/L3
provisioning branches retire with `_resolve_l1_switch_operations` (whose
positional pairing is issue #366; retirement closes it by deletion),
`_resolve_l2_switch_operations`, and `_resolve_l3_switch_operations`.

Because retirement lands late in the ladder, issue #366 does not wait for
it: an early phase swaps the positional pairing for the chain-walk grouping
the wiring path already uses (the issue's own suggested fix), so the known
mispairing is closed while the legacy path still serves.

Device add/remove via the device-set PATCH changes meaning, and this is the
one user-visible semantics decision in this ADR: an added device's wiring
comes from the fork, so adding a device wires nothing until a fork save
draws it (the PATCH still drives dynamic instances and health tiers); a
removed device's wiring releases via a staged heal (the sweep's existing
delta-less heal reconciles the fork's intended set, which the fork save
path already prunes of removed devices). The alternative, keeping
device-set wiring for adds only, preserves two provisioning algorithms
forever and is rejected here, but this is flagged for explicit sign-off.

## Decision 7: Layered status and retry surfaces

The wiring-status payload gains a `layer` field per row and grows L2
membership and L3 pin rows (additive; existing consumers read L1 rows
unchanged). The Wiring tab groups rows by layer with the same
status/attempts/error/retry affordances; Retry failed stays one button
covering every retryable row. EXTERNAL_API.md gains the layered shape if
the facade exposes wiring status.

## What does not change

VLAN-number derivation and allocation semantics (find-or-assign, exhaustion
DLQ, the race-retry); L3 route content semantics (latest config version at
provision time, pinned, never re-derived: first-class L3, issue #34, builds
on this later and is out of scope); the device-set PATCH's non-wiring roles;
the frozen-reservation consumer no-op (a late `wiring_changed` on a frozen
reservation is still ignored; the retry-channel freeze becomes
direction-scoped, see Decision 5); the #412 invariant (an ACTIVE row is
immutable to failure writers) which extends to the new ledgers.

## Delivery phases (each independently mergeable)

1. Result gating (#393): the five sites through driver_result_failed, plus
   live integration coverage with the mock L2/L3 fail knobs. No schema.
2. Mispair fix (#366): chain-walk pairing replaces positional in
   `_resolve_l1_switch_operations`; multi-pair-through-one-switch
   integration test that fails on the old code.
3. Ledger schema (#369 included): `intended` on l1_connection_assignments
   with backfill; `l2_port_assignments`; route_assignments FAILED/attempts
   columns; retry channels honor intended. Migrations plus unit pins.
4. Layered event and reconcile, L2 (DELIVERED, issue #416): fork-driven
   membership reconcile on wiring_changed. Membership is derived
   layer-agnostically from the recorded hops (option C, the #416 phase 4
   resolution: a hop terminating on a Layer 2 Switch joins that port, an
   inter-switch trunk contributes none), so fork_connections stay
   L1-hop-only. The L2 pass is ALWAYS a full reconcile against cabling's
   intended set (a released hop cannot prove a membership should leave; only
   the intended set can), runs after the L1 pass (Decision 4 ordering:
   removes then adds), and drives add_to_vlan/remove_from_vlan against the
   l2_port_assignments ledger with result-gated writes, the #412 guard, the
   cross-reservation supersession guard, allocation lifecycle coupling (first
   built membership allocates the fabric VLAN, last leave frees it), and both
   retry channels honoring intended with a `layer` field. The legacy
   device-set path also records its outcomes into the ledger through the
   phases 4-6 transition overlap, absorbed by ledger-ACTIVE idempotency.
   Migration 0019 backfills memberships from historical add_to_vlan runs so
   the first heal after upgrade re-adds nothing.
5. Layered reconcile, L3: adjacency from fork L3 rows, pin lifecycle,
   adjacency-aware release.
6. Ledger-driven teardown: terminal transitions release from ledgers;
   failed-teardown migrates off action_succeeded_for_reservation.
7. Initial-provision unification (DELIVERED, issue #416): activation stages
   the initial wiring_changed; legacy resolvers retire; device-add semantics
   per Decision 6.
8. Surfaces and docs: layered wiring-status and Wiring tab, EXTERNAL_API,
   FEATURES/PLANNED_FEATURES status flips, ADR 0007 status pointer.

## Test plan

- Unit (execution): per-layer set arithmetic including the move-a-port
  case; allocation-vs-membership lifecycle (last membership releases the
  allocation); intended-direction retry for both channels; result-gated
  ledger writes (present-key falsy fails, bare data succeeds); ordering
  inheritance (stale skip, heal, gap) exercised with mixed-layer deltas;
  backfill reconstruction for intended.
- Integration (live, mock drivers): L2 membership follows a fork re-wire
  (the open-risk-5 case: move a DUT's port, VLAN membership moves); L3
  edge add/remove provisions and deprovisions adjacency-aware; result
  gating end to end per layer (the #393 analogue of
  test_execution_result_gating.py); ledger-driven failed teardown; the
  existing test_vlan_assignment.py and test_l3_route_provisioning.py
  scenarios re-expressed against the fork-driven flow (they currently pin
  device-set-driven behavior and will change deliberately: grep-callers
  discipline applies, full suite at every phase gate).
- E2e (Playwright): the Wiring tab shows layered rows; an L2 re-wire
  surfaces membership status; retry per the established effect-assertion
  pattern.
- Every phase gate runs the FULL integration suite (the #406/#409 lesson:
  absence-of-validation and semantics changes are ungreppable).

## Risks and open questions

- Device-add wiring semantics (Decision 6) is user-visible: sign-off
  required, and the release notes must state it.
- Pre-phase-2 reservations: active reservations at upgrade have ledger
  gaps for L2/L3; the first layered reconcile lands on the gap path and
  converges, but L2 memberships built by the legacy path need a backfill
  (from execution_runs, the 0014 pattern) so the first heal does not
  re-add ports already present; same for L3 pins (already stored).
- The L2 edge-to-port resolution (which switch ports an L2 fork row
  implies) must reuse the save-time resolved hops rather than re-deriving
  from the live graph (the Decision 5 verbatim-hops discipline from ADR
  0007); phase 4 must define the recorded shape for L2 rows.
- reservation.updated's adjacency-aware L3 removal semantics must be
  preserved exactly through the heal path; the existing unit pins define
  the contract.
- Ladder length: eight phases is the longest arc since #32; phases 1 to 3
  are small and front-load the three open issues, so value lands early
  even if later phases pause.
