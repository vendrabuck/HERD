# Provisioning and wiring specification

| | |
|---|---|
| Area prefix | `WIRE` (used in rule identifiers, for example `WIRE-ORDER-1`) |
| Verified at | commit `61bebe96` (`v0.6.0-76-g61bebe96`), 2026-10-05 |
| Owning services | execution (`services/execution/`) |
| Other services involved | reservations (stages the events, answers the corroboration check, proxies the user routes), cabling (the fork's intended wiring and routing intent, fabric lookup), inventory (switch devices, templates, config versions, driver packages), the driver packages run in execution's sandbox |
| Design records | [ADR 0006](../design/0006-fork-reconcile-and-as-built.md), [ADR 0007](../design/0007-connection-driven-reconcile.md), [ADR 0009](../design/0009-l2-l3-connection-driven-reconcile.md), [ADR 0014](../design/0014-first-class-layer-3-routing.md) |
| Related guides | [ARCHITECTURE.md](../ARCHITECTURE.md), [DRIVERS.md](../DRIVERS.md), [OPERATIONS.md](../OPERATIONS.md) (DLQ inspection and replay), [ENV_VARS.md](../ENV_VARS.md), [ROLES.md](../ROLES.md), [USER_GUIDE.md](../USER_GUIDE.md) |

All service paths below are the execution service's own paths. Users never call them
directly: reservations proxies the wiring status and retry routes (`reservations.md`,
RES-FORK-18).

## 1. Purpose

A reservation's topology says which devices should be cabled together, which VLANs
their switch ports should join, and which routes the layer 3 switches should carry.
This area turns that intent into device state: it drives the layer 1 matrix switches,
layer 2 switches, and layer 3 switches through their drivers, records in three ledgers
exactly what each switch confirmed, retries what failed, and removes everything it
applied when the reservation ends. It does not decide what the wiring should be (the
fork in cabling does, `topology.md`), does not change reservation status
(`reservations.md`), and does not create virtual instances (`dynamic-resources.md`).

## 2. Actors and permissions

Execution's wiring routes are internal only; the user-facing gate is reservations'
owner-or-admin check (RES-FORK-4) and the endpoint matrix is in [ROLES.md](../ROLES.md).

| Actor | May | May not |
|---|---|---|
| User | Read the wiring status of their own reservation and retry its failed rows, through reservations' proxy | Call execution's internal routes; retry a reservation that is still `PENDING` or `PENDING_PROVISION` (refused by reservations) |
| Admin | The same for any reservation, through the proxy | Call execution's internal routes |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Reservations: read the layered wiring status and trigger a manual retry (section 7) | Anything else in this area; there is no internal write route besides retry |
| The NATS consumer (no user) | Apply `reservation.wiring_changed`, tear down on terminal events; every driver run it makes is attributed to the nil UUID (WIRE-DRIVER-5) | Act on an event reservations does not corroborate (WIRE-GATE-2) |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Intended wiring | The fork's recorded L1 hops (`device_a_id`, `port_a`, `device_b_id`, `port_b`, `physical_connection_id`, `edge_key`) plus its resolved L3 routing intent; read from cabling on every apply, never stored here | cabling | cabling's fork tables (`topology.md`) |
| Hop | One recorded physical cable segment between two device ports | cabling | cabling's `fork_connections` |
| L1 cross-connect assignment | One switch port pair this reservation asked a matrix switch to connect. `reservation_id` and `switch_device_id` are bare ids, no foreign key | execution | `l1_connection_assignments` (`L1ConnectionAssignment` in `services/execution/app/models/l1_connection_assignment.py`) |
| L2 membership | One (switch, port) this reservation put into its fabric VLAN | execution | `l2_port_assignments` (`L2PortAssignment`) |
| VLAN allocation | The VLAN number one reservation uses in one fabric, plus the switches the VLAN must be defined on and has been defined on | execution | `vlan_assignments` (`VlanAssignment`); `fabric_id` comes from cabling, bare id, and is the component key at creation time only (WIRE-VLAN-2) |
| L3 route pin | The route list this reservation installed on one L3 switch | execution | `route_assignments` (`RouteAssignment`) |
| Assignment row fields | `status` (ACTIVE, FAILED, RELEASED), `intended` (ACTIVE or RELEASED: the direction the last write attempted), `attempts`, `last_error`, `claimed_until`, `created_at`, `released_at` | execution | the three ledger tables |
| Wiring state | Per reservation: the last fork version applied and whether wiring is frozen | execution | `reservation_wiring_state` (`ReservationWiringState`) |
| Execution run | The audit row of one driver action | execution | `execution_runs` (`operations-and-observability.md`) |
| Fabric | The connected component of the cabling graph a switch belongs to, transit included, as cabling computes it on the current graph | cabling | cabling (`GET /fabric/internal`) |
| Switch config version | The latest stored config of an L3 switch (interfaces, virtual routers, routes) | inventory | inventory (`device-configuration.md`) |

## 4. State model

Four small state machines live here. Only this section states their transitions.

**Statuses: assignment row (all three ledgers).**

- `ACTIVE`: the switch confirmed the change; this reservation holds it.
- `FAILED`: a change could not be confirmed. `intended` says which way: `ACTIVE` means
  a build that did not land, `RELEASED` means a removal that did not land.
- `RELEASED`: removed and confirmed, or settled without a driver call. Final.

**Transitions: assignment row.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | `ACTIVE` | `record_l1_connect`, `record_l2_membership_active`, `record_route_active` after a successful build | wiring not frozen; no FAILED row of this reservation for the key | nothing | WIRE-LEDGER-1, WIRE-LEDGER-2, WIRE-LEDGER-3 |
| `FAILED` | `ACTIVE` | the same three functions | compare-and-swap on status `FAILED`; wiring not frozen | nothing | WIRE-LEDGER-2 |
| (none) or `FAILED` | `FAILED`, intended `RELEASED` | the same three functions | wiring frozen at record time | nothing | WIRE-LEDGER-4 |
| (none) | `FAILED` | `record_l1_failed`, `record_l2_failed`, `record_route_failed` (reconcile path) | no non-RELEASED row for the key | nothing | WIRE-LEDGER-5, WIRE-LEDGER-6 |
| `FAILED` | `FAILED` | the failure writers; `park_stale_l1_build`, `park_stale_l2_build`, `park_stale_route_build` | key match (reconcile) or row id plus status `FAILED` (retry) | nothing | WIRE-LEDGER-6, WIRE-LEDGER-8, WIRE-LEDGER-13 |
| `ACTIVE` | `FAILED`, intended `RELEASED` | the failure writers on a failed removal | release direction only | nothing | WIRE-LEDGER-7 |
| `ACTIVE` | `FAILED`, intended `RELEASED` | `record_l2_membership_active` of another reservation | the row's own reservation is frozen | nothing | WIRE-LEDGER-12 |
| `ACTIVE` | `FAILED`, intended `RELEASED` | `record_route_reconciled` | routes unchanged since the delta was computed; wiring frozen | nothing | WIRE-LEDGER-14 |
| `ACTIVE` | `FAILED`, intended `ACTIVE` | `record_route_reconcile_failed` | routes unchanged since the delta was computed | nothing | WIRE-LEDGER-15 |
| `FAILED`, intended `ACTIVE` | `FAILED`, intended `RELEASED` | `park_stale_route_build` at terminal teardown | the row is still `FAILED` | nothing | WIRE-TEARDOWN-6 |
| `ACTIVE` | `ACTIVE` (new route list) | `record_route_reconciled` | routes unchanged since the delta was computed; wiring not frozen | nothing | WIRE-LEDGER-14 |
| `ACTIVE` or `FAILED` | `RELEASED` | `release_l1_connection`, `release_l2_membership`, `release_route_membership` after a successful removal or a settlement | row is this reservation's, not RELEASED | nothing | WIRE-LEDGER-9 |
| `ACTIVE` | `RELEASED` | `record_l1_connect` of another reservation | same switch and canonical pair | nothing | WIRE-LEDGER-11 |
| `FAILED`, intended `RELEASED` | `RELEASED` | `supersede_release_if_reclaimed`, `supersede_l2_release_if_reclaimed` | another reservation holds an ACTIVE row on the port | nothing | WIRE-LEDGER-16 |

No transition leaves `RELEASED` (WIRE-LEDGER-10).

**Statuses: VLAN allocation.** `ACTIVE` (the number is held in the fabric) and
`RELEASED` (freed; final).

**Transitions: VLAN allocation.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | `ACTIVE` | `find_or_assign_allocation` | no ACTIVE allocation of the reservation reaches the component; number held by no ACTIVE allocation that reaches it | nothing | WIRE-VLAN-1, WIRE-VLAN-2, WIRE-VLAN-3 |
| `ACTIVE` | `RELEASED` | `_release_orphaned_allocations` | zero ACTIVE memberships reference it | nothing | WIRE-VLAN-4 |

**Wiring state row.** It has no status column; two fields move.

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | row with `last_applied_fork_version` set | `stamp_last_applied` after an apply | none | nothing | WIRE-ORDER-1 |
| `last_applied_fork_version` = v | larger value | `stamp_last_applied` | new version greater than v | nothing | WIRE-ORDER-1 |
| (none) or `frozen` false | `frozen` true | `freeze_reservation_wiring` on a terminal event | none | nothing | WIRE-FREEZE-1 |

**Claim stamp (`claimed_until` on an assignment row).**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| null or in the past | now plus the claim budget | `WiringRowClaims.claim` | row status `FAILED` | nothing | WIRE-CLAIM-1, WIRE-CLAIM-3 |
| any | null | every record and park write | none | nothing | WIRE-CLAIM-2 |

**Concurrency.** Inside one replica the consumer handles one message at a time; the
background retry tick runs in the same process and can interleave with it. Successful
build flips and retry failure writes are SQL compare-and-swaps on status; the two L3
reconcile writers compare the pinned routes under a row lock; a partial-unique index per
ledger is the final arbiter for ACTIVE rows (WIRE-LEDGER-3); the two retry channels
exclude each other with the claim stamp (WIRE-CLAIM-1). Freeze is monotonic, and every
build recorder re-checks it at record time (WIRE-LEDGER-4).

**Rules.**

- **WIRE-LEDGER-1.** An assignment row is written `ACTIVE` only after the driver call it
  records was judged successful (WIRE-DRIVER-3); a failed call never produces an ACTIVE
  row. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`, `_apply_one_vlan_action`, `_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_driver_result_failure_lands_failed_row`); `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_failed_add_lands_failed_not_active`); `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_reconcile_failed_provision_lands_failed_intended_active`)
- **WIRE-LEDGER-2.** A successful build returns this reservation's existing ACTIVE row
  for the key unchanged; otherwise it flips this reservation's FAILED row for the key to
  ACTIVE in place by a compare-and-swap on status `FAILED` (a lost swap re-reads and
  returns the winner's row untouched); otherwise it inserts a new ACTIVE row. The key is
  (switch, canonical port pair) for L1, (switch, port) for L2, and (switch) for L3. An L3
  flip replaces the stored routes with the routes just driven; the provision that drove
  them first removed the stored routes they no longer name (WIRE-L3-19). \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_connect`, `_find_reusable_failed`); `services/execution/app/services/l2_membership_service.py` (`record_l2_membership_active`); `services/execution/app/services/route_service.py` (`record_route_active`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_record_connect_inserts_active_row`, `test_record_connect_reusable_failed_flip_sets_intended_active`); `services/execution/tests/test_l2_membership_service.py` (`test_record_active_reuses_prior_failed_row`); `services/execution/tests/test_wiring_claim.py` (`test_l1_success_flip_returns_the_winner_when_its_cas_loses`, `test_l2_success_flip_returns_the_winner_when_its_cas_loses`); `services/execution/tests/test_route_service.py` (`test_record_route_active_is_idempotent_for_redelivery`, `test_record_route_active_reusable_cas_loser_never_overwrites`)
- **WIRE-LEDGER-3.** A partial-unique index allows at most one ACTIVE row per L1
  (switch, port pair), per L2 (switch, port, VLAN allocation), and per L3 (reservation,
  switch); an insert that loses that race rolls back and returns the winner's row. \
  Enforced in: `services/execution/app/models/l1_connection_assignment.py` (`uq_l1_active_per_switch_pair`); `services/execution/app/models/l2_port_assignment.py` (`uq_l2_active_per_switch_port_vlan`); `services/execution/app/models/route_assignment.py` (`uq_route_active_per_res_device`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_partial_unique_index_blocks_duplicate_active_insert`, `test_record_connect_loses_race_returns_winner`, `test_failed_row_does_not_block_new_active_claim`); `services/execution/tests/test_l2_port_assignment_model.py` (`test_partial_unique_index_blocks_duplicate_active_insert`); `services/execution/tests/test_route_service.py` (`test_partial_unique_index_blocks_duplicate_active_insert`)
- **WIRE-LEDGER-4.** A successful build recorded after the reservation's wiring froze is
  not recorded ACTIVE: the row is inserted, or this reservation's FAILED row flipped, as
  `FAILED` intended `RELEASED` with a fixed reason (`FROZEN_BUILD_PENDING_RELEASE`,
  `FROZEN_JOIN_PENDING_REMOVAL`, `FROZEN_PROVISION_PENDING_REMOVAL`) and unchanged
  attempts, so the release-direction retry channels remove what was built. An L3 flip
  keeps the stored routes and adds the routes just configured (`union_routes`), so the
  removal covers both. An L1 pair this reservation already holds ACTIVE is returned
  unchanged before the freeze check. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_connect`, `_park_frozen_build`); `services/execution/app/services/l2_membership_service.py` (`record_l2_membership_active`); `services/execution/app/services/route_service.py` (`record_route_active`, `union_routes`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_record_connect_frozen_parks_failed_intended_released`, `test_record_connect_frozen_reuses_failed_row_and_keeps_attempts`, `test_record_connect_frozen_short_circuits_own_active_row_unchanged`); `services/execution/tests/test_l2_membership_service.py` (`test_record_active_frozen_parks_failed_intended_released`); `services/execution/tests/test_route_service.py` (`test_record_route_active_frozen_parks_failed_intended_released`); `services/execution/tests/test_wiring_retry_service.py` (`test_tick_build_racing_terminal_freeze_does_not_strand_active_row`)
- **WIRE-LEDGER-5.** Every failure write states the direction it attempted: intended
  `ACTIVE` for a failed build, `RELEASED` for a failed removal. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_failed`); `services/execution/app/services/l2_membership_service.py` (`record_l2_failed`); `services/execution/app/services/route_service.py` (`record_route_failed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_release_direction_driver_failure_lands_failed_row_intended_released`); `services/execution/tests/test_l2_membership_service.py` (`test_412_guard_does_not_block_release_direction_failure`); `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_record_route_failed_release_direction_flips_active_to_failed`)
- **WIRE-LEDGER-6.** On the reconcile path (no row id) a failure write updates this
  reservation's non-RELEASED row for the key, adding this pass's attempts to the total
  and replacing `last_error`, or inserts a new FAILED row when there is none. An L2
  failure with no resolved allocation stores the nil UUID as its allocation; an L3
  failure with no routes stores an empty list. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_failed`); `services/execution/app/services/l2_membership_service.py` (`record_l2_failed`); `services/execution/app/services/route_service.py` (`record_route_failed`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_failed_record_accumulates_attempts_on_failed_row`, `test_failed_record_creates_fresh_row_when_none_exists`); `services/execution/tests/test_l2_membership_service.py` (`test_record_failed_accumulates_attempts`, `test_record_failed_present_key_falsy_is_a_failure_semantics`)
- **WIRE-LEDGER-7.** A build-direction failure write never changes an ACTIVE row: a
  concurrent writer already proved the change. A release-direction failure write flips an
  ACTIVE row to `FAILED` intended `RELEASED`. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_failed`); `services/execution/app/services/l2_membership_service.py` (`record_l2_failed`); `services/execution/app/services/route_service.py` (`record_route_failed`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_failed_record_never_downgrades_active_row`, `test_failed_release_direction_does_flip_an_active_row`); `services/execution/tests/test_l2_membership_service.py` (`test_412_guard_active_row_immutable_to_build_failure`); `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_record_route_failed_ignores_stale_build_failure_on_active_row`)
- **WIRE-LEDGER-8.** A retry channel's failure write is one `UPDATE` matching the row id
  it loaded and status `FAILED`; when the row has moved (flipped ACTIVE, or RELEASED by a
  save), nothing is written and nothing is inserted. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`_record_l1_failed_for_row`); `services/execution/app/services/l2_membership_service.py` (`_record_l2_failed_for_row`); `services/execution/app/services/route_service.py` (`_record_route_failed_for_row`) \
  Pinned by: `services/execution/tests/test_wiring_retry_stale_write.py` (`test_l1_tick_failure_write_never_resurrects_a_row_released_mid_flight`, `test_l2_tick_failure_write_never_resurrects_rows_released_mid_flight`, `test_l3_tick_failure_write_never_resurrects_a_pin_released_mid_flight`, `test_l2_tick_failure_write_no_ops_when_the_row_went_active`, `test_l1_tick_failure_write_applies_while_the_row_is_still_failed`)
- **WIRE-LEDGER-9.** A successful removal, or a settlement with no driver call, flips this
  reservation's non-RELEASED row for the key (ACTIVE, or FAILED) to `RELEASED`, intended
  `RELEASED`, clears `last_error`, and stamps `released_at`; no matching row is a no-op. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`release_l1_connection`); `services/execution/app/services/l2_membership_service.py` (`release_l2_membership`); `services/execution/app/services/route_service.py` (`release_route_membership`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_release_flips_active_to_released`, `test_release_flips_retried_failed_row_to_released`, `test_release_no_matching_active_row_is_noop`); `services/execution/tests/test_l2_membership_service.py` (`test_release_flips_release_direction_failed_row`, `test_release_missing_returns_none`)
- **WIRE-LEDGER-10.** A RELEASED row is never rewritten: every keyed read for a write
  excludes RELEASED rows, the row-id failure write and the claim both require status
  `FAILED`, and a later build of the same key inserts a new row. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`_find_reusable_failed`, `record_l1_failed`); `services/execution/app/services/wiring_claim.py` (`WiringRowClaims`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_release_frees_the_pair_for_a_new_claim`); `services/execution/tests/test_wiring_claim.py` (`test_a_row_no_longer_failed_cannot_be_claimed`)
- **WIRE-LEDGER-11.** A successful L1 connect flips another reservation's ACTIVE row on
  the same switch and canonical pair to `RELEASED` in the same commit, because a matrix
  port carries one cross-connect. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_connect`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_record_connect_supersedes_stale_cross_reservation_active_row`)
- **WIRE-LEDGER-12.** A successful L2 join parks every other reservation's ACTIVE row on
  the same (switch, port) as `FAILED` intended `RELEASED`, attempts 0, reason
  `STALE_JOIN_SUPERSEDED_PENDING_REMOVAL`, if and only if that reservation's wiring is
  frozen; a row of an unfrozen reservation is left alone. \
  Enforced in: `services/execution/app/services/l2_membership_service.py` (`record_l2_membership_active`) \
  Pinned by: `services/execution/tests/test_l2_membership_service.py` (`test_record_parks_stale_cross_reservation_active_row`, `test_record_parks_every_stale_row_on_the_port`, `test_record_leaves_unfrozen_cross_reservation_row_alone`, `test_record_leaves_unfrozen_state_row_cross_reservation_alone`)
- **WIRE-LEDGER-13.** Parking a stale build flips a FAILED row to intended `RELEASED`
  with attempts 0 and the given reason; a row that is no longer FAILED is returned
  untouched. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`park_stale_l1_build`); `services/execution/app/services/l2_membership_service.py` (`park_stale_l2_build`); `services/execution/app/services/route_service.py` (`park_stale_route_build`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_park_stale_build_flips_failed_intended_released`, `test_park_stale_build_non_failed_row_left_untouched`, `test_park_stale_build_missing_row_returns_none`)
- **WIRE-LEDGER-14.** `record_route_reconciled` locks this reservation's ACTIVE pin for
  the switch and acts only when its stored routes equal the routes the delta was
  computed against (else it is a logged no-op): when frozen it flips the pin `FAILED`
  intended `RELEASED` keeping the prior routes, otherwise it stores the new route list
  and stays ACTIVE. \
  Enforced in: `services/execution/app/services/route_service.py` (`record_route_reconciled`) \
  Pinned by: `services/execution/tests/test_route_service.py` (`test_record_route_reconciled_advances_the_pin_on_an_active_row`, `test_record_route_reconciled_stale_previous_routes_is_a_noop`, `test_record_route_reconciled_frozen_parks_failed_intended_released`, `test_record_route_reconciled_returns_none_when_no_active_row`)
- **WIRE-LEDGER-15.** `record_route_reconcile_failed` flips an ACTIVE pin, under the same
  locked routes comparison, to `FAILED` intended `ACTIVE`, adding the attempts; it has no
  build-direction guard. The stored routes become what may still be installed: unchanged
  when nothing was driven (a gate refusal, a load failure, a failed login), otherwise the
  previous routes whose removal did not confirm plus every route the delta tried to add.
  The row then has no ACTIVE pin, so the next save provisions the switch as newly
  adjacent (WIRE-L3-2, WIRE-L3-3) and a build retry drives it (WIRE-RETRY-9); either one
  removes the stored routes its route set drops before configuring it (WIRE-L3-19), and
  a terminal teardown removes them all (WIRE-TEARDOWN-6). \
  Enforced in: `services/execution/app/services/route_service.py` (`record_route_reconcile_failed`); `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_route_service.py` (`test_record_route_reconcile_failed_keeps_previous_pin`, `test_record_route_reconcile_failed_is_not_guarded_by_412_unlike_record_route_failed`, `test_record_route_reconcile_failed_stale_previous_routes_is_a_noop`, `test_record_route_reconcile_failed_returns_none_when_not_active`, `test_record_route_reconcile_failed_records_possibly_installed_routes`, `test_record_route_reconcile_failed_stale_writer_ignores_possibly_installed`); `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_delta_partial_failure_records_possibly_installed_routes_lands_failed`, `test_failed_delta_records_previous_and_added_routes_when_remove_fails`)
- **WIRE-LEDGER-16.** A release-direction FAILED row is settled `RELEASED` with no driver
  call when another reservation holds an ACTIVE row on the same switch claiming either
  port of the pair (L1) or the same (switch, port) (L2). L3 has no such settlement:
  route pins are per reservation and never displace each other. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`supersede_release_if_reclaimed`); `services/execution/app/services/l2_membership_service.py` (`supersede_l2_release_if_reclaimed`); `services/execution/app/services/wiring_retry_service.py` (`_reattempt_l3_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_retry_supersession_flips_release_row_without_driver_call`, `test_retry_no_supersession_when_active_row_same_reservation_fires_driver`); `services/execution/tests/test_l2_membership_service.py` (`test_supersede_when_other_reservation_active_on_same_port`, `test_supersede_false_when_no_other_reservation`); `services/execution/tests/test_wiring_retry_l3.py` (`test_l3_release_is_not_superseded_by_another_reservation_on_same_switch`)
- **WIRE-VLAN-1.** `find_or_assign_allocation` returns the existing ACTIVE allocation of
  the reservation that reaches the component being allocated for (WIRE-VLAN-2's
  reachability test), the oldest when several do, so a cable change never gives one
  reservation a second number inside one component; otherwise it inserts one ACTIVE
  allocation. A reservation that already holds a reachable allocation is answered from
  its own rows without looking up any other reservation's switches. \
  Enforced in: `services/execution/app/services/vlan_service.py` (`find_or_assign_allocation`, `_reachable_active`) \
  Pinned by: `services/execution/tests/test_vlan_service.py` (`test_assign_vlan_idempotent`, `test_assign_vlan_concurrent_same_reservation_idempotent`, `test_same_reservation_reuses_reachable_allocation_after_cable_change`, `test_idempotent_path_does_not_walk_other_reservations`); `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_cable_change_keeps_one_allocation_and_scope_for_one_reservation`)
- **WIRE-VLAN-2.** The uniqueness scope is reachability on the current cabling graph
  (#1003): a number held by an ACTIVE allocation anywhere in the connected component of
  the switches being allocated for, transit switches included, is never given to
  another reservation. An allocation reaches the component when its stored `fabric_id`
  equals the component's current fabric id, or when cabling's CURRENT fabric id for any
  of its anchor switches (`switch_device_ids` plus `defined_switch_ids`) does; the
  stored `fabric_id` is only the key at creation time and is never compared alone. A new
  allocation takes the reservation's preferred VLAN (derived from its id, 2 to 4094)
  when no reaching allocation holds it, else the lowest free one. The read, choice, and
  insert run under one transaction-scoped advisory lock (`herd-execution-vlan-allocation`);
  the partial-unique index on (fabric_id, vlan_id) stays as a backstop, and an insert
  that trips it recomputes, at most five times, then raises `RuntimeError`. The same
  number may be used in two components that share no cable. Fabric lookups are
  memoized per pass, so one allocation costs one lookup per switch being allocated for
  plus one per distinct anchor switch of the other live allocations. \
  Enforced in: `services/execution/app/services/vlan_service.py` (`find_or_assign_allocation`, `allocation_reaches`, `allocation_anchor_switches`, `FabricResolver`, `_derive_vlan_id`, `_MAX_ASSIGN_RETRIES`, `_ALLOCATION_LOCK_KEY`); `services/execution/app/models/vlan_assignment.py` (`uq_vlan_active_per_fabric`) \
  Pinned by: `services/execution/tests/test_vlan_service.py` (`test_derive_vlan_id_range`, `test_assign_vlan_conflict_same_fabric`, `test_assign_vlan_loses_race_retries_onto_free_vlan`, `test_assign_vlan_same_id_different_fabric`, `test_cable_change_between_joins_keeps_numbers_distinct`, `test_cable_removed_split_components_may_reuse_number`, `test_defined_switch_counts_as_an_anchor`, `test_row_inserted_before_the_lock_is_seen_under_it`); `services/execution/tests/test_vlan_service_edges.py` (`test_assign_vlan_raises_on_persistent_contention`, `test_fabric_resolver_memoizes_and_does_not_cache_failures`); `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_cable_change_between_two_reservations_joins_keeps_numbers_distinct`); `tests/integration/test_vlan_assignment.py` (`test_vlan_ids_stay_distinct_across_a_cable_change_between_joins`)
- **WIRE-VLAN-3.** When every number in reach is in use, allocation raises
  `PermanentEventError`, so the event is dead-lettered on its first delivery
  (WIRE-CONSUME-9). \
  Enforced in: `services/execution/app/services/vlan_service.py` (`find_or_assign_allocation`) \
  Pinned by: `services/execution/tests/test_vlan_service_edges.py` (`test_assign_vlan_raises_when_all_in_use`)
- **WIRE-VLAN-4.** After a membership pass, each allocation that a removal in that pass
  touched is released when it has no ACTIVE membership left; a port moved within one
  fabric keeps its allocation. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_release_orphaned_allocations`); `services/execution/app/services/l2_membership_service.py` (`count_active_memberships_for_vlan`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_last_release_frees_allocation`, `test_reconcile_move_within_fabric_keeps_allocation`); `services/execution/tests/test_l2_membership_service.py` (`test_count_active_memberships_tracks_allocation_lifecycle`)
- **WIRE-ORDER-1.** `stamp_last_applied` inserts the wiring state row or raises its
  version; it never lowers the version and never clears `frozen`, including when it loses
  an insert race. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`stamp_last_applied`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_stamp_last_applied_inserts_row_when_absent`, `test_stamp_last_applied_advances_existing_row`, `test_stamp_last_applied_never_regresses`, `test_stamp_last_applied_loses_race_does_not_regress_a_higher_winner_value`)
- **WIRE-FREEZE-1.** `freeze_reservation_wiring` sets `frozen` true, inserting the row if
  absent (an insert race flips the winner's row); no code sets it back to false. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`freeze_reservation_wiring`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_freeze_inserts_row_when_absent`, `test_freeze_is_idempotent`, `test_freeze_loses_race_flips_winners_row_frozen`)
- **WIRE-CLAIM-1.** A claim is one `UPDATE` setting `claimed_until` to now plus the
  budget where the row id matches, status is `FAILED`, and `claimed_until` is null or in
  the past; zero rows means the claim is lost. An expired stamp is reclaimable with no
  reaper. \
  Enforced in: `services/execution/app/services/wiring_claim.py` (`WiringRowClaims`, `unclaimed`) \
  Pinned by: `services/execution/tests/test_wiring_claim.py` (`test_claim_stamps_the_budget_and_a_second_claimant_loses`, `test_a_row_no_longer_failed_cannot_be_claimed`, `test_claim_expiry_lets_a_new_pass_take_a_dead_holders_row`)
- **WIRE-CLAIM-2.** Every success flip, release, failure write (by key or by row id),
  stale park, and frozen park of an own FAILED row sets `claimed_until` to null.
  `record_route_reconciled`'s frozen branch does not, and no retry claims the rows it
  writes. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`record_l1_connect`, `release_l1_connection`, `record_l1_failed`, `park_stale_l1_build`); `services/execution/app/services/l2_membership_service.py` (`record_l2_membership_active`, `release_l2_membership`, `record_l2_failed`, `park_stale_l2_build`); `services/execution/app/services/route_service.py` (`record_route_active`, `release_route_membership`, `record_route_failed`, `park_stale_route_build`) \
  Pinned by: `services/execution/tests/test_wiring_claim.py` (`test_l1_success_flip_clears_the_claim`, `test_l1_release_clears_the_claim`, `test_l1_row_identity_failure_write_clears_the_claim`, `test_l1_key_upsert_failure_write_clears_the_claim`, `test_l2_stale_build_park_clears_the_claim`, `test_l3_success_flip_clears_the_claim`, `test_l3_release_clears_the_claim`)
- **WIRE-CLAIM-3.** The claim budget is three attempts times three driver actions times
  `EXECUTION_TIMEOUT_SECONDS`, plus the in-line backoff, rounded up to whole minutes
  (5 minutes at the defaults); there is no setting of its own. \
  Enforced in: `services/execution/app/services/wiring_claim.py` (`claim_budget`) \
  Pinned by: `services/execution/tests/test_wiring_claim.py` (`test_claim_budget_covers_one_row_worst_case_drive`, `test_claim_budget_scales_with_the_driver_timeout`)

## 5. API surface

None. Execution serves no user-facing wiring route. Users read wiring status and retry
failed rows through reservations' `GET /{id}/wiring-status` and `POST /{id}/wiring/retry`
(`reservations.md`, RES-FORK-4 and RES-FORK-18), which forward to the internal routes in
section 7. Execution's run-listing routes (`GET /runs`) belong to
`operations-and-observability.md`.

## 6. Events

This area publishes only dead-letter copies.

| Subject | Producer | Staged when | Consumers | Payload keys | Rules |
|---|---|---|---|---|---|
| `herd.reservations.dlq.execution` | execution consumer | a message is undecodable, raises `PermanentEventError`, or fails at its fifth delivery (published directly, not through an outbox) | none; retained in the `HERD_DLQ` stream for inspection and replay | the original message bytes, unchanged | WIRE-CONSUME-8, WIRE-CONSUME-9, WIRE-CONSUME-11, WIRE-CONSUME-13 |

Events consumed from `HERD_RESERVATIONS` (all produced by reservations, section 6 of
`reservations.md`):

| Subject | What this area does | Rules |
|---|---|---|
| `herd.reservations.wiring_changed` | ordered apply of the fork's wiring | WIRE-ORDER-2 to WIRE-ORDER-13 |
| `herd.reservations.cancelled`, `completed`, `failed` | freeze, then tear down from the ledgers | WIRE-DISPATCH-3, WIRE-TEARDOWN-1 to WIRE-TEARDOWN-6 |
| `herd.reservations.created` | no wiring (health tiers only) | WIRE-DISPATCH-1 |
| `herd.reservations.updated` | no wiring (dynamic teardown of removed devices only) | WIRE-DISPATCH-2 |
| `herd.reservations.provision_requested` | not wiring; `dynamic-resources.md` | WIRE-GATE-1 |
| any other subject on the stream | logged and acked | WIRE-DISPATCH-4 |

## 7. Internal API

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| GET | `/internal/reservations/{reservation_id}/wiring-status` | `X-Internal-Token` | reservations (wiring status proxy) | `{reservation_id, last_applied_fork_version, frozen, connections: [...]}` | WIRE-STATUS-1 to WIRE-STATUS-6 |
| POST | `/internal/reservations/{reservation_id}/wiring/retry` | `X-Internal-Token` | reservations (wiring retry proxy) | `{reservation_id, results: [...]}`, each result carrying `outcome` | WIRE-RETRY-1 to WIRE-RETRY-12, WIRE-STATUS-5, WIRE-STATUS-6 |

Neither route is mounted on a replica started with `EXECUTION_POLLER_ONLY=true`
(WIRE-STATUS-7).

## 8. Features

### 8.1 Consuming reservation events

**What it does.** Execution listens to every reservation lifecycle event and processes
them one at a time, so wiring work happens in the order reservations announced it. A
message that cannot be processed is retried with a delay and, failing that, set aside
for an operator.

**Surfaces.** `start_nats_consumer` and `process_batch` in
`services/execution/app/services/nats_consumer.py`, started from
`services/execution/app/main.py`; the shared helpers in
`services/common/herd_common/jetstream.py`. Subjects are listed in section 6.

**Rules.**

- **WIRE-CONSUME-1.** One durable pull consumer, `execution-consumer`, reads
  `HERD_RESERVATIONS` filtered to `herd.reservations.*`, with `max_deliver` 5, `ack_wait`
  `NATS_ACK_WAIT_SECONDS`, and no `backoff`; its config is created or updated on the
  server before the subscription binds. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`start_nats_consumer`, `NATS_MAX_DELIVER`, `NATS_ACK_WAIT_SECONDS`); `services/common/herd_common/jetstream.py` (`ensure_consumer`) \
  Pinned by: `services/execution/tests/test_nats_consumer_full.py` (`test_start_nats_consumer_success`); `services/execution/tests/test_nats_consumer_heartbeat.py` (`test_ack_wait_is_the_real_effective_window_no_backoff_shrinks_it`); `services/execution/tests/test_config_ack_wait.py` (`test_consumer_module_derives_both_values_from_settings`); `tests/integration/test_nats_consumer_configs_live.py` (`test_consumers_have_real_ack_wait_and_no_backoff`)
- **WIRE-CONSUME-2.** The loop fetches one message per pull with a 5 second wait; an
  empty wait fetches again, and any other fetch error waits 5 seconds and fetches again. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`start_nats_consumer`, `NATS_FETCH_TIMEOUT_SECONDS`) \
  Pinned by: `services/execution/tests/test_nats_consumer_full.py` (`test_start_nats_consumer_loop_fetches_one_message_at_a_time`)
- **WIRE-CONSUME-3.** The consumer never writes the stream's config: it creates
  `HERD_RESERVATIONS` (with no `max_age`) only when the stream is missing, and a failure
  there is logged while the consumer still starts. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`start_nats_consumer`); `services/common/herd_common/jetstream.py` (`ensure_stream_exists`) \
  Pinned by: `services/execution/tests/test_nats_consumer_full.py` (`test_start_nats_consumer_stream_create_failure`); `services/common/tests/test_jetstream.py` (`test_ensure_stream_exists_existing_stream_never_calls_add_stream`, `test_ensure_stream_exists_not_found_triggers_one_add_stream_with_no_max_age`)
- **WIRE-CONSUME-4.** When NATS is unreachable at startup the failure is logged and the
  service runs without the consumer; once connected, the client reconnects without limit.
  Known gap, see #1083: with no broker reachable the connect call retries and never
  raises, so startup waits for NATS (`operations-and-observability.md`, OPS-NATS-1). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`start_nats_consumer`) \
  Pinned by: `services/execution/tests/test_nats_consumer_full.py` (`test_start_nats_consumer_connection_failure`)
- **WIRE-CONSUME-5.** On a migration-managed schema that lacks a model table, the
  consumer start is deferred until the table appears, so events wait on the stream
  instead of failing. \
  Enforced in: `services/execution/app/main.py` (`lifespan`); `services/common/herd_common/consumer_schema_gate.py` (`start_consumer_when_schema_ready`) \
  Pinned by: `services/execution/tests/test_main.py` (`test_lifespan_defers_consumer_on_managed_schema_missing_tables`, `test_lifespan_starts_consumer_immediately_when_schema_ready`)
- **WIRE-CONSUME-6.** While a fetched message is being handled, every unsettled message
  of the batch gets an in-progress signal every half of `ack_wait`, so a slow wiring pass
  is not redelivered; the signal stops for a message as soon as it is settled. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_batch`, `NATS_HEARTBEAT_SECONDS`); `services/common/herd_common/jetstream.py` (`process_batch_with_heartbeat`, `heartbeat_interval`) \
  Pinned by: `services/execution/tests/test_nats_consumer_heartbeat.py` (`test_process_batch_heartbeats_a_slow_message_until_it_settles`, `test_heartbeat_interval_is_below_ack_wait`); `tests/unit/test_consumer_heartbeat_wiring.py` (`test_every_pull_consumer_module_uses_the_shared_heartbeat`)
- **WIRE-CONSUME-7.** Every driver call made while handling an event runs on a worker
  thread, so the event loop keeps sending heartbeats while a switch answers. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_run_sandbox`) \
  Pinned by: `services/execution/tests/test_nats_consumer_heartbeat.py` (`test_run_sandbox_runs_off_the_event_loop`)
- **WIRE-CONSUME-8.** A message whose body is not JSON is published to
  `herd.reservations.dlq.execution` and acked, logged `nats_poison_message`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_poison_json_routes_to_dlq_and_acks`); `tests/integration/test_dlq_and_idempotency.py` (`test_poison_reservation_event_is_retained_in_dlq`)
- **WIRE-CONSUME-9.** A `PermanentEventError` from the handler dead-letters the message on
  its first delivery and acks it, logged `nats_dlq_permanent`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`, `PermanentEventError`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_permanent_error_dlqs_on_first_delivery`)
- **WIRE-CONSUME-10.** Any other exception before the fifth delivery naks the message with
  a delay taken from `NATS_NAK_BACKOFF_SECONDS`: delivery n gets entry n, clamped to the
  last entry. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`, `NATS_NAK_BACKOFF_SECONDS`); `services/common/herd_common/jetstream.py` (`nak_delay`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_transient_error_naks_below_max_deliver`, `test_process_message_transient_error_first_delivery_uses_first_schedule_entry`, `test_process_message_transient_error_third_delivery_uses_third_schedule_entry`); `services/common/tests/test_jetstream.py` (`test_nak_delay_clamps_num_delivered_past_schedule_length`)
- **WIRE-CONSUME-11.** At the fifth delivery a failing message is dead-lettered and acked,
  logged `nats_dlq_exhausted`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_at_max_deliver_routes_to_dlq_and_acks`)
- **WIRE-CONSUME-12.** A failed dead-letter publish is logged and the message is still
  acked. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_publish_to_dlq`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_dlq_publish_failure_does_not_propagate`)
- **WIRE-CONSUME-13.** The dead-letter subject has four tokens, so the consumer's
  three-token filter never redelivers it; startup creates or updates the `HERD_DLQ`
  stream over `herd.*.dlq.>` with `NATS_STREAM_MAX_AGE_SECONDS`, and a failure there is
  logged without stopping the service. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`NATS_DLQ_SUBJECT`); `services/execution/app/main.py` (`_ensure_dlq_stream`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_dlq_subject_not_redelivered_to_consumer`); `services/execution/tests/test_main.py` (`test_ensure_dlq_stream_binds_shared_stream`, `test_ensure_dlq_stream_swallows_broker_error`)
- **WIRE-CONSUME-14.** A handler that returns normally acks the message. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_happy_path_acks`)
- **WIRE-CONSUME-15.** The handler receives the payload's `event_id` as its dedupe key,
  else `<stream>:<sequence>`; the wiring handler does not use it, and a republished
  wiring event is made harmless by the version check and the ledger gates instead
  (WIRE-ORDER-3, WIRE-L1-1, WIRE-L2-3). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`, `handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_keys_on_payload_event_id`, `test_process_message_falls_back_to_stream_sequence_without_event_id`); `tests/integration/test_dlq_and_idempotency.py` (`test_redelivery_does_not_rerun_succeeded_add_to_vlan`)

**Out of scope.** The outbox relay and the producer side of every event
(`reservations.md`, `operations-and-observability.md`); what a dead-lettered
`provision_requested` triggers (`dynamic-resources.md`).

### 8.2 Event corroboration

**What it does.** Because the message bus has no authentication, execution checks each
lifecycle event against the reservations service before it touches any hardware or
ledger, and ignores an event whose claim the reservation's current status does not
support.

**Surfaces.** `_verify_reservation_event` and `_EVENT_CORROBORATION_RULES` in
`services/execution/app/services/nats_consumer.py`; reservations
`GET /internal/{id}` (section 10).

**Rules.**

- **WIRE-GATE-1.** Before any handler runs, an event listed in the gate's table is
  checked against reservations' status for its `reservation_id`: `cancelled`,
  `completed`, and `failed` need any terminal status; `created` needs
  `PENDING_PROVISION` or `ACTIVE`; `updated` needs any non-terminal status;
  `provision_requested` needs `PENDING_PROVISION`; `wiring_changed` needs `ACTIVE`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_EVENT_CORROBORATION_RULES`, `_verify_reservation_event`) \
  Pinned by: `services/execution/tests/test_nats_consumer_event_verification.py` (`test_event_status_table`, `test_updated_for_a_pending_reservation_is_verified`)
- **WIRE-GATE-2.** A status that does not corroborate, a 404, or any other answer below
  500 that is not 200 acks the message without running the handler, logged
  `nats_event_unverified` with the reported status and a reason. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_verify_reservation_event`, `process_reservation_message`) \
  Pinned by: `services/execution/tests/test_nats_consumer_event_verification.py` (`test_unverified_terminal_event_acks_without_running_the_handler`, `test_gate_404_acks_without_running_the_handler`, `test_created_for_a_cancelled_reservation_is_unverified`); `tests/integration/test_event_verification_gate.py` (`test_forged_cancelled_event_for_an_active_reservation_is_ignored`)
- **WIRE-GATE-3.** A 5xx or transport error from that check raises
  `TransientUpstreamError`, so the message is nacked and retried (fail closed). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_get_internal`, `_verify_reservation_event`) \
  Pinned by: `services/execution/tests/test_nats_consumer_event_verification.py` (`test_5xx_raises_transient_upstream_error`, `test_transport_error_raises_transient_upstream_error`, `test_gate_transport_failure_naks_like_any_other_transient_error`)
- **WIRE-GATE-4.** An event with no table entry, or with no `reservation_id`, makes no
  call and goes straight to its handler. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_verify_reservation_event`) \
  Pinned by: `services/execution/tests/test_nats_consumer_event_verification.py` (`test_event_outside_the_table_is_not_gated`, `test_missing_reservation_id_skips_the_gate`)
- **WIRE-GATE-5.** Only the status is corroborated; device ids carried in a payload are
  not checked against reservations' record. By decision: the internal status answer
  carries no device list (comment above `_EVENT_CORROBORATION_RULES`). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_EVENT_CORROBORATION_RULES`) \
  Pinned by: none

**Out of scope.** Broker authentication (none exists; see section 13).

### 8.3 What each lifecycle event does here

**What it does.** Of the lifecycle events, only `wiring_changed` builds or changes
wiring, and only the three terminal events remove it.

**Surfaces.** `handle_reservation_event` in
`services/execution/app/services/nats_consumer.py`.

**Rules.**

- **WIRE-DISPATCH-1.** `reservation.created` drives no wiring; it only moves the devices'
  health polling tier. Initial wiring arrives as the `wiring_changed` reservations stages
  at activation (`reservations.md`, RES-FORK-3). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`, `HANDLED_RESERVATION_EVENTS`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_created_drives_only_health_tiers_no_wiring`, `test_retired_wiring_symbols_removed`)
- **WIRE-DISPATCH-2.** `reservation.updated` drives no wiring; an added device is wired
  only by a later fork save, and a removed device's wiring is released through the fork
  prune (WIRE-ORDER-13). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_updated_added_only_drives_no_teardown_no_wiring`, `test_updated_removed_devices_drive_dynamic_teardown_only`); `tests/integration/test_device_set_patch_wiring.py` (`test_patch_add_wires_nothing_until_fork_save_and_remove_releases`)
- **WIRE-DISPATCH-3.** A terminal event (`cancelled`, `completed`, `failed`) freezes the
  wiring, then tears down from the ledgers (section 8.12), then runs dynamic-instance
  teardown; one without `reservation_id` is logged and acked with no work. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`, `DYNAMIC_TEARDOWN_EVENTS`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_handle_cancelled_event_dispatches_ledger_teardown`); `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_terminal_event_none_reservation_id_warns_and_skips`)
- **WIRE-DISPATCH-4.** Any other event name, `expiring_soon` included, is logged as
  unknown and acked. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_handle_unknown_event`)
- **WIRE-DISPATCH-5.** A failure of the health-tier update is logged and never fails the
  message. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`) \
  Pinned by: none

**Out of scope.** Health tiers (`operations-and-observability.md`); dynamic-instance
creation and teardown (`dynamic-resources.md`).

### 8.4 Ordered apply of fork changes

**What it does.** Every time a reservation's topology fork is saved, healed, or pruned,
execution brings the switches in line with it, applying fork versions in order and
falling back to a full comparison whenever it may have missed something.

**Surfaces.** `handle_wiring_changed` in
`services/execution/app/services/nats_consumer.py`; event
`herd.reservations.wiring_changed` (staged by reservations: RES-FORK-3, RES-FORK-8,
RES-FORK-13, RES-FORK-16); cabling `GET /internal/forks/{id}` (section 10). The wiring
state transitions are WIRE-ORDER-1 and WIRE-FREEZE-1.

**Rules.**

- **WIRE-ORDER-2.** An event missing `reservation_id` or `fork_version` is logged and
  acked with no work. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_missing_reservation_id_warns_and_returns_without_raising`, `test_missing_fork_version_warns_and_returns_without_raising`)
- **WIRE-ORDER-3.** On a frozen reservation the event is a no-op before any driver call
  and the version is not stamped; a version at or below the last applied one is a no-op. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_frozen_reservation_is_noop_zero_driver_calls`, `test_stale_version_is_noop`, `test_replay_after_success_is_noop`); `tests/integration/test_wiring_changed_reconcile.py` (`test_wiring_changed_frozen_after_complete_no_reconnect`, `test_wiring_changed_stale_replay_no_double_apply`)
- **WIRE-ORDER-4.** The L1 pass is a full reconcile when the event carries no delta
  (`released` or `built` is null), when no wiring state row exists, or when the version is
  not exactly one above the last applied; otherwise it applies the carried `released`
  and `built` hops. A delta-less heal at exactly the next version is a full reconcile. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_contiguous_delta_apply_builds_pair`, `test_gap_triggers_full_reconcile`, `test_missing_state_is_gap_then_stamped`, `test_heal_at_last_applied_plus_one_takes_full_reconcile`); `tests/integration/test_wiring_changed_reconcile.py` (`test_delta_less_heal_converges_after_initial_staging_failure`)
- **WIRE-ORDER-5.** An L1 full reconcile releases this reservation's ACTIVE pairs that are
  not in the fork's pairs and builds the fork's pairs that are not ACTIVE. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_gap_full_reconcile_releases_active_row_no_longer_desired`); `tests/integration/test_wiring_changed_reconcile.py` (`test_fork_save_release_drives_disconnect`)
- **WIRE-ORDER-6.** The fork is read from cabling on every apply. A 404 reads as an empty
  intended set, so the apply converges everything to released; any other non-200, a 5xx,
  or a transport error raises `TransientUpstreamError`, the message is nacked, and
  nothing is stamped. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_fork_intended_wires`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_fork_fetch_404_still_means_empty`, `test_fork_fetch_403_raises_transient`, `test_fork_fetch_422_raises_transient`, `test_upstream_error_on_fork_fetch_raises`, `test_fork_fetch_403_naks_heal_and_preserves_wiring`)
- **WIRE-ORDER-7.** Only connections whose `layer` is `L1` or absent are used as hops;
  the fork's `l3_routes` are grouped by switch id, and a missing `l3_routes` key means no
  routing intent. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_fork_intended_wires`, `ForkIntent`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_fetch_fork_intended_wires_200_filters_to_l1_and_defaults_layer`)
- **WIRE-ORDER-8.** The L2 and L3 passes always reconcile against the full intended set
  read in WIRE-ORDER-6, on delta, heal, and gap alike; only the L1 pass uses a carried
  delta. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`, `_reconcile_l2_memberships`, `_reconcile_l3_adjacency`) \
  Pinned by: `tests/integration/test_l2_reconcile.py` (`test_l2_membership_provisions_then_releases_on_fork_save`); `tests/integration/test_l3_intent_execution.py` (`test_fork_save_changing_one_route_drives_delta_and_pin_advances`)
- **WIRE-ORDER-9.** Within one apply the L1 pass runs first (releases before builds on
  each switch), then the L2 pass, then the L3 pass. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`, `_apply_wiring_pairs`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_move_a_wire_releases_before_builds`); `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_l3_pass_runs_after_l2_within_one_apply`)
- **WIRE-ORDER-10.** After the three passes the version is stamped, even when rows were
  left FAILED; a raise before that point leaves the version unstamped, so the redelivery
  repeats the whole apply. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_driver_failure_acks_and_does_not_raise`, `test_upstream_error_raises_for_nak`)
- **WIRE-ORDER-11.** A device, template, or config lookup in inventory that answers 5xx
  or fails in transport raises `TransientUpstreamError` (nack); any other non-200 reads as
  not found. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_device`, `_fetch_template`, `_fetch_latest_config`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_fetch_device_raises_on_5xx`, `test_fetch_device_returns_none_on_404`, `test_fetch_template_raises_on_5xx`, `test_fetch_device_raises_on_transport_error`)
- **WIRE-ORDER-12.** Within one event each device and each switch's latest config is
  fetched at most once. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_FetchContext`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_fetch_context_memoizes_device_and_config_fetches`)
- **WIRE-ORDER-13.** A device removed from an ACTIVE reservation reaches execution as a
  `wiring_changed` whose `released` holds cabling's pruned hops and whose `built` is
  empty (RES-FORK-16); it is applied like any other event, so the removed device's
  connections are released. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`) \
  Pinned by: `tests/integration/test_device_set_patch_wiring.py` (`test_patch_add_wires_nothing_until_fork_save_and_remove_releases`)

**Out of scope.** When reservations stages the event and the ledger that guarantees it
(`reservations.md`, RES-EVENT-2, RES-EVENT-3); what the fork contains (`topology.md`).

### 8.5 Pairing hops into cross-connects

**What it does.** A drawn connection that passes through matrix switches is stored as a
chain of cables. Execution walks each chain to work out which two ports every matrix
switch on it must connect, and refuses to guess when the chain is ambiguous.

**Surfaces.** `_wires_to_switch_pairs` and `_chain_walk_group` in
`services/execution/app/services/nats_consumer.py`.

**Rules.**

- **WIRE-PAIR-1.** Hops are grouped by `edge_key`; each group is walked from one end of
  its chain, and every interior `Layer 1 Switch` gets the pair (port it was entered on,
  port it was left on), with the hop's `physical_connection_id`. Two groups through the
  same switch give that switch two pairs. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_wires_to_switch_pairs`, `_chain_walk_group`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_multi_switch_chain_pairs_both_switches`, `test_two_edges_one_switch_keyed_pairs_correctly`, `test_two_edges_two_switches_both_pair`)
- **WIRE-PAIR-2.** All hops with a null `edge_key` form one group. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_wires_to_switch_pairs`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_two_edges_one_switch_null_edge_key_fails_safe`, `test_mixed_keyed_and_null_edge_keys`)
- **WIRE-PAIR-3.** A connected part of a group that has a device on more than two hops, or
  does not have exactly two ends, fails every hop in it with
  `WIRING_NOT_SIMPLE_CHAIN_REASON`; a hop from a device to itself fails the same way. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_chain_walk_group`, `_wires_to_switch_pairs`, `WIRING_NOT_SIMPLE_CHAIN_REASON`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_branch_is_not_a_simple_chain_fails_safe`, `test_two_edges_one_switch_null_edge_key_fails_safe`)
- **WIRE-PAIR-4.** A hop whose endpoint device inventory reports missing fails with
  `WIRING_UNRESOLVABLE_REASON`; recorded hops are applied as recorded and never
  re-routed. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_wires_to_switch_pairs`, `WIRING_UNRESOLVABLE_REASON`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_unresolvable_hop_lands_failed_with_pinned_reason`)
- **WIRE-PAIR-5.** A failed hop is recorded FAILED with attempts 0, keyed on the hop's own
  `device_a_id`, `port_a`, and `port_b`, with intended `RELEASED` when it came from the
  release side of a carried delta and `ACTIVE` otherwise. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_wiring_pairs`, `handle_wiring_changed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_unresolvable_hop_lands_failed_with_pinned_reason`)

**Out of scope.** How cabling resolves a drawn edge into hops and stamps `edge_key`
(`topology.md`).

### 8.6 Layer 1 cross-connects

**What it does.** For each matrix switch, execution logs in once, disconnects the port
pairs the reservation no longer wants, connects the new ones, and logs out, recording
each confirmed change.

**Surfaces.** `_apply_wiring_pairs` and `_apply_one_port_action` in
`services/execution/app/services/nats_consumer.py`; the ledger functions in
`services/execution/app/services/l1_assignment_service.py`.

**Rules.**

- **WIRE-L1-1.** Per switch: one `login`, then `disconnect_ports` for each release pair
  still believed live, then `connect_ports` for each build pair not already ACTIVE for
  this reservation, then one `logout`. A pair is believed live when its row is ACTIVE or
  FAILED intended `RELEASED`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_wiring_pairs`); `services/execution/app/services/l1_assignment_service.py` (`pair_needs_release`, `is_pair_active`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_move_a_wire_releases_before_builds`, `test_release_of_absent_pair_is_noop`); `services/execution/tests/test_l1_assignment_service.py` (`test_pair_needs_release_true_for_failed_intended_released`, `test_pair_needs_release_false_for_failed_intended_active`)
- **WIRE-L1-2.** Port pairs are stored sorted, so (a, b) and (b, a) are one pair. \
  Enforced in: `services/execution/app/services/l1_assignment_service.py` (`canonical_port_pair`) \
  Pinned by: `services/execution/tests/test_l1_assignment_service.py` (`test_canonical_port_pair_both_orders_collide`, `test_record_connect_reversed_order_is_idempotent`, `test_release_matches_reversed_port_order`)
- **WIRE-L1-3.** A switch inventory reports missing, a missing template, or a driver load
  that raises parks every build pair on it FAILED intended `ACTIVE` and every release pair
  still believed live FAILED intended `RELEASED`, attempts 0, with a reason starting
  `WIRING_UNRESOLVABLE_REASON`, and makes no driver call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_wiring_pairs`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_driver_load_raise_parks_pairs_failed_without_nak`)
- **WIRE-L1-4.** A failed `login` parks the switch's pairs the same way with reason
  `driver login failed: ...` and the login's attempts, and makes no port call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_wiring_pairs`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_login_failure_parks_pairs_failed_and_skips_port_ops`)
- **WIRE-L1-5.** A failed `logout` changes no pair's outcome. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_wiring_pairs`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_logout_failure_does_not_fail_the_pass`)
- **WIRE-L1-6.** One pair's failure lands its own FAILED row and the pass continues with
  the other pairs and switches. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_per_connection_failure_does_not_abort_siblings`)
- **WIRE-L1-7.** A confirmed connect records the pair ACTIVE (WIRE-LEDGER-2) and a
  confirmed disconnect releases it (WIRE-LEDGER-9); the driver receives `port_a` and
  `port_b`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`) \
  Pinned by: `tests/integration/test_l1_provisioning.py` (`test_l1_ports_connected_on_reservation_create`, `test_l1_two_port_distinct_edges_drive_two_distinct_connect_calls`, `test_l1_ports_disconnected_on_reservation_cancel`)

**Out of scope.** Matrix switch command syntax (the driver package, `DRIVERS.md`).

### 8.7 Layer 2 VLAN membership

**What it does.** When a reservation's device is cabled to a layer 2 switch, the switch
port joins a VLAN private to that reservation in the switch's fabric; when the cable
goes, the port leaves it.

**Surfaces.** `_derive_l2_memberships`, `_reconcile_l2_memberships`,
`_apply_l2_memberships`, and `_apply_one_vlan_action` in
`services/execution/app/services/nats_consumer.py`; the ledger functions in
`services/execution/app/services/l2_membership_service.py`.

**Rules.**

- **WIRE-L2-1.** A hop endpoint on a `Layer 2 Switch` whose other endpoint is not a
  `Layer 2 Switch` makes that (switch, port) a member; a hop between two layer 2 switches
  (a trunk) makes no member; a hop with a missing endpoint makes none. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_derive_l2_memberships`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_derive_dut_to_l2_joins_switch_port`, `test_derive_l2_to_l2_trunk_contributes_nothing`, `test_derive_through_l1_joins_only_l2_side`, `test_derive_dedups_shared_membership`)
- **WIRE-L2-2.** The reconcile adds the intended members that are not ACTIVE and removes
  the ACTIVE members that are not intended. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l2_memberships`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_joins_member_and_allocates_vlan`, `test_reconcile_move_a_port_moves_membership_same_vlan`); `tests/integration/test_l2_reconcile.py` (`test_l2_membership_provisions_then_releases_on_fork_save`, `test_l2_membership_moves_to_new_port_same_vlan`)
- **WIRE-L2-3.** Per switch: one `login`, `remove_from_vlan` (`port`, `vlan_id`) for each
  removal still believed live, `add_to_vlan` (`port`, `vlan_id`, `tag` `tagged`) for each
  addition not already ACTIVE, then one `logout`. A member is believed live when its row
  is ACTIVE or FAILED intended `RELEASED`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l2_memberships`, `_apply_one_vlan_action`); `services/execution/app/services/l2_membership_service.py` (`membership_needs_remove`, `is_membership_active`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_apply_l2_memberships_add_already_active_is_skipped_no_driver_call`, `test_apply_l2_memberships_remove_not_live_is_skipped_no_driver_call`); `services/execution/tests/test_l2_membership_service.py` (`test_membership_needs_remove_true_for_failed_release`, `test_membership_needs_remove_false_for_failed_build`)
- **WIRE-L2-4.** A removal drives the VLAN number of the allocation its row points at,
  ACTIVE or already released. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_vlan_ids_for`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_settles_stale_failed_join_through_remove`)
- **WIRE-L2-5.** A switch inventory reports missing, a missing template, a driver load
  that raises, or a failed `login` parks the switch's additions FAILED intended `ACTIVE`
  and its removals still believed live FAILED intended `RELEASED`, with no membership
  driver call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l2_memberships`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_apply_l2_memberships_switch_not_found_parks_adds_and_removes_failed`, `test_reconcile_template_not_found_parks_add_failed`, `test_reconcile_driver_load_raises_parks_add_failed_no_driver_call`, `test_reconcile_login_failure_parks_add_and_remove_failed_no_port_ops`)
- **WIRE-L2-6.** An addition whose fabric got no allocation is parked FAILED with the nil
  allocation and reason `recorded hop unresolvable: no VLAN allocation for fabric`, with
  no driver call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l2_memberships`, `_resolve_add_allocations`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_no_allocation_for_fabric_parks_add_failed_no_driver_call`)

**Out of scope.** VLAN numbers and definitions are section 8.8.

### 8.8 VLAN allocation and definition

**What it does.** Each reservation gets its own VLAN number in each fabric it uses, no
two live reservations whose switches can reach each other through cabling share a
number, and the VLAN is created on every
switch the traffic crosses and deleted when the last member leaves.

**Surfaces.** `services/execution/app/services/vlan_service.py`;
`_resolve_add_allocations`, `_refresh_allocation_scopes`,
`_define_pending_for_allocations`, `_run_vlan_definition_op`, and
`_release_orphaned_allocations` in `services/execution/app/services/nats_consumer.py`;
cabling `GET /fabric/internal`. The allocation transitions are WIRE-VLAN-1 to
WIRE-VLAN-4.

**Rules.**

- **WIRE-VLAN-5.** Only switches with an addition are allocated for, grouped by the
  fabric cabling reports for them now, one allocation per fabric. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_resolve_add_allocations`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_allocation_groups_switches_by_fabric`, `test_allocation_shares_one_vlan_within_a_fabric`); `tests/integration/test_vlan_assignment.py` (`test_vlan_ids_are_unique_within_same_fabric`)
- **WIRE-VLAN-6.** The fabric lookup fails closed (#1003): any non-200 answer, a
  transport error, or an unparseable body raises `TransientUpstreamError` with the text
  `cabling fabric lookup for device <id>: upstream <status>`, `...: transport error
  <ExceptionClass>`, or `...: unparseable answer`. Nothing is allocated and no stand-in
  fabric is used: the reconcile nacks the event (WIRE-ORDER-11), the retry tick leaves
  the rows FAILED, and the manual retry answers 503. A failed lookup is not memoized. \
  Enforced in: `services/execution/app/services/vlan_service.py` (`fetch_fabric_id`, `FabricResolver`); `services/execution/app/services/nats_consumer.py` (`_resolve_add_allocations`, `_refresh_allocation_scopes`) \
  Pinned by: `services/execution/tests/test_vlan_service_edges.py` (`test_fetch_fabric_id_non_200_raises_transient`, `test_fetch_fabric_id_transport_error_raises_transient`, `test_fetch_fabric_id_unparseable_answer_raises_transient`); `services/execution/tests/test_vlan_service.py` (`test_outage_during_allocation_raises_and_allocates_nothing`); `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_allocation_fails_closed_when_fabric_lookup_fails`); `services/execution/tests/test_wiring_retry_l2.py` (`test_nil_allocation_build_retry_fabric_outage_fails_closed`)
- **WIRE-VLAN-7.** The definition scope is every `Layer 2 Switch` on any intended hop,
  trunks included; on every reconcile it replaces `switch_device_ids` on each of the
  reservation's ACTIVE allocations with the scope switches whose current fabric id
  matches the current fabric id of one of the allocation's anchor switches or its
  stored `fabric_id` (so a cable change that re-keys the component does not empty the
  scope). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_derive_l2_definition_scope`, `_refresh_allocation_scopes`) \
  Pinned by: `services/execution/tests/test_nats_consumer_vlan_definitions.py` (`test_scope_membership_only_switch`, `test_scope_includes_trunk_transit_switches`, `test_scope_through_l1_hop_credits_only_l2_side`); `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_cable_change_keeps_one_allocation_and_scope_for_one_reservation`)
- **WIRE-VLAN-8.** Before any membership call, `create_vlan` runs on each scope switch
  not yet in `defined_switch_ids`, each in its own login, call, and logout session; a
  success adds the switch to `defined_switch_ids`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_define_pending_for_allocations`, `_run_vlan_definition_op`) \
  Pinned by: `services/execution/tests/test_nats_consumer_vlan_definitions.py` (`test_define_runs_before_first_add_per_switch`); `tests/integration/test_vlan_assignment.py` (`test_vlan_assigned_on_reservation_create_with_l2_switch`)
- **WIRE-VLAN-9.** A failed `create_vlan` parks that switch's additions FAILED intended
  `ACTIVE` with reason `create_vlan failed: ...`, keeps the allocation, and is attempted
  again on the next apply or retry. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l2_memberships`, `_define_pending_for_allocations`) \
  Pinned by: `services/execution/tests/test_nats_consumer_vlan_definitions.py` (`test_create_failure_parks_membership_build_and_keeps_allocation`, `test_create_failure_retry_defines_then_joins`)
- **WIRE-VLAN-10.** A reconcile with no membership change still refreshes the scope and
  defines newly added transit switches. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l2_memberships`) \
  Pinned by: `services/execution/tests/test_nats_consumer_vlan_definitions.py` (`test_scope_grows_with_no_membership_delta_defines_new_transit_switch`, `test_heal_after_converged_apply_is_a_no_op`)
- **WIRE-VLAN-11.** When an allocation is released, `delete_vlan` runs on each switch in
  its `defined_switch_ids`, unless another ACTIVE allocation now holds the same number in
  the same fabric, in which case none runs; a failed delete is logged, the allocation
  stays released, and only the switches whose delete succeeded leave
  `defined_switch_ids`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_release_orphaned_allocations`) \
  Pinned by: `services/execution/tests/test_nats_consumer_vlan_definitions.py` (`test_delete_on_last_free_per_switch`, `test_delete_skipped_when_vlan_reallocated_on_fabric`, `test_delete_failure_logs_and_continues`); `tests/integration/test_vlan_assignment.py` (`test_vlan_released_on_reservation_cancel`)

**Out of scope.** How cabling computes a fabric (`topology.md`).

### 8.9 Layer 3 routes

**What it does.** When a reservation's device is cabled to a layer 3 switch, the switch
gets the reservation's routes: the routes drawn on the topology when there are any, else
the switch's stored config routes. Execution records exactly what it installed and
removes exactly that when the switch is no longer needed.

**Surfaces.** `_derive_l3_adjacency`, `_reconcile_l3_adjacency`, `_gate_l3_drive_routes`,
`_apply_l3_adjacency`, and `_drive_l3_route` in
`services/execution/app/services/nats_consumer.py`; the pin functions in
`services/execution/app/services/route_service.py`; the shared validator
`services/common/herd_common/l3_validation.py`.

**Rules.**

- **WIRE-L3-1.** A switch is adjacent when a hop endpoint lands on a `Layer 3 Switch`
  whose other endpoint is not a `Layer 3 Switch`; on a hop between two layer 3 switches,
  a side becomes adjacent only when the fork carries routing intent for it. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_derive_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_derive_dut_to_l3_joins_switch`, `test_derive_l3_to_l3_trunk_contributes_nothing`, `test_derive_through_l1_credits_the_l3_side`, `test_derive_l3_to_l3_trunk_with_intent_on_one_end_only`, `test_derive_l3_to_l3_trunk_with_intent_on_both_ends_is_adjacent`)
- **WIRE-L3-2.** Per switch, the reconcile provisions adjacent switches without an ACTIVE
  pin, deprovisions ACTIVE pins no longer adjacent, and considers the rest for a route
  delta; deprovisions run first, then provisions, then deltas. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`, `_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_reconcile_provisions_pinned_routes_on_first_adjacency`, `test_reconcile_deprovisions_on_last_adjacency_lost`, `test_reconcile_multi_hop_shared_adjacency_keeps_routes_until_last_hop`); `tests/integration/test_l3_reconcile.py` (`test_l3_routes_provision_on_gained_adjacency_and_release_on_lost`, `test_l3_shared_adjacency_keeps_routes_until_last_hop_leaves`)
- **WIRE-L3-3.** A provision installs the fork's routing intent for the switch when there
  is any; otherwise the routes of this reservation's existing non-RELEASED pin (ACTIVE
  before FAILED) when that row records any; otherwise the `routes` of the switch's latest
  config version. A row that records no routes is a drive-gate refusal recorded before
  anything was applied, not an applied empty set, so it falls back to the config (ADR
  0014 amendment for issue #1004). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`); `services/execution/app/services/route_service.py` (`get_effective_pinned_routes`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_precedence_intent_beats_config_on_first_adjacency`, `test_precedence_no_intent_still_falls_back_to_config`, `test_precedence_decided_per_switch_in_one_reconcile`, `test_reconcile_reuses_pinned_set_not_edited_config_on_reprovision`, `test_refused_intent_then_removed_save_drives_the_configured_routes`, `test_refused_intent_then_fixed_save_drives_the_intent`, `test_failed_intent_provision_then_intent_removed_keeps_that_set_not_config`); `services/execution/tests/test_route_service.py` (`test_get_effective_pinned_routes_is_none_for_a_failed_row_with_no_routes`, `test_get_effective_pinned_routes_still_returns_a_failed_rows_routes`); `tests/integration/test_l3_intent_execution.py` (`test_reservation_with_intent_provisions_exactly_the_intent`)
- **WIRE-L3-4.** A switch for which that rule yields no routes is not provisioned and no
  pin is written; a FAILED row for it that records no routes is released with no driver
  call, unless a retry channel holds its drive claim. `_apply_l3_adjacency` drives and
  records nothing for a provision that carries no routes. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`, `_apply_l3_adjacency`); `services/execution/app/services/route_service.py` (`release_unapplied_route_pin`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_provision_skips_switch_whose_config_has_no_routes`, `test_refused_intent_removed_with_no_configured_routes_save_releases_the_row`, `test_apply_l3_adjacency_provision_with_no_routes_drives_and_records_nothing`); `services/execution/tests/test_route_service.py` (`test_release_unapplied_route_pin_releases_a_failed_row_with_no_routes`, `test_release_unapplied_route_pin_leaves_a_row_that_records_routes`, `test_release_unapplied_route_pin_leaves_an_active_row`, `test_release_unapplied_route_pin_leaves_a_claimed_row`, `test_release_unapplied_route_pin_matches_only_the_given_row_id`); `tests/integration/test_l3_route_provisioning.py` (`test_no_route_ops_when_l3_device_has_no_config`)
- **WIRE-L3-5.** A deprovision removes the pin's stored routes, never routes re-read from
  the config, and is skipped when the pin is not believed installed (ACTIVE, or FAILED
  intended `RELEASED`). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`); `services/execution/app/services/route_service.py` (`route_needs_remove`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_reconcile_deprovisions_on_last_adjacency_lost`); `tests/integration/test_l3_route_provisioning.py` (`test_route_removal_matches_provisioned_set_after_config_edit`)
- **WIRE-L3-6.** A switch that stays adjacent with no routing intent is not touched, also
  when its intent was just removed. By decision (ADR 0014 phase 3 amendment, addendum
  X4). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_intent_removed_keeps_the_applied_set`); `tests/integration/test_l3_intent_execution.py` (`test_save_removing_all_intent_leaves_the_applied_set`)
- **WIRE-L3-7.** A switch that stays adjacent with routing intent gets a delta by route
  identity: `remove_route` for pinned routes not in the intent, then `configure_route` for
  intent routes not pinned, in one login and logout; equal sets make no call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`, `_apply_l3_adjacency`, `_route_set_identity_keys`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_delta_add_only_configures_only_the_new_route`, `test_delta_remove_only_removes_only_the_dropped_route`, `test_delta_unchanged_intent_makes_no_driver_call`, `test_delta_call_order_removes_before_adds_one_login_logout`); `tests/integration/test_l3_intent_execution.py` (`test_fork_save_changing_one_route_drives_delta_and_pin_advances`)
- **WIRE-L3-8.** Route identity is the shared four-field key (destination, interface,
  next hop, virtual router) from `herd_common`, never raw dict equality. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_route_set_identity_keys`); `services/common/herd_common/l3_route_identity.py` (`route_identity_key`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_route_set_identity_uses_the_shared_herd_common_helper`)
- **WIRE-L3-9.** A switch's route calls run inside one login and logout, one execution
  run per route, identified by destination plus `interface|next_hop|virtual_router`, so
  routes differing only by next hop or virtual router are separate calls. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_drive_l3_route`, `_route_run_identity`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_provision_wraps_routes_in_one_login_logout`, `test_route_run_identity_packs_four_fields`, `test_route_run_identity_distinguishes_routes_that_differ_only_by_vrf`, `test_reconcile_ecmp_siblings_both_configured_not_collapsed`)
- **WIRE-L3-10.** A switch succeeds only when every route call succeeds. A clean provision
  pins ACTIVE (WIRE-LEDGER-2), a clean deprovision releases the pin (WIRE-LEDGER-9), and a
  clean delta stores the full new intent (WIRE-LEDGER-14). A failed provision records
  FAILED intended `ACTIVE` with the attempted routes plus any stored routes whose removal
  did not confirm (WIRE-L3-19); a failed deprovision records FAILED intended `RELEASED`
  keeping the pin; a failed delta goes through `record_route_reconcile_failed`
  (WIRE-LEDGER-15). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_reconcile_failed_provision_lands_failed_intended_active`, `test_delta_partial_failure_records_possibly_installed_routes_lands_failed`, `test_rebuild_whose_residue_removal_fails_keeps_the_residue_recorded`, `test_delta_mixed_removes_and_adds_in_one_reconcile`); `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_l3_teardown_driver_failure_keeps_pin_and_lands_failed_released`)
- **WIRE-L3-11.** A switch inventory reports missing, a missing template, a driver load
  that raises (with the reason WIRE-DRIVER-7 gives it), or a failed `login` records the
  switch FAILED in its direction (through `record_route_reconcile_failed` for a delta)
  with no route call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_apply_l3_adjacency_switch_not_found_parks_provision_failed`, `test_apply_l3_adjacency_template_not_found_parks_provision_failed`, `test_apply_l3_adjacency_driver_load_raises_parks_provision_failed`, `test_apply_l3_adjacency_deprovision_switch_not_found_parks_failed`, `test_reconcile_login_failure_parks_provision_failed_no_configure_call`)
- **WIRE-L3-12.** On a frozen reservation a delta drives only its removals and records
  through WIRE-LEDGER-14's frozen branch; a frozen delta with only additions is skipped
  with no driver call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_apply_l3_adjacency_frozen_reconcile_runs_removes_skips_adds`, `test_apply_l3_adjacency_frozen_reconcile_only_adds_is_noop`)
- **WIRE-L3-13.** Routing intent passes the drive gate before it is provisioned or used
  for a delta. A refused newly adjacent switch is recorded FAILED intended `ACTIVE` with
  the gate's reason, keeping the routes of an existing non-RELEASED row or, when there is
  none, storing an empty route list, which records that nothing is installed (WIRE-L3-3,
  WIRE-L3-4); a refused staying switch goes through `record_route_reconcile_failed`
  keeping its pin; neither makes a driver call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`, `_gate_l3_drive_routes`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_gate_missing_config_version_lands_unconfigured_failed_no_driver_call`, `test_gate_reconcile_failure_keeps_previous_pin_via_reconcile_failed_path`, `test_vrf_route_fails_switch_with_no_driver_call`)
- **WIRE-L3-14.** The gate refuses the whole switch with `l3_vrf_unsupported` when any
  route names a `virtual_router` and the switch's driver, loaded first, does not declare
  `supports_vrf`; a missing switch, a missing driver, or a broken package also refuses,
  while any other load failure (a failed package download) raises
  `TransientUpstreamError`, judged by `is_permanent_load_failure` (WIRE-DRIVER-7). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_gate_l3_drive_routes`, `_l3_driver_supports_vrf`); `services/execution/app/services/driver_loader.py` (`is_permanent_load_failure`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_vrf_route_fails_switch_with_no_driver_call`, `test_vrf_route_on_delta_fails_via_reconcile_failed_path`, `test_vrf_route_drives_when_the_driver_declares_supports_vrf`, `test_vrf_route_still_fails_when_the_driver_declares_only_dry_run`); `tests/integration/test_l3_intent_execution.py` (`test_vrf_route_on_a_non_declaring_driver_parks_l3_vrf_unsupported`)
- **WIRE-L3-15.** The gate re-validates only when some route's
  `validated_config_version_id` is missing or differs from the id of the switch's current
  latest config version; when every stamp is current the routes are trusted as saved. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_gate_l3_drive_routes`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_gate_missing_validation_stamp_revalidates_and_drives_clean_route`, `test_gate_stale_validation_stamp_revalidates_against_current_config`, `test_gate_current_validation_stamp_trusts_verbatim_no_content_check`, `test_gate_current_validation_stamp_skips_the_interface_check_too`)
- **WIRE-L3-16.** Re-validation refuses with `l3_switch_unconfigured` when the switch has
  no config version or no usable interface, and otherwise runs the shared per-route
  validator with the config's interfaces, virtual routers, and interface ports and the
  ports this fork's hops land on; the first failing route refuses the whole switch with
  its reason. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_gate_l3_drive_routes`, `_validate_route_at_drive_time`, `_wired_ports_by_device`); `services/common/herd_common/l3_validation.py` (`validate_one_route`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_gate_unknown_interface_among_intent_fails_whole_switch_no_driver_call`, `test_gate_route_on_an_unwired_interface_fails_the_switch`, `test_gate_route_on_a_logical_interface_drives_normally`, `test_gate_declared_port_is_what_the_interface_resolves_against`, `test_wired_ports_by_device_groups_both_endpoints_and_skips_malformed_rows`)
- **WIRE-L3-17.** A driver that declares `supports_vrf` receives `virtual_router` on every
  route call (null for a default-table route); any other driver never receives the
  keyword. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_drive_l3_route`, `_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_declaring_driver_receives_virtual_router_null_for_a_default_table_route`, `test_non_declaring_driver_is_never_handed_the_virtual_router_keyword`); `tests/integration/test_l3_intent_execution.py` (`test_vrf_route_reaches_a_declaring_driver_with_the_keyword`, `test_a_default_table_route_still_carries_no_vrf_to_a_non_declaring_driver`)
- **WIRE-L3-18.** A latest-config read that answers 5xx or fails in transport nacks the
  message; a 404 means the switch has no config. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_latest_config`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_config_fetch_5xx_raises_transient_for_nak`, `test_config_fetch_404_returns_none`)
- **WIRE-L3-19.** A provision of a switch that has a FAILED pin (a save rebuilding a
  switch whose provision or delta failed, or a build or release retry) first drives
  `remove_route` for the pin's stored routes that its own route set does not name, then
  `configure_route` for its set, in the same login and logout, so a rebuild never
  forgets a route an earlier failed pass may have left on the switch. A residue removal
  that fails leaves the switch FAILED and keeps that route stored next to the route set. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`); `services/execution/app/services/route_service.py` (`possibly_installed_routes`, `routes_not_in`, `union_routes`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_save_after_failed_delta_removes_the_routes_its_intent_drops`, `test_retry_after_failed_delta_removes_the_routes_its_intent_drops`, `test_rebuild_whose_residue_removal_fails_keeps_the_residue_recorded`); `services/execution/tests/test_route_service.py` (`test_possibly_installed_routes_reads_a_failed_row_of_either_direction`, `test_possibly_installed_routes_ignores_an_active_pin`, `test_union_routes_dedups_by_route_identity_keeping_first_occurrence`, `test_routes_not_in_compares_by_identity_not_raw_dict_equality`)

**Out of scope.** What makes routing intent valid at save time (`topology.md`); config
versions (`device-configuration.md`).

### 8.10 Driver calls and how success is judged

**What it does.** Every switch command is run in an isolated process, retried briefly if
it fails, and counted as done only when the driver's own answer says so.

**Surfaces.** `_run_sandbox` and `_run_driver_with_retry` in
`services/execution/app/services/nats_consumer.py`; `execute_driver_method` in
`services/execution/app/services/driver_sandbox.py`; `driver_result_failed` and
`build_context` in `services/execution/app/services/execution_service.py`.

**Rules.**

- **WIRE-DRIVER-1.** Each driver action runs in its own sandboxed child process with
  `EXECUTION_TIMEOUT_SECONDS`; a driver exception comes back as the error
  `driver raised <ClassName>`, never its message. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`, `_parse_driver_exception`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_execute_failing_driver`, `test_execute_timeout`, `test_execute_connect_ports_via_method_kwargs`)
- **WIRE-DRIVER-2.** A failed action is retried in line, three attempts in all, sleeping
  0.2 seconds then doubling to at most 2 seconds; the attempts made are added to the row
  on failure. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_run_driver_with_retry`, `WIRING_DRIVER_ATTEMPTS`, `WIRING_DRIVER_INITIAL_DELAY`, `WIRING_DRIVER_MAX_DELAY`) \
  Pinned by: none
- **WIRE-DRIVER-3.** An action fails when the sandbox reports failure, or when its output
  is a dict with a `success` key whose value is falsy; output with no `success` key
  succeeds. \
  Enforced in: `services/execution/app/services/execution_service.py` (`driver_result_failed`); `services/execution/app/services/nats_consumer.py` (`_run_driver_with_retry`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_present_key_falsy_result_is_failure`, `test_reconcile_bare_data_output_stays_success`); `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_reconcile_present_key_falsy_result_is_failure`, `test_reconcile_bare_data_output_stays_success`); `tests/integration/test_l2_l3_result_gating.py` (`test_l2_add_to_vlan_result_failure_records_failed`, `test_l3_configure_route_result_failure_records_failed`)
- **WIRE-DRIVER-4.** A driver failure never nacks the message: it lands a FAILED row and
  the message is acked. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`, `_apply_one_vlan_action`, `_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_driver_failure_acks_and_does_not_raise`); `tests/integration/test_l2_l3_result_gating.py` (`test_l3_remove_route_result_failure_keeps_pin_and_acks`)
- **WIRE-DRIVER-5.** Every login, logout, and change writes an execution run with the
  action, its ports or route identity, its keyword arguments, the reservation id, the
  context with password fields redacted, start and end times, and SUCCESS or FAILED; the
  acting user is the nil UUID. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`WIRING_SYSTEM_USER`, `_apply_one_port_action`); `services/execution/app/services/execution_service.py` (`redact_context_for_logging`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_port_action_runs_carry_timestamps`); `services/execution/tests/test_execution_service.py` (`test_redact_context`)
- **WIRE-DRIVER-6.** The driver's context carries every device `field_data` key prefixed
  `HERD_`, plus the device id, name, connection type, reservation id, and user id. \
  Enforced in: `services/execution/app/services/execution_service.py` (`build_context`) \
  Pinned by: `services/execution/tests/test_execution_service.py` (`test_build_context`)
- **WIRE-DRIVER-7.** A driver load that raises parks the switch's rows in their own
  direction with no driver call, classified by the kind of failure. A broken package
  (`DriverPackageError`) is permanent: the reason is `WIRING_UNRESOLVABLE_REASON` followed
  by `driver load failed: DriverPackageError`, with no attempt counted, and neither retry
  channel retries it (WIRE-RETRY-4). Any other load failure, a failed package download
  in particular, is transient: the reason is `driver load failed: <ClassName>` (the
  wrapped cause's class, for example `ConnectError`), with one attempt counted, and both
  retry channels drive the row again in its direction, builds and releases (a teardown's
  included) alike. A transient failure in the L2 `create_vlan` pre-pass parks the
  dependent joins under `create_vlan failed: driver load failed: <ClassName>`. The stored
  text never carries the exception's message, which goes only to the log. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_wiring_load_failure`, `_apply_wiring_pairs`, `_run_vlan_definition_op`, `_apply_l2_memberships`, `_apply_l3_adjacency`); `services/execution/app/services/driver_loader.py` (`is_permanent_load_failure`, `driver_load_failure_text`); `services/execution/app/services/wiring_retry_service.py` (`is_retryable_failure`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_driver_load_raise_parks_pairs_failed_without_nak`); `services/execution/tests/test_wiring_driver_load_classification.py` (`test_wiring_load_failure_reason_and_attempts_by_kind`, `test_l1_transient_download_failure_parks_build_and_release_retryable`, `test_l1_broken_package_parks_build_and_release_permanent`, `test_l2_transient_download_failure_parks_add_and_remove_retryable`, `test_l2_transient_failure_during_vlan_define_parks_the_add_retryable`, `test_l3_transient_download_failure_parks_provision_and_deprovision_retryable`, `test_l3_broken_package_parks_provision_and_deprovision_permanent`, `test_l1_transient_rows_converge_on_the_next_retry_tick`, `test_l2_transient_remove_converges_on_the_next_retry_tick`, `test_l3_transient_rows_converge_on_the_next_retry_tick`, `test_l1_broken_package_rows_are_not_retried_by_the_tick`, `test_teardown_l1_removal_with_transient_download_failure_stays_retryable`); `services/execution/tests/test_wiring_retry_service.py` (`test_is_retryable_classifies_pinned_reasons_not_retryable`)

**Out of scope.** Driver package loading, caching, and validation (`inventory.md`,
`device-configuration.md`); the driver method contract (`DRIVERS.md`).

### 8.11 Settling builds whose intent is gone

**What it does.** If a change failed and the user then removed it from the topology,
execution stops trying to build it and instead makes sure nothing of it is left on the
switch.

**Surfaces.** The full reconciles in `services/execution/app/services/nats_consumer.py`.

**Rules.**

- **WIRE-STALE-1.** An L1 full reconcile parks each FAILED intended-`ACTIVE` row whose
  pair is neither intended nor ACTIVE and whose reason is not a pinned one, and adds it to
  the release side; the build side still compares the intended set only with ACTIVE rows,
  so a failed pair that is still intended is built again. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_wiring_changed`, `NON_RETRYABLE_REASON_PREFIXES`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_full_reconcile_settles_stale_failed_build_through_release`, `test_full_reconcile_still_rebuilds_failed_build_that_is_still_intended`, `test_stale_pinned_reason_build_row_is_not_release_driven`)
- **WIRE-STALE-2.** The L2 reconcile does the same for memberships; a stale row with a
  nil or unknown allocation is released directly with no driver call, whatever its reason. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l2_memberships`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l2_reconcile.py` (`test_reconcile_settles_stale_failed_join_through_remove`, `test_reconcile_still_rebuilds_failed_join_that_is_still_intended`, `test_reconcile_stale_nil_allocation_join_flips_released_with_no_driver_call`)
- **WIRE-STALE-3.** The L3 reconcile does the same for pins of switches no longer
  adjacent, removing the row's stored routes. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_reconcile_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_reconcile_settles_stale_failed_provision_through_remove`, `test_reconcile_still_rebuilds_failed_provision_that_is_still_intended`)
- **WIRE-STALE-4.** A settlement whose removal fails leaves the row FAILED intended
  `RELEASED`, which the retry channels retry. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`) \
  Pinned by: `services/execution/tests/test_nats_consumer_wiring_changed.py` (`test_stale_build_release_failure_parks_failed_intended_released_retryable`)

**Out of scope.** None.

### 8.12 Teardown when a reservation ends

**What it does.** When a reservation is cancelled, completes, or fails, execution first
blocks any further building for it, then removes every cross-connect, VLAN membership,
and route set it recorded as installed.

**Surfaces.** `handle_reservation_event` and `_teardown_from_ledgers` in
`services/execution/app/services/nats_consumer.py`. The freeze transition is
WIRE-FREEZE-1.

**Rules.**

- **WIRE-TEARDOWN-1.** The freeze is committed before the first teardown driver call; a
  failed freeze raises, the message is nacked, and nothing is torn down. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`) \
  Pinned by: `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_freeze_lands_before_teardown_releases`, `test_frozen_is_durable_before_first_teardown_driver_call`, `test_freeze_failure_raises_and_blocks_teardown`)
- **WIRE-TEARDOWN-2.** Teardown removes exactly this reservation's ACTIVE rows: L1 pairs
  first, then L2 memberships (freeing and undefining their emptied allocations), then L3
  pins with their stored routes. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_from_ledgers`) \
  Pinned by: `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_terminal_event_releases_all_three_ledgers`, `test_partial_ledger_only_l2_tears_down_cleanly`, `test_partial_ledger_only_l3_pin_tears_down_cleanly`, `test_teardown_scoped_to_the_reservation`); `tests/integration/test_ledger_teardown.py` (`test_cancel_releases_all_three_ledgers`)
- **WIRE-TEARDOWN-3.** Teardown ignores the frozen flag, and empty ledgers make no driver
  call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_from_ledgers`) \
  Pinned by: `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_teardown_not_blocked_by_preexisting_frozen`, `test_empty_ledgers_noop_without_driver_calls`)
- **WIRE-TEARDOWN-4.** A teardown driver failure lands FAILED intended `RELEASED` (an L3
  pin keeps its routes) and the release-direction retry channels finish it after the
  reservation ended. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_from_ledgers`) \
  Pinned by: `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_l1_teardown_driver_failure_lands_failed_intended_released`, `test_failed_teardown_row_converges_on_manual_retry`); `tests/integration/test_ledger_teardown.py` (`test_cancel_teardown_failure_converges_on_terminal_retry`); `tests/integration/test_wiring_changed_reconcile.py` (`test_cancelled_disconnect_failure_direction_aware_retry`)
- **WIRE-TEARDOWN-5.** An upstream failure during teardown nacks the message, rows not
  yet released stay ACTIVE, and the redelivery releases only what is left. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_from_ledgers`) \
  Pinned by: `services/execution/tests/test_nats_consumer_ledger_teardown.py` (`test_teardown_upstream_error_naks_and_keeps_rows_active`, `test_redelivered_terminal_event_is_idempotent`); `tests/integration/test_failed_teardown.py` (`test_l3_failed_event_removes_pinned_routes_and_redelivery_is_idempotent`, `test_l1_failed_event_disconnects_only_applied_pairs`)
- **WIRE-TEARDOWN-6.** Teardown also removes what a FAILED intended-`ACTIVE` L3 pin
  records (a failed provision or delta, WIRE-L3-10, WIRE-LEDGER-15), since those routes
  may be installed. Before any driver call it parks each such pin `FAILED` intended
  `RELEASED` under `TEARDOWN_PENDING_REMOVAL`, then deprovisions its stored routes with
  the ACTIVE pins; a pin that stores no routes is released with no driver call. A failed
  removal stays release-direction, which the retry channels drive while frozen
  (WIRE-TEARDOWN-4). L1 and L2 FAILED intended-`ACTIVE` rows are not read: a failed L1
  connect or L2 join has nothing confirmed to undo. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_from_ledgers`); `services/execution/app/services/route_service.py` (`park_stale_route_build`, `TEARDOWN_PENDING_REMOVAL`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_teardown_removes_every_route_a_failed_delta_may_have_left`, `test_teardown_removal_failure_parks_release_direction_and_tick_finishes_it`, `test_teardown_parks_failed_build_with_the_teardown_reason_before_driving`, `test_teardown_releases_a_failed_build_with_no_routes_without_a_driver_call`); `tests/integration/test_l3_intent_execution.py` (`test_cancel_after_a_failed_route_change_removes_every_route_ever_configured`)

**Out of scope.** Dynamic-instance teardown, which runs after this (`dynamic-resources.md`);
the fork archive (`reservations.md`, RES-FORK-11).

### 8.13 Retrying failed wiring

**What it does.** A row that failed is retried automatically every minute until it
succeeds or reaches its attempt limit, and the owner can retry all failed rows of a
reservation at once from the Wiring tab. Each row is retried in the direction it was
going; a build whose intent has gone is never retried.

**Surfaces.** `reattempt_reservation`, `run_wiring_retry_tick`, and
`run_wiring_retry_loop` in `services/execution/app/services/wiring_retry_service.py`;
internal route `POST /internal/reservations/{reservation_id}/wiring/retry`; the user
interface in section 8.15.

**Rules.**

- **WIRE-RETRY-1.** A manual retry reads every FAILED row of the reservation in all three
  ledgers and ignores the attempts cap. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`reattempt_reservation`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_retry_flips_failed_build_to_active_on_success`, `test_retry_failed_reattempt_accumulates_attempts_and_updates_error`)
- **WIRE-RETRY-2.** On a frozen reservation with no release-direction FAILED row in any
  ledger, a manual retry raises `WiringReservationFrozen`, answered 409. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`reattempt_reservation`, `WiringReservationFrozen`); `services/execution/app/routers/executions.py` (`internal_wiring_retry`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_retry_frozen_reservation_refuses`, `test_retry_frozen_reservation_only_build_rows_still_raises`); `services/execution/tests/test_router_wiring_endpoints.py` (`test_wiring_retry_frozen_maps_to_409`); `services/execution/tests/test_wiring_retry_l2.py` (`test_frozen_pure_build_l2_raises`); `services/execution/tests/test_wiring_retry_l3.py` (`test_frozen_pure_build_l3_raises`)
- **WIRE-RETRY-3.** On a frozen reservation that has a release-direction FAILED row, each
  build-direction row is reported `frozen` with no driver call and the release rows are
  retried. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`reattempt_reservation`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_retry_frozen_reservation_processes_releases_reports_builds_frozen`); `services/execution/tests/test_wiring_retry_l2.py` (`test_frozen_processes_release_reports_build_frozen`); `services/execution/tests/test_wiring_retry_l3.py` (`test_frozen_processes_release_reports_build_frozen`)
- **WIRE-RETRY-4.** A row whose `last_error` starts with `WIRING_UNRESOLVABLE_REASON` or
  `WIRING_NOT_SIMPLE_CHAIN_REASON` is reported `not_retryable` with no driver call; a row
  with no error is retryable. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`is_retryable_failure`, `reattempt_reservation`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_is_retryable_classifies_driver_failures_retryable`, `test_is_retryable_classifies_pinned_reasons_not_retryable`, `test_retry_non_retryable_pinned_reason_makes_no_driver_call`); `services/execution/tests/test_wiring_retry_l2.py` (`test_retry_non_retryable_pinned_reason_makes_no_driver_call`)
- **WIRE-RETRY-5.** Each retryable row is retried in its own direction through the same
  apply functions as the reconcile, and its outcome is read back by row id: `superseded`
  (WIRE-LEDGER-16), `reconnected` (now ACTIVE), `released` (now RELEASED), `in_progress`
  (another channel held it), else `still_failed`; a row deleted before the read-back is
  left out. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`_reattempt_rows`, `_reattempt_l2_rows`, `_reattempt_l3_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_retry_mixed_directions_in_one_reservation`, `test_retry_release_direction_row_drives_disconnect_and_ends_released`, `test_retry_release_direction_repeat_failure_stays_failed_released`, `test_reattempt_rows_skips_id_deleted_before_refresh`); `services/execution/tests/test_wiring_retry_l2.py` (`test_build_direction_retry_drives_add_and_ends_active`, `test_release_direction_retry_drives_remove_and_frees_last_allocation`); `services/execution/tests/test_wiring_retry_l3.py` (`test_build_direction_retry_reconfigures_pinned_set_and_ends_active`, `test_release_direction_retry_removes_pinned_set_and_ends_released`)
- **WIRE-RETRY-6.** Before a build-direction row is driven, the fork's current intended
  set is read once per reservation and the row's key derived the reconcile's way; a row
  whose key is gone is parked stale (WIRE-LEDGER-13) and not driven, and an L2 row with a
  nil or unknown allocation is instead released directly. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`_reattempt_rows`, `_reattempt_l2_rows`, `_reattempt_l3_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_retry_build_intent_gone_parks_and_makes_no_build_call`, `test_retry_mixed_intent_builds_present_pair_and_parks_absent_pair`, `test_parked_stale_build_settles_through_release_on_a_later_pass`); `services/execution/tests/test_wiring_retry_l2.py` (`test_l2_build_intent_gone_parks_and_makes_no_driver_call`, `test_l2_nil_allocation_stale_build_flips_released_and_mints_no_allocation`); `services/execution/tests/test_wiring_retry_l3.py` (`test_l3_build_intent_gone_parks_and_makes_no_driver_call`)
- **WIRE-RETRY-7.** When that read fails, no build-direction row of that reservation is
  driven or changed in that pass and each reads back `still_failed`; release rows still
  run, and other reservations are unaffected. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`_reattempt_rows`, `_reattempt_l2_rows`, `_reattempt_l3_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_tick_fetch_failure_drives_nothing_for_that_reservation_only`); `services/execution/tests/test_wiring_retry_l2.py` (`test_l2_fetch_failure_blocks_builds_but_releases_still_run`); `services/execution/tests/test_wiring_retry_l3.py` (`test_l3_fetch_failure_leaves_build_row_untouched`)
- **WIRE-RETRY-8.** An L2 build row with a nil or unknown allocation is re-allocated
  before it is driven; when that fails again it is re-recorded by row id with one more
  attempt and the no-allocation reason, and not driven. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`_reattempt_l2_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_l2.py` (`test_nil_allocation_build_retry_reresolves_and_joins_real_vlan`, `test_nil_allocation_build_retry_resolution_failure_makes_no_driver_call`)
- **WIRE-RETRY-9.** An L3 build row is driven with the fork's current routing intent for
  the switch when there is any, after the drive gate; a gate refusal re-records the row
  by row id with one more attempt and the reason, with no driver call; a gate transport
  failure skips only that row. With no intent the row's stored routes are driven; a row
  that stores none is driven with the switch's configured routes, or released with no
  driver call when those are empty too (WIRE-L3-4), and a transport failure reading the
  configuration skips only that row. The
  stored routes the driven set drops are removed first (WIRE-L3-19), and a success
  replaces the stored routes with those driven (WIRE-LEDGER-2). \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`_reattempt_l3_rows`); `services/execution/app/services/nats_consumer.py` (`_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_nats_consumer_l3_reconcile.py` (`test_retry_after_failed_delta_removes_the_routes_its_intent_drops`, `test_refused_intent_then_removed_retry_drives_the_configured_routes`, `test_refused_intent_then_fixed_retry_drives_the_intent`, `test_failed_intent_provision_then_intent_removed_retry_keeps_that_set`, `test_refused_intent_removed_with_no_configured_routes_retry_releases_the_row`); `services/execution/tests/test_wiring_retry_l3.py` (`test_l3_build_retry_with_current_intent_drives_intent_not_stale_pin`, `test_l3_build_retry_without_intent_drives_row_routes_verbatim`, `test_l3_build_retry_with_intent_gate_failure_makes_no_driver_call`, `test_l3_build_retry_gate_transient_failure_isolated_per_row`, `test_l3_build_retry_trunk_skipped_switch_still_retried_when_intent_present`)
- **WIRE-RETRY-10.** An L2 release settled as superseded also frees its allocation when
  no ACTIVE membership remains, undefining the VLAN where it was defined. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`_reattempt_l2_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_l2.py` (`test_release_superseded_settles_without_driver_call`, `test_superseded_settlement_undefines_provably_defined_vlan`)
- **WIRE-RETRY-11.** A manual retry reports a row another channel holds as `in_progress`
  before any driver work, and classifies `not_retryable` and `frozen` before consulting
  claims. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`reattempt_reservation`); `services/execution/app/services/wiring_claim.py` (`claimable_row_ids`) \
  Pinned by: `services/execution/tests/test_wiring_claim.py` (`test_manual_retry_reports_in_progress_for_a_row_the_tick_holds`, `test_manual_retry_reports_in_progress_at_all_three_layers`, `test_manual_retry_still_reports_not_retryable_over_in_progress`)
- **WIRE-RETRY-12.** A `TransientUpstreamError` raised while a manual retry applies rows
  answers 503. \
  Enforced in: `services/execution/app/routers/executions.py` (`internal_wiring_retry`) \
  Pinned by: `services/execution/tests/test_router_wiring_endpoints.py` (`test_wiring_retry_upstream_maps_to_503`, `test_wiring_retry_relays_outcomes`)
- **WIRE-RETRY-13.** Each tick selects, per ledger, up to `WIRING_RETRY_BATCH_SIZE` (at
  least 1) unclaimed FAILED rows with attempts below `WIRING_RETRY_MAX_ATTEMPTS`, oldest
  first, skipping rows locked by another transaction. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`run_wiring_retry_tick`); `services/execution/app/services/l1_assignment_service.py` (`due_failed_rows`); `services/execution/app/services/l2_membership_service.py` (`due_failed_l2_rows`); `services/execution/app/services/route_service.py` (`due_failed_route_rows`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_tick_respects_batch_cap`, `test_tick_skips_rows_past_max_attempts`); `services/execution/tests/test_wiring_claim.py` (`test_l1_claimed_row_is_invisible_to_the_tick_select`, `test_l1_expired_claim_is_reclaimable`)
- **WIRE-RETRY-14.** The tick skips rows with a pinned reason (counted
  `skipped_not_retryable`) and build-direction rows of frozen reservations (counted
  `skipped_frozen`); release-direction rows are retried on a frozen reservation. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`run_wiring_retry_tick`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_tick_skips_frozen_reservation`, `test_tick_retries_frozen_release_and_skips_frozen_build`, `test_tick_skips_non_retryable_l2_and_l3_rows`, `test_tick_skips_frozen_l2_and_l3_build_rows`, `test_parked_post_freeze_build_converges_released_on_next_tick`)
- **WIRE-RETRY-15.** An exception from one layer's retry is logged, its rows stay FAILED,
  and the other layers still run. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`run_wiring_retry_tick`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_tick_l1_reattempt_exception_leaves_row_failed_and_does_not_raise`, `test_tick_l2_reattempt_exception_leaves_row_failed_and_does_not_raise`, `test_tick_l3_reattempt_exception_leaves_row_failed_and_does_not_raise`)
- **WIRE-RETRY-16.** The tick returns and logs (`wiring_retry_tick`, at its end) the
  totals `rows_due`, `rows_retried`, `reconnected`, `released`, `superseded`,
  `still_failed`, `in_progress`, the two skip counts, and per-layer retried counts. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`run_wiring_retry_tick`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_tick_reports_released_stat_for_release_direction_rows`, `test_tick_reports_superseded_stat`); `services/execution/tests/test_wiring_retry_l2.py` (`test_tick_labels_layers_and_counts_per_layer`); `services/execution/tests/test_wiring_retry_l3.py` (`test_tick_labels_layers_and_counts_l3`)
- **WIRE-RETRY-17.** The loop ticks every `WIRING_RETRY_INTERVAL_SECONDS` (at least 1); a
  tick that raises doubles the wait up to the larger of ten intervals and 300 seconds, a
  healthy tick resets it, and a cancellation ends the loop. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`run_wiring_retry_loop`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_loop_sleeps_base_interval_on_healthy_tick`, `test_loop_backs_off_on_tick_failure`, `test_loop_propagates_cancellation_raised_from_inside_the_tick`)
- **WIRE-RETRY-18.** The loop starts only when `WIRING_RETRY_ENABLED` is true,
  independent of `EXECUTION_POLLER_ONLY`, so it runs in every replica that enables it. \
  Enforced in: `services/execution/app/services/wiring_retry_service.py` (`start_wiring_retry_scheduler`); `services/execution/app/main.py` (`lifespan`) \
  Pinned by: `services/execution/tests/test_wiring_retry_service.py` (`test_scheduler_start_respects_enable_flag`)

**Out of scope.** Reservations' status gate on the proxy (`reservations.md`,
RES-FORK-18).

### 8.14 Keeping two retry channels off the same row

**What it does.** The automatic retry and the owner's Retry button never send the same
command to a switch at the same time: whichever reaches a row first holds it for a few
minutes, and the other skips it.

**Surfaces.** `services/execution/app/services/wiring_claim.py`; the claim calls in
`_apply_one_port_action`, `_apply_one_vlan_action`, and `_apply_l3_adjacency`. The stamp
transitions are WIRE-CLAIM-1 to WIRE-CLAIM-3.

**Rules.**

- **WIRE-CLAIM-4.** A retried row is claimed immediately before its own driver call (an
  L3 pin once, before its switch's login), never at selection; the reconcile path claims
  nothing. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`, `_apply_one_vlan_action`, `_apply_l3_adjacency`); `services/execution/app/services/wiring_claim.py` (`WiringRowClaims`) \
  Pinned by: `services/execution/tests/test_wiring_retry_claim_race_live_pg.py` (`test_a_row_selected_before_it_was_claimed_loses_the_drive_time_cas`); `services/execution/tests/test_wiring_claim.py` (`test_claim_of_none_row_id_is_always_granted`)
- **WIRE-CLAIM-5.** A row whose claim is lost gets no execution run, no driver call, and
  no ledger write in that pass. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_apply_one_port_action`, `_apply_one_vlan_action`, `_apply_l3_adjacency`) \
  Pinned by: `services/execution/tests/test_wiring_retry_claim_race_live_pg.py` (`test_l1_row_is_driven_exactly_once_across_both_channels`, `test_l2_row_is_driven_exactly_once_across_both_channels`, `test_l3_pin_is_driven_exactly_once_across_both_channels`, `test_the_manual_channel_reports_in_progress_when_the_tick_wins`)
- **WIRE-CLAIM-6.** A pass that reaches a row it already holds keeps its claim. \
  Enforced in: `services/execution/app/services/wiring_claim.py` (`WiringRowClaims`) \
  Pinned by: `services/execution/tests/test_wiring_claim.py` (`test_claim_is_idempotent_within_one_pass`)
- **WIRE-CLAIM-7.** Only the retry-only reads filter out claimed rows; the
  per-reservation FAILED reads, shared by the reconcile's stale settlement and the manual
  retry's report, return every FAILED row. \
  Enforced in: `services/execution/app/services/wiring_claim.py` (`unclaimed`, `claimable_row_ids`); `services/execution/app/services/l1_assignment_service.py` (`failed_assignments_for_reservation`) \
  Pinned by: `services/execution/tests/test_wiring_claim.py` (`test_claimable_row_ids_answers_only_the_free_rows`, `test_manual_retry_reports_in_progress_for_a_row_the_tick_holds`)

**Out of scope.** The L2 `create_vlan` pre-pass of a retry runs before any row claim (see
section 13).

### 8.15 Wiring status

**What it does.** The reservation's Wiring tab lists every cross-connect, VLAN
membership, and route set the reservation has had, with its state, the reason for any
failure, and a Retry button while the reservation is active.

**Surfaces.** User interface
`frontend/src/components/reservations/ReservationWiringTab.tsx` and
`frontend/src/api/reservations.ts`; internal route
`GET /internal/reservations/{reservation_id}/wiring-status`, reached through
reservations' proxy.

**Rules.**

- **WIRE-STATUS-1.** The status answer lists every row of the reservation in all three
  ledgers, every status, L1 then L2 then L3 and each oldest first, with its layer, status,
  intended, attempts, `last_error`, and `retryable` (FAILED without a pinned reason), plus
  `last_applied_fork_version` and `frozen`. \
  Enforced in: `services/execution/app/routers/executions.py` (`internal_wiring_status`, `WiringConnectionStatus`) \
  Pinned by: `services/execution/tests/test_router_wiring_endpoints.py` (`test_wiring_status_shape_includes_state_and_retryable`, `test_wiring_status_layered_l2_and_l3_rows`); `services/execution/tests/test_l1_assignment_service.py` (`test_all_assignments_returns_every_status_oldest_first`)
- **WIRE-STATUS-2.** An L2 row reports its allocation's VLAN number, or null when the
  allocation is the nil placeholder or unknown; an L3 row reports `route_count`, the
  length of its stored routes. \
  Enforced in: `services/execution/app/routers/executions.py` (`internal_wiring_status`) \
  Pinned by: `services/execution/tests/test_router_wiring_endpoints.py` (`test_wiring_status_l2_unresolvable_allocation_reports_null_vlan`, `test_wiring_status_l3_route_count_reflects_applied_set_after_a_delta`)
- **WIRE-STATUS-3.** A reservation with no rows and no state row answers an empty list,
  a null version, and `frozen` false. \
  Enforced in: `services/execution/app/routers/executions.py` (`internal_wiring_status`) \
  Pinned by: `services/execution/tests/test_router_wiring_endpoints.py` (`test_wiring_status_empty_case`)
- **WIRE-STATUS-4.** The tab groups rows into L1, L2, and L3 sections (a row without a
  layer counts as L1), shows the frozen marker and a release-pending marker on a FAILED
  row intended `RELEASED`, shows a re-save hint instead of Retry on a non-retryable row,
  and offers Retry only on an ACTIVE reservation with a retryable FAILED row. \
  Enforced in: `frontend/src/components/reservations/ReservationWiringTab.tsx` (`ReservationWiringTab`) \
  Pinned by: `frontend/src/test/components/ReservationWiringTab.test.tsx` (`groups layered rows into L1/L2/L3 sections with layer-specific detail`, `treats a row without a layer tag as L1 (pre-phase-8 backend tolerance)`, `shows the frozen marker when the wiring state is frozen`, `a FAILED row intended RELEASED shows the release-pending marker`, `a not_retryable FAILED row shows the re-save recovery hint and no retry button`, `ACTIVE reservation with a retryable FAILED row shows the Retry button and last_error`, `an ended reservation renders read-only (no retry button) even with a retryable FAILED row`)
- **WIRE-STATUS-5.** Both internal routes answer 403 to a missing or wrong
  `X-Internal-Token`. \
  Enforced in: `services/execution/app/routers/executions.py` (`_require_internal_token`) \
  Pinned by: `services/execution/tests/test_router_wiring_endpoints.py` (`test_wiring_status_requires_internal_token`, `test_wiring_retry_requires_internal_token`)
- **WIRE-STATUS-6.** Both internal routes answer 500 when `INTERNAL_API_TOKEN` is not
  configured. \
  Enforced in: `services/execution/app/routers/executions.py` (`_require_internal_token`) \
  Pinned by: none
- **WIRE-STATUS-7.** A replica with `EXECUTION_POLLER_ONLY=true` mounts neither internal
  route. \
  Enforced in: `services/execution/app/main.py` (`mount_api_routers`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_mount_api_routers_poller_only_mounts_nothing`)
- **WIRE-STATUS-8.** After a retry the tab shows a summary counting every outcome kind
  and refreshes the list; a failed retry shows the error detail. \
  Enforced in: `frontend/src/api/reservations.ts` (`summarizeWiringRetry`, `useRetryReservationWiring`) \
  Pinned by: `frontend/src/test/api/reservationWiring.test.tsx` (`summarizeWiringRetry tallies every outcome kind so the parts sum to results.length`, `useRetryReservationWiring toasts a success summary and invalidates the panel query`, `useRetryReservationWiring toasts the error detail on failure`); `tests/e2e/test_wiring_failed_row_playwright.py` (`test_wiring_tab_failed_row_and_retry`); `tests/e2e/test_wiring_layered_playwright.py` (`test_wiring_tab_layered_l2_membership_retry_and_release`)

**Out of scope.** Who may see the tab and the proxy's status gate (`reservations.md`,
RES-FORK-4, RES-FORK-18, RES-FORK-19).

## 9. Errors

Only the two internal routes return HTTP errors. The consumer returns nothing to a
caller: its outcomes are ack, nack with a delay, or dead-letter (section 8.1). FastAPI
validation errors (422) carry `detail` as a list.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 403 | `Invalid internal token` | missing or wrong `X-Internal-Token` on either internal route | WIRE-STATUS-5 |
| 409 | `Reservation wiring is frozen; retry is not allowed` | manual retry on a frozen reservation with no release-direction FAILED row | WIRE-RETRY-2 |
| 422 | validation list | `reservation_id` is not a UUID | WIRE-STATUS-1 |
| 500 | `Internal API token not configured` | `INTERNAL_API_TOKEN` empty in execution | WIRE-STATUS-6 |
| 503 | `Upstream service unavailable: <reason>` | an inventory or cabling call failed while a manual retry applied rows | WIRE-RETRY-12 |

Reservations relays these to the user as described in RES-FORK-18.

## 10. Interactions with other services

Events are in section 6; the internal routes this area serves are in section 7.

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out | reservations | `GET /internal/{id}` (internal token, 10 s) | corroborate an event's status | Fail closed: 5xx or transport nacks; 404 or another non-200 acks without acting (WIRE-GATE-2, WIRE-GATE-3) |
| Out | cabling | `GET /internal/forks/{id}` (internal token, 10 s) | the intended hops and routing intent | Fail closed: 404 is an empty set; any other non-200 or transport nacks the event (WIRE-ORDER-6), and a retry drives no build row of that reservation (WIRE-RETRY-7) |
| Out | cabling | `GET /fabric/internal?device_id` (internal token, 10 s) | the current fabric of an L2 switch, the reachability test for VLAN uniqueness | Fail closed: any non-200, transport error, or bad body nacks the event; in a retry the tick logs it and the manual route answers 503 (WIRE-VLAN-6) |
| Out | inventory | `GET /devices/{id}/internal`, `GET /templates/{id}/internal` (internal token, 10 s) | classify hop endpoints, load switch drivers | 5xx or transport nacks the event (WIRE-ORDER-11); in a retry the tick logs it and the manual route answers 503; another non-200 reads as missing (WIRE-PAIR-4, WIRE-L1-3) |
| Out | inventory | `GET /devices/{id}/config-versions/latest/internal` (internal token, 10 s) | L3 config routes and the drive gate | 5xx or transport nacks (WIRE-L3-18); 404 means no config (WIRE-L3-4, WIRE-L3-16) |
| Out | inventory | `GET /drivers/{id}/internal-download` (through the driver loader) | fetch a driver package on a cache miss | The switch's rows are parked FAILED and retryable by both retry channels; a broken package is parked non-retryable (WIRE-DRIVER-7); in the VRF capability check a failed download nacks instead (WIRE-L3-14) |
| Out | driver package | `login`, `connect_ports`, `disconnect_ports`, `create_vlan`, `delete_vlan`, `add_to_vlan`, `remove_from_vlan`, `configure_route`, `remove_route`, `logout` in the sandbox | change the switch | Three attempts in line, then a FAILED row; never nacks (WIRE-DRIVER-2, WIRE-DRIVER-4) |
| Out | NATS | `herd.reservations.dlq.execution` publish | dead-letter a message | Logged; the message is still acked (WIRE-CONSUME-12) |

## 11. Configuration

All are execution-service environment variables; [ENV_VARS.md](../ENV_VARS.md) has the
full list.

| Setting | Default | Effect |
|---|---|---|
| `NATS_ACK_WAIT_SECONDS` | `30` | The consumer's `ack_wait`; the heartbeat runs at half of it; values below 2 refuse to load |
| `NATS_NAK_BACKOFF_SECONDS` | `1,5,15,60,120` | Delay for the nth failed delivery (the dev and test override pins `0,1,1,1,1`) |
| `NATS_STREAM_MAX_AGE_SECONDS` | `604800` | Retention of the `HERD_DLQ` stream this service creates |
| `EXECUTION_TIMEOUT_SECONDS` | `30` | Timeout of each driver action, and the base of the claim budget |
| `WIRING_RETRY_ENABLED` | `true` | Run the background retry loop in this replica |
| `WIRING_RETRY_INTERVAL_SECONDS` | `60` | Seconds between retry ticks |
| `WIRING_RETRY_BATCH_SIZE` | `20` | Rows per ledger per tick |
| `WIRING_RETRY_MAX_ATTEMPTS` | `10` | Attempts after which the tick stops retrying a row (manual retry ignores it) |
| `EXECUTION_POLLER_ONLY` | `false` | When true, the internal routes are not mounted; the consumer and the retry loop still run |
| `INTERNAL_API_TOKEN` | empty | Token for the internal routes and every outgoing internal call; empty makes the internal routes answer 500 |

Fixed in code, not configurable: `max_deliver` 5; one message per fetch with a 5 second
wait; three in-line driver attempts with 0.2 s doubling to 2 s; 10 s for every
inventory, cabling, and reservations call; VLAN range 2 to 4094 with five allocation
attempts; the claim budget (WIRE-CLAIM-3).

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/execution/tests/` (in-memory SQLite): `test_nats_consumer*.py`, `test_l1_assignment_service.py`, `test_l2_membership_service.py`, `test_route_service.py`, `test_vlan_service*.py`, `test_wiring_retry*.py`, `test_wiring_claim.py`; `services/common/tests/test_jetstream.py`; `tests/unit/test_consumer_heartbeat_wiring.py`; `frontend/src/test/components/ReservationWiringTab.test.tsx`, `frontend/src/test/api/reservationWiring.test.tsx` | Driver calls are recorded fakes; `FOR UPDATE SKIP LOCKED` and row locks are no-ops on SQLite |
| Functional (through the service API) | `services/execution/tests/test_router_wiring_endpoints.py` (httpx against the app); `services/execution/tests/test_wiring_retry_claim_race_live_pg.py` against a real Postgres | The live suite runs in the `make master` and `make everything` gates |
| Integration (running stack) | `tests/integration/test_wiring_changed_reconcile.py`, `test_l1_provisioning.py`, `test_l2_reconcile.py`, `test_vlan_assignment.py`, `test_l3_reconcile.py`, `test_l3_route_provisioning.py`, `test_l3_intent_execution.py`, `test_l2_l3_result_gating.py`, `test_ledger_teardown.py`, `test_failed_teardown.py`, `test_event_verification_gate.py`, `test_dlq_and_idempotency.py`, `test_device_set_patch_wiring.py`, `test_nats_consumer_configs_live.py` | Drive the checked-in mock drivers; the NOS lab tiers (`tests/nos_lab/`) drive real FRR and SR Linux and are covered in `device-configuration.md` |
| Stress and load | None | No load test drives fork saves, wiring retries, or teardown; `tests/load/locustfile.py` covers reservations and topology validation only |
| Browser end-to-end | `tests/e2e/test_reservation_wiring.py`, `test_wiring_failed_row_playwright.py`, `test_wiring_layered_playwright.py`, `test_l3_routing_playwright.py` | Run nightly and in the gates, not per pull request |

Not run for this document: nothing was checked against a running stack. The
live-Postgres suite, the integration suite, and the browser suite were read, not run.
The in-line retry loop count (WIRE-DRIVER-2) is not asserted by any test.

## 13. Known limits and gaps

### Open defects

- #1083, WIRE-CONSUME-4: with NATS unreachable at boot the service waits in startup
  instead of starting without the consumer.

### Limits by decision

- An empty fork answer (cabling 404) converges a reservation's wiring to nothing
  (WIRE-ORDER-6), while every other unreadable answer defers. Recorded in the docstring
  of `_fetch_fork_intended_wires` and in issue #460.
- The fork version is stamped even when rows were left FAILED (WIRE-ORDER-10); the
  failed rows are the retry channels' work. Recorded in ADR 0007 (Decision 6).
- A pinned-reason row (unresolvable hop, not a simple chain) is never retried and never
  settled by the stale-intent pass (WIRE-RETRY-4, WIRE-STALE-1); recovery is a fork
  re-save. Recorded in ADR 0007 (Decision 5) and the comment on
  `NON_RETRYABLE_REASON_PREFIXES`.
- Only an event's status is corroborated, not its device ids (WIRE-GATE-5), and NATS
  itself has no authentication. Recorded in the comment above
  `_EVENT_CORROBORATION_RULES` and in [SECURITY.md](../../SECURITY.md).
- A switch that keeps adjacency but loses its routing intent keeps its installed routes
  (WIRE-L3-6). Recorded in the ADR 0014 phase 3 amendment (addendum X4).
- A failed `delete_vlan` leaves an empty VLAN defined on the switch while the allocation
  is released (WIRE-VLAN-11), and a switch that leaves the definition scope keeps its
  definition until last-free. Recorded in issue #442 and the docstrings of
  `_release_orphaned_allocations` and `_refresh_allocation_scopes`.
- The L2 `create_vlan` pre-pass runs before any per-row retry claim, so two channels may
  both define a VLAN; `create_vlan` must succeed on an already-defined VLAN. Recorded in
  [DRIVERS.md](../DRIVERS.md) and the docstring of `_define_pending_for_allocations`.
- An L3 route pin is per reservation and has no supersession settlement
  (WIRE-LEDGER-16). Recorded in the docstring of `_reattempt_l3_rows`.
- VLAN uniqueness is checked when an allocation is made (WIRE-VLAN-2). A cable added
  later that joins two components already holding the same number, or removed under a
  live allocation, is not re-checked; there is no reconcile. The release supersession
  guard (WIRE-VLAN-11) still compares stored fabric ids. Recorded in issue #1003 and the
  module docstring of `vlan_service.py`.

### Rules with no test

- WIRE-GATE-5: payload device ids are not cross-checked.
- WIRE-DISPATCH-5: a failed health-tier update does not fail the message.
- WIRE-DRIVER-2: the in-line retry makes three attempts with backoff (only the constant
  is asserted).
- WIRE-STATUS-6: the internal routes answer 500 with no token configured.
