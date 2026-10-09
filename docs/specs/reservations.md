# Reservations specification

| | |
|---|---|
| Area prefix | `RES` (used in rule identifiers, for example `RES-CREATE-1`) |
| Verified at | commit `fd589e50` (`v0.6.0-68-gfd589e50`), 2026-10-05 |
| Owning services | reservations (`services/reservations/`) |
| Other services involved | inventory (device and template data, device status), cabling (topology validation, the per-reservation fork), execution (consumes events, posts the provision result, owns wiring status), ai-orchestrator (purpose classification), auth (JWT only), notifications and integration (consume events) |
| Design records | [ADR 0001](../design/0001-editable-reservation-topologies.md), [ADR 0004](../design/0004-dynamic-resources.md), [ADR 0006](../design/0006-fork-reconcile-and-as-built.md), [ADR 0007](../design/0007-connection-driven-reconcile.md), [ADR 0009](../design/0009-l2-l3-connection-driven-reconcile.md), [ADR 0013](../design/0013-lab-purpose-classification.md) |
| Related guides | [USER_GUIDE.md](../USER_GUIDE.md) (Reservations), [ROLES.md](../ROLES.md), [ARCHITECTURE.md](../ARCHITECTURE.md) (Reservation state machine), [ENV_VARS.md](../ENV_VARS.md), [AI_PURPOSE_CLASSIFICATION.md](../AI_PURPOSE_CLASSIFICATION.md) |

All API paths below are the reservations service's own paths. Through the gateway they
are prefixed with `/api/reservations` (for example `POST /api/reservations/`).

## 1. Purpose

A reservation books lab equipment for a time window so two people never drive the same
exclusive device at once. The reservations service holds the booking, refuses
overlapping bookings of exclusive devices, moves each booking through its lifecycle
(scheduled, provisioning, active, finished), keeps inventory's device status in step
with who holds what, and announces every lifecycle change as an event that execution,
notifications, and integration act on. It does not wire hardware, create virtual
instances, or store topology content itself: it asks other services to, and records the
outcome.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). Ownership rules beyond role are
numbered rules in section 8; section 5 names the caller condition for each route.

| Actor | May | May not |
|---|---|---|
| User | Create reservations on devices they can see; list, read, edit, cancel, and release their own; set the purpose category on their own; read the calendar (filtered to reservations whose every device they can see); read and edit the fork of their own ACTIVE reservation | See, edit, cancel, or release another user's reservation; list all reservations; use the purpose review surface |
| Admin | Everything a user may, on devices without the visibility filter; list every reservation (`all=true`); cancel any reservation; set any reservation's purpose category; read and edit any reservation's fork and wiring through the API; review, accept, dismiss, backfill, and trigger purpose suggestions; read utilization reports | Read one other user's reservation by id, edit it, or release it (all answer 404; see RES-VIEW-2, RES-PATCH-2, RES-RELEASE-2) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Read a reservation's status, list reservations by device or topology, ask whether a user holds a device now, list a device's current holders, post a provision result (section 7) | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Reservation | One booking: owner, window `[start_time, end_time)`, status, purpose text, optional purpose category, optional parent topology | reservations | `reservations` table (`Reservation` in `services/reservations/app/models/reservation.py`) |
| Device membership | One device booked by one reservation. `device_id` is a bare inventory id, no foreign key | reservations | `reservation_devices` (`ReservationDevice`) |
| Dynamic request | One requested hypervisor-backed instance. Its `id` is the request id execution keys idempotent creation on; `template_id` is a bare inventory template id | reservations | `reservation_dynamic_requests` (`ReservationDynamicRequest`) |
| Topology type | `PHYSICAL` or `CLOUD`, derived from the booked devices | reservations (copied from inventory) | `reservations.topology_type` |
| Parent topology | The topology the booking was made from. `topology_id` is a bare cabling id, no foreign key | cabling | `reservations.topology_id` |
| Exclusive device | A device only one reservation may hold at a time. Read from inventory's `exclusive` flag on every call; a device whose record lacks the flag counts as exclusive | inventory | inventory |
| Device status | `AVAILABLE` or `RESERVED` (among others) on the inventory device; reservations writes it to mirror holds | inventory | inventory |
| Fork | The reservation's editable copy of its parent topology, keyed by the reservation id | cabling | cabling's fork tables |
| Fork wiring ledger | The last fork version for which this service staged a `reservation.wiring_changed` event | reservations | `fork_wiring_ledger` (`ForkWiringLedger`) |
| Outbox event | A lifecycle event written in the same transaction as the state change, published to NATS later | reservations | `outbox` (`OutboxEvent`) |
| Purpose suggestion | The AI orchestrator's classification of a finished reservation, stored verbatim and reviewed by an admin | reservations (produced by ai-orchestrator) | `reservations.purpose_suggestion` |
| Pending prune marker | Device ids removed from an ACTIVE reservation whose fork wiring release has not yet converged | reservations | `reservations.pending_fork_prune_device_ids` |
| Provision clock | When the row last entered `PENDING_PROVISION`, written in the same statement as each entry (the create path's insert, the sweep's claim) and never cleared; the two provisioning backstops measure their timeout from it. Null on rows that predate migration 0017, which fall back to `updated_at` | reservations | `reservations.provision_started_at` |

## 4. State model

**Statuses.**

- `PENDING`: scheduled for a future start; holds nothing in inventory (RES-HOLD-1).
- `PENDING_PROVISION`: provisioning in flight; holds its exclusive devices.
- `ACTIVE`: live; holds its exclusive devices.
- `COMPLETED`: ended at its end time or released early. Terminal, holds nothing.
- `CANCELLED`: cancelled by the owner or an admin. Terminal, holds nothing.
- `FAILED`: provisioning failed, timed out, or the window elapsed before activation.
  Terminal, holds nothing.

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | `PENDING` | `POST /` | `start_time` more than `RESERVATION_START_GRACE_SECONDS` ahead | nothing | RES-CREATE-12 |
| (none) | `ACTIVE` | `POST /` | starts now; no exclusive device; no dynamic request | `reservation.created` | RES-CREATE-13 |
| (none) | `PENDING_PROVISION` | `POST /` | starts now; at least one exclusive device or dynamic request | `reservation.provision_requested` when there is no exclusive device, else nothing | RES-CREATE-14, RES-DYN-4 |
| `PENDING_PROVISION` | `ACTIVE` | `POST /` after the inventory flip | no dynamic request | `reservation.created` | RES-CREATE-14 |
| `PENDING_PROVISION` | `PENDING_PROVISION` | `POST /` after the inventory flip; scheduled activation after the flip | dynamic requests present | `reservation.provision_requested` | RES-DYN-4, RES-DYN-5 |
| `PENDING_PROVISION` | `FAILED` | `POST /` | the inventory flip exhausted its attempts | `reservation.failed` | RES-CREATE-15 |
| `PENDING` | `PENDING_PROVISION` | expiration sweep (claim) | `start_time <= now < end_time` | nothing | RES-SWEEP-1, RES-STATUS-3 |
| `PENDING_PROVISION` | `ACTIVE` | scheduled activation after the inventory flip | no dynamic request | `reservation.created` | RES-SWEEP-1 |
| `PENDING_PROVISION` | `PENDING` | scheduled activation | the inventory flip exhausted its attempts | nothing | RES-SWEEP-2 |
| `PENDING` | `FAILED` | expiration sweep | `end_time <= now` | `reservation.failed` | RES-SWEEP-3 |
| `PENDING_PROVISION` | `ACTIVE` | `POST /internal/{id}/provision-result` | succeeded | `reservation.created` | RES-DYN-6, RES-DYN-7 |
| `PENDING_PROVISION` | `FAILED` | `POST /internal/{id}/provision-result` | failed | `reservation.failed` | RES-DYN-6, RES-DYN-8 |
| `PENDING_PROVISION` | `FAILED` | expiration sweep (dynamic timeout backstop) | dynamic requests; `provision_started_at` (else `updated_at`) older than `PROVISION_TIMEOUT_SECONDS` | `reservation.failed` | RES-SWEEP-6, RES-SWEEP-8 |
| `PENDING_PROVISION` | `PENDING` | expiration sweep (restart backstop) | no dynamic request; `provision_started_at` (else `updated_at`) older than `PROVISION_TIMEOUT_SECONDS` | nothing (the row's exclusive devices are released, holder-aware) | RES-SWEEP-7, RES-SWEEP-8 |
| `ACTIVE` | `COMPLETED` | expiration sweep (auto-complete) | `end_time <= now` | `reservation.completed` | RES-SWEEP-4 |
| `ACTIVE` | `COMPLETED` | `PUT /{id}/release` | owner only | `reservation.completed` | RES-RELEASE-3, RES-RELEASE-4 |
| `PENDING`, `PENDING_PROVISION`, `ACTIVE` | `CANCELLED` | `DELETE /{id}` | owner or admin | `reservation.cancelled` | RES-CANCEL-3, RES-CANCEL-4, RES-CANCEL-5 |

**Concurrency.** Every transition except the sweep's `PENDING` to `PENDING_PROVISION`
claim is a compare-and-swap through `_claim_status_transition` (RES-STATUS-1). The
sweep's claim relies on a row lock instead (RES-STATUS-3). Concurrent creates on the
same devices are serialized by advisory locks (RES-CONFLICT-4).

**Rules.**

- **RES-CREATE-12.** A booking whose start is more than the grace ahead skips the
  current-status check and is created `PENDING`, with no inventory write, no fork, and
  no event. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_future_is_pending_and_defers_provisioning`, `test_create_reservation_future_skips_now_availability_check`)
- **RES-CREATE-13.** A booking that starts now with no exclusive device and no dynamic
  request is created `ACTIVE` directly, with `reservation.created` in the same
  transaction and no inventory write. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_non_exclusive_skips_pending_provision`)
- **RES-CREATE-14.** Otherwise a booking that starts now is committed
  `PENDING_PROVISION` first, then its exclusive devices are set `RESERVED` with three
  attempts (0.5 s initial delay, doubling, 5 s cap), then it moves to `ACTIVE` with
  `reservation.created` in that transaction. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `_claim_provision_transition`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_exclusive_enqueues_created_event_in_txn`)
- **RES-CREATE-15.** When the inventory flip exhausts its attempts, the reservation
  moves to `FAILED` with `reservation.failed` and the purpose marker in that
  transaction, the devices that did reach `RESERVED` are set back to `AVAILABLE` through
  the holder check (RES-HOLD-3), and the caller gets 503. No fork archive is requested
  (the row never had a fork). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_fails_when_inventory_exhausts_retries`, `test_create_reservation_reverts_partially_reserved_devices_on_failure`); `tests/integration/test_provisioning_failed.py` (`test_provisioning_failure_lands_failed_and_reverts_devices`); `services/reservations/tests/test_purpose_classify_marker.py` (`test_create_flip_failure_stamps_marker`, `test_create_flip_failure_revert_skips_a_device_another_live_row_holds`)
- **RES-CREATE-16.** A create whose status compare-and-swap loses to a concurrent
  cancel during the flip stages no event, creates no fork, reverts the devices it
  flipped (holder-aware), and returns the row as the cancel left it. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_lost_activation_race`, `_revert_flipped_devices_best_effort`) \
  Pinned by: `services/reservations/tests/test_reservation_status_cas.py` (`test_create_loses_to_cancel_during_flip_stays_cancelled`, `test_lost_create_revert_skips_a_device_a_newer_booking_holds`); `services/reservations/tests/test_reservation_status_cas_live_pg.py` (`test_create_versus_cancel_mid_flip_never_leaves_a_zombie_active`)
- **RES-DYN-4.** A booking with dynamic requests that starts now never goes straight to
  `ACTIVE`: it stays `PENDING_PROVISION`, stages `reservation.provision_requested`
  (after the exclusive-device flip when there is one, else in the create transaction),
  and stages no `reservation.created` and creates no fork yet. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `_provision_requested_event`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_lands_pending_provision`, `test_dynamic_booking_stages_provision_requested_payload_exactly`, `test_dynamic_booking_no_created_event_no_fork`)
- **RES-DYN-5.** A scheduled booking with dynamic requests is handled the same way at
  activation: flip, then stage `reservation.provision_requested`, staying
  `PENDING_PROVISION`. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_activate_pending_reservation`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_scheduled_dynamic_claim_gates_activation`)
- **RES-DYN-6.** The provision-result callback applies only while the reservation is
  `PENDING_PROVISION`; any other status answers 200 with `applied: false` and changes
  nothing. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`apply_provision_result`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_callback_repeat_success_is_noop`, `test_callback_after_cancel_does_not_retransition`, `test_callback_after_failed_does_not_resurrect`)
- **RES-DYN-7.** A successful callback attaches the reported `device_ids` (skipping ones
  already booked), moves to `ACTIVE` with `reservation.created` in that transaction, then
  creates the fork best-effort. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`apply_provision_result`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_callback_success_activates_and_attaches_devices`, `test_dynamic_only_callback_success_attaches_instances`)
- **RES-DYN-8.** A failed callback moves to `FAILED` with `reservation.failed` and the
  purpose marker in that transaction, then releases exclusive devices (holder-aware) and
  archives the fork. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`apply_provision_result`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_callback_failure_fails_and_stages_failed_event`); `services/reservations/tests/test_fork_archive_reconcile.py` (`test_provision_result_failed_archives_fork`)
- **RES-DYN-9.** A callback and the timeout backstop racing for one row produce one
  winner; the loser stages nothing. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_claim_provision_transition`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_success_callback_loses_race_to_backstop_is_clean_noop`, `test_backstop_loses_race_to_success_callback_is_clean_noop`)
- **RES-SWEEP-1.** A `PENDING` row with `start_time <= now < end_time` is claimed to
  `PENDING_PROVISION` and then activated exactly as a start-now booking (flip, then
  `ACTIVE` with `reservation.created`, then fork). \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`, `_activate_pending_reservation`) \
  Pinned by: `services/reservations/tests/test_expiration.py` (`test_expiration_activates_pending`, `test_activation_emits_created_not_completed`); `services/reservations/tests/test_expiration_hold_invariant.py` (`test_pending_row_still_inside_its_window_is_still_activated`)
- **RES-SWEEP-2.** When the activation flip exhausts its three attempts, the row goes
  back to `PENDING` by compare-and-swap and all of its exclusive devices are released,
  holder-aware; a later tick retries. It is never failed for this. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_activate_pending_reservation`) \
  Pinned by: `services/reservations/tests/test_expiration.py` (`test_activation_inventory_failure_stays_pending`); `services/reservations/tests/test_expiration_hold_invariant.py` (`test_failed_successor_flip_releases_the_device_its_predecessor_skipped`, `test_failed_successor_flip_does_not_release_a_device_a_third_row_holds`)
- **RES-SWEEP-3.** A `PENDING` row whose `end_time` has passed is moved to `FAILED` by
  compare-and-swap with `reservation.failed` and the purpose marker, logging
  `reservation_window_elapsed`; it is never activated, and no inventory or fork call is
  made. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`) \
  Pinned by: `services/reservations/tests/test_expiration_hold_invariant.py` (`test_expired_pending_row_is_failed_not_activated`, `test_expired_pending_row_logs_fixed_action`)
- **RES-SWEEP-4.** An `ACTIVE` row with `end_time <= now` is moved to `COMPLETED` by
  compare-and-swap with `reservation.completed` and the purpose marker; a row a
  concurrent release or cancel already ended is skipped. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_complete_expired_rows`) \
  Pinned by: `services/reservations/tests/test_expiration.py` (`test_expiration_completes_expired_active`, `test_expiration_stages_one_completed_event_per_reservation`); `services/reservations/tests/test_expiration_status_cas.py` (`test_auto_complete_loses_to_a_concurrent_release_is_a_noop`)
- **RES-SWEEP-6.** A `PENDING_PROVISION` row with dynamic requests that entered
  `PENDING_PROVISION` more than `PROVISION_TIMEOUT_SECONDS` (default 900) ago is moved
  to `FAILED` by compare-and-swap with `reservation.failed` and the purpose marker, then
  its devices are released and its fork archived. Both provisioning backstops (this one
  and RES-SWEEP-7) measure the timeout from `provision_started_at`, which every
  transition into `PENDING_PROVISION` writes in the same statement (the create path's
  insert, the sweep's claim), so a later write to the row, for example a
  purpose-category PATCH, does not restart it. A row with no stamp (it predates
  migration 0017) is measured from `updated_at`. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`); `services/reservations/app/services/reservation_service.py` (`create_reservation`); `services/reservations/app/models/reservation.py` (`provision_started_at`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_timeout_backstop_fails_stuck_dynamic_reservation`, `test_timeout_backstop_leaves_fresh_dynamic_reservation`); `services/reservations/tests/test_fork_archive_reconcile.py` (`test_timeout_backstop_failed_archives_fork`); `services/reservations/tests/test_provision_clock.py` (`test_purpose_category_patch_does_not_move_the_backstop_deadline`, `test_backstop_reads_provision_started_at_not_updated_at`, `test_backstop_falls_back_to_updated_at_when_unstamped`, `test_create_stamps_provision_started_at_when_it_enters_pending_provision`, `test_create_leaves_provision_started_at_null_for_pending_and_active`, `test_sweep_claim_stamps_provision_started_at`, `test_every_entry_into_pending_provision_stamps_provision_started_at`, `test_cas_into_pending_provision_is_only_the_self_transition_guard`)
- **RES-SWEEP-7.** A physical-only `PENDING_PROVISION` row stranded past the same
  deadline (measured the same way) is moved back to `PENDING` by compare-and-swap, so a later tick re-activates
  it; nothing is torn down. Only the writer whose compare-and-swap won then releases
  the row's exclusive devices through the holder-aware filter (RES-HOLD-1: a `PENDING`
  row holds nothing, and a later cancel or elapsed-window failure writes no inventory
  status), after the commit and before the tick's claimed rows activate; a device
  another live row holds is skipped. A later claim re-flips the devices. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`); `services/reservations/app/services/reservation_service.py` (`_release_exclusive_devices_best_effort`, `release_devices_not_held_by_others`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_restart_backstop_reverts_stranded_physical_reservation`, `test_restart_backstop_reclaims_and_reactivates_across_cycles`, `test_restart_backstop_skips_row_activated_concurrently`); `services/reservations/tests/test_restart_backstop_release.py` (`test_restart_backstop_revert_releases_the_rows_devices`, `test_restart_backstop_revert_then_elapsed_window_releases_exactly_once`, `test_restart_backstop_revert_then_cancel_leaves_nothing_reserved`, `test_restart_backstop_release_skips_a_device_another_live_row_holds`, `test_restart_backstop_lost_cas_releases_nothing`)
- **RES-SWEEP-8.** `PROVISION_TIMEOUT_SECONDS=0` disables both provisioning backstops. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_timeout_backstop_disabled_when_zero`, `test_restart_backstop_disabled_when_timeout_zero`)
- **RES-CANCEL-3.** Cancel on `COMPLETED`, `CANCELLED`, or `FAILED` answers 204 and
  changes nothing: no event, no inventory write. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`cancel_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_cancel_failed_is_terminal_no_release_no_event`); `services/reservations/tests/test_reservations.py` (`test_cancel_already_cancelled`)
- **RES-CANCEL-4.** Cancel compare-and-swaps from the exact status it read; when the row
  moved, it re-reads and retries, at most three times, then logs
  `reservation_cancel_cas_exhausted` and stages nothing. A row that went terminal
  meanwhile is a clean no-op. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`cancel_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_status_cas.py` (`test_cancel_retries_with_the_fresh_status_when_the_row_moved_forward`, `test_cancel_cas_exhaustion_warns_with_fixed_action_and_stages_nothing`, `test_cancel_loses_to_a_concurrent_completion_is_a_noop`)
- **RES-CANCEL-5.** A winning cancel stages `reservation.cancelled` whose `user_id` is
  the owner (also on an admin cancel), stamps the purpose marker, then releases devices
  only if it left `PENDING_PROVISION` or `ACTIVE`, then requests a fork archive (a
  no-op for a `PENDING` row, which has no fork). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`cancel_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_admin_cancel_any_records_canceller`); `services/reservations/tests/test_purpose_classify_marker.py` (`test_cancel_reservation_stamps_marker`); `services/reservations/tests/test_fork_archive_reconcile.py` (`test_cancel_reservation_archives_fork`)
- **RES-RELEASE-3.** Release on any status other than `ACTIVE` answers 200 with the
  reservation unchanged. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`release_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_release_non_active_reservation`, `test_release_pending_reservation`)
- **RES-RELEASE-4.** A winning release compare-and-swaps `ACTIVE` to `COMPLETED`, stages
  `reservation.completed` and the purpose marker in that transaction, then releases
  devices and archives the fork; a release that loses to a cancel or auto-complete is a
  no-op. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`release_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_status_cas.py` (`test_release_win_completes_stages_one_event_and_releases`, `test_release_loses_to_a_concurrent_cancel_is_a_noop`); `services/reservations/tests/test_reservation_status_cas_live_pg.py` (`test_auto_complete_versus_release_stages_exactly_one_terminal_event`)
- **RES-STATUS-1.** A status write is a conditional `UPDATE ... WHERE id = :id AND
  status IN (:expected)` that matches only the statuses its caller expects to leave and
  reports whether it won; only the winner stages an event, writes inventory, or calls
  cabling, and the loser performs no side effect. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_claim_status_transition`, `_claim_provision_transition`) \
  Pinned by: `services/reservations/tests/test_reservation_status_cas.py` (`test_claim_status_transition_matches_only_expected_statuses`, `test_claim_provision_transition_is_the_pending_provision_wrapper`); `services/reservations/tests/test_dynamic_requests.py` (`test_claim_provision_transition_is_single_winner`)
- **RES-STATUS-2.** A scheduled activation that loses to a cancel during its flip
  stages nothing and reverts the devices it flipped. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_activate_pending_reservation`) \
  Pinned by: `services/reservations/tests/test_expiration_status_cas.py` (`test_scheduled_activation_loses_to_cancel_during_flip`)
- **RES-STATUS-3.** The sweep claims due `PENDING` rows with `SELECT ... FOR UPDATE SKIP
  LOCKED` and sets `PENDING_PROVISION` through the ORM, not through
  `_claim_status_transition`; safety against a concurrent cancel rests on the Postgres
  row lock. When the claim holds the row first, a concurrent cancel blocks on it, then
  re-reads and cancels from `PENDING_PROVISION` (releasing the devices) and the
  activation that follows does nothing; when the cancel holds it first, SKIP LOCKED
  leaves the row out. Either way the row ends `CANCELLED` with one
  `reservation.cancelled` and no `reservation.created`. On SQLite the lock is a no-op. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_due_pending_stmt`, `_claim_due_pending_rows`) \
  Pinned by: `services/reservations/tests/test_sweep_claim_cancel_race_live_pg.py` (`test_claim_holds_the_row_first_cancel_blocks_then_cancels_from_provision`, `test_cancel_holds_the_row_first_claim_skips_it`)

## 5. API surface

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| POST | `/` | any signed-in user; a non-admin only on devices and dynamic templates visible to them | 201 | RES-CREATE-1 to RES-CREATE-18, RES-CONFLICT-1 to RES-CONFLICT-5, RES-TOPO-1 to RES-TOPO-6, RES-DYN-1 to RES-DYN-4, RES-DYN-10, RES-PURPOSE-1 |
| GET | `/` | any signed-in user (own rows); admin with `all=true` (every row) | 200 | RES-LIST-1 to RES-LIST-9 |
| GET | `/calendar` | any signed-in user | 200 | RES-CAL-1, RES-CAL-2, RES-CAL-3 |
| GET | `/purpose-categories` | any signed-in user | 200 | RES-PURPOSE-1 |
| GET | `/{id}` | owner only, every role | 200 | RES-VIEW-1, RES-VIEW-2 |
| PATCH | `/{id}` | owner only, every role | 200 | RES-PATCH-1 to RES-PATCH-13 |
| DELETE | `/{id}` | owner, or any admin | 204 | RES-CANCEL-1 to RES-CANCEL-5 |
| PUT | `/{id}/release` | owner only, every role | 200 | RES-RELEASE-1 to RES-RELEASE-4 |
| PATCH | `/{id}/purpose-category` | owner, or any admin | 200 | RES-PURPOSE-1, RES-PURPOSE-2, RES-PURPOSE-3 |
| GET | `/{id}/fork` | owner, or any admin | 200 | RES-FORK-4, RES-FORK-5, RES-FORK-10 |
| PUT | `/{id}/fork/canvas` | owner, or any admin; ACTIVE only | 200 | RES-FORK-4, RES-FORK-6, RES-FORK-10 |
| POST | `/{id}/fork/save` | owner, or any admin; ACTIVE only | 200 | RES-FORK-4, RES-FORK-6, RES-FORK-7, RES-FORK-8, RES-FORK-10 |
| GET | `/{id}/fork/versions/{vid}` | owner, or any admin | 200 | RES-FORK-4, RES-FORK-10 |
| POST | `/{id}/fork/versions/{vid}/restore` | owner, or any admin; ACTIVE only | 200 | RES-FORK-4, RES-FORK-6, RES-FORK-9, RES-FORK-10 |
| GET | `/{id}/wiring-status` | owner, or any admin | 200 | RES-FORK-4, RES-FORK-18 |
| POST | `/{id}/wiring/retry` | owner, or any admin; not `PENDING` or `PENDING_PROVISION` | 200 | RES-FORK-4, RES-FORK-18 |
| GET | `/admin/purpose-review` | admin | 200 | RES-PURPOSE-9, RES-PURPOSE-14 |
| POST | `/admin/purpose-review/{id}/accept` | admin | 200 | RES-PURPOSE-10, RES-PURPOSE-14 |
| POST | `/admin/purpose-review/{id}/dismiss` | admin | 200 | RES-PURPOSE-11, RES-PURPOSE-14 |
| POST | `/admin/purpose-review/{id}/classify` | admin | 200 | RES-PURPOSE-13, RES-PURPOSE-14 |
| POST | `/admin/purpose/backfill` | admin | 200 | RES-PURPOSE-12, RES-PURPOSE-14 |
| GET | `/reports/utilization`, `/reports/utilization.csv` | admin | 200 | none here; belongs to `operations-and-observability.md` |

The fork and wiring routes answer with the body cabling or execution returned. The
`GET /` query parameters are `skip` (default 0), `limit` (1 to 500, default 50),
`all`, `sort_by`, `sort_dir`, `search`, `status` (repeatable), `purpose_category`,
`starts_after`, `starts_before`, `ends_after`, and `ends_before`; it answers
`{items, total, skip, limit}`. `GET /calendar` takes `range_start`, `range_end`, and
optionally `status` (repeatable) and `device_id`.

## 6. Events

Every event goes to the NATS JetStream stream `HERD_RESERVATIONS` through the outbox
(RES-EVENT-1); the producer is always the reservations service.

| Subject | Producer | Staged when | Consumers | Payload keys | Rules |
|---|---|---|---|---|---|
| `herd.reservations.created` | reservations | a row lands `ACTIVE` (create, scheduled activation, provision-result success) | execution, notifications, integration | `event`, `reservation_id`, `user_id`, `device_ids`, `topology_id`, `topology_type`, `start_time`, `end_time`, `purpose_category` | RES-CREATE-13, RES-CREATE-14, RES-SWEEP-1, RES-DYN-7 |
| `herd.reservations.provision_requested` | reservations | a dynamic-carrying row is ready for its instances | execution | `event`, `reservation_id`, `user_id`, `device_ids`, `topology_id`, `topology_type`, `dynamic_requests` (`id`, `template_id`), `purpose_category` | RES-DYN-4, RES-DYN-5 |
| `herd.reservations.failed` | reservations | any transition into `FAILED` | execution, notifications (since #1077), integration | `event`, `reservation_id`, `user_id`, `device_ids`, `topology_id`, `topology_type`, `purpose_category` | RES-CREATE-15, RES-DYN-8, RES-SWEEP-3, RES-SWEEP-6 |
| `herd.reservations.cancelled` | reservations | a winning cancel | execution, notifications, integration | as `failed`; `user_id` is the owner | RES-CANCEL-5 |
| `herd.reservations.completed` | reservations | a winning release or auto-complete | execution, notifications, integration | as `failed` | RES-RELEASE-4, RES-SWEEP-4 |
| `herd.reservations.updated` | reservations | a successful PATCH | execution, notifications, integration | `event`, `reservation_id`, `user_id`, `device_ids`, `added_device_ids`, `removed_device_ids`, `end_time_changed`, `end_time`, `purpose_category` | RES-PATCH-13 |
| `herd.reservations.expiring_soon` | reservations | the reminder cycle | notifications, integration | `event`, `reservation_id`, `user_id`, `device_ids`, `end_time`, `purpose_category` | RES-SWEEP-9 |
| `herd.reservations.wiring_changed` | reservations | fork save, activation after fork create, sweep heal, removed-device prune | execution | `event`, `reservation_id`, `fork_version`, `released`, `built` (both null on a heal) | RES-FORK-3, RES-FORK-8, RES-FORK-13, RES-FORK-16, RES-EVENT-2, RES-EVENT-3 |

Integration delivers an event only to webhooks subscribed to its name, and only the six
names `created`, `updated`, `cancelled`, `completed`, `failed`, and `expiring_soon` can
be subscribed. What each consumer does with an event is specified in that consumer's
area.

## 7. Internal API

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| GET | `/internal/{id}` | `X-Internal-Token` | execution (event corroboration), inventory (apply scheduler), ai-orchestrator (idle-conversation sweeper) | `{id, status, is_active, start_time, end_time, purpose_classification_pending}` | RES-INTERNAL-1, RES-INTERNAL-2, RES-INTERNAL-6, RES-INTERNAL-7 |
| GET | `/internal/active?user_id&device_id` | `X-Internal-Token` | inventory (reservation-owner widening) | `{owns_active}` | RES-INTERNAL-1, RES-INTERNAL-3 |
| GET | `/internal/active-users?device_id` | `X-Internal-Token` | notifications (health fan-out) | list of user ids | RES-INTERNAL-1, RES-INTERNAL-4 |
| GET | `/internal/held-devices?user_id` | `X-Internal-Token` | inventory (instance-device visibility grant) | `{device_ids}` | RES-INTERNAL-1, RES-INTERNAL-8 |
| GET | `/internal/by-topology/{topology_id}` | `X-Internal-Token` | cabling (topology edit lock and delete guard) | list of `{id, user_id, topology_id, status, end_time}` | RES-INTERNAL-1, RES-INTERNAL-5 |
| GET | `/internal/by-device/{device_id}` | `X-Internal-Token` | inventory (device delete guard, config restore, a schedule's `reservation_id`), execution (`POST /execute`'s `reservation_id`) | list of `{id, user_id, device_id, status, end_time}` | RES-INTERNAL-1, RES-INTERNAL-5 |
| POST | `/internal/{id}/provision-result` | `X-Internal-Token` | execution | `{reservation_id, status, applied}` | RES-INTERNAL-1, RES-DYN-6, RES-DYN-7, RES-DYN-8 |

## 8. Features

### 8.1 Create a reservation

**What it does.** A user picks devices they can see, a time window, and optionally a
purpose, a purpose category, a parent topology, and dynamic instances. HERD books it
now (it goes live within seconds) or, when the start is in the future, holds the window
and starts it at the start time.

**Surfaces.** User interface `frontend/src/components/reservations/CreateReservationModal.tsx`;
route `POST /` (body `ReservationCreate` in `services/reservations/app/schemas/reservation.py`).
The status a new booking starts in is RES-CREATE-12 to RES-CREATE-16 (section 4).

**Rules.**

- **RES-CREATE-1.** `device_ids` holds at most 200 entries; duplicates are dropped
  before anything else runs. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`ReservationCreate`, `_dedupe_preserve_order`) \
  Pinned by: `services/reservations/tests/test_schema_bounds.py` (`test_device_ids_over_cap_rejected`); `services/reservations/tests/test_reservations.py` (`test_create_reservation_duplicate_device_ids`)
- **RES-CREATE-2.** A booking must name at least one device or one dynamic request. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`require_device_or_dynamic`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_neither_device_nor_dynamic_request_422_wording`)
- **RES-CREATE-3.** `end_time` must be strictly after `start_time`. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`end_after_start`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_end_equals_start`)
- **RES-CREATE-4.** A `start_time` more than `RESERVATION_START_GRACE_SECONDS` (default
  300) before now is refused. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`validate_window`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_rejects_past_start`)
- **RES-CREATE-5.** A window longer than `RESERVATION_MAX_DURATION_SECONDS` (default 30
  days) is refused; 0 disables the cap. A window of exactly the cap passes. PATCH applies
  the same check (RES-PATCH-5). \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`validate_window`, `check_max_duration`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_rejects_overlong_duration`)
- **RES-CREATE-6.** `purpose` is at most 2000 characters. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`ReservationCreate`) \
  Pinned by: `services/reservations/tests/test_schema_bounds.py` (`test_purpose_over_cap_rejected`)
- **RES-CREATE-7.** A non-admin may book only devices inventory reports visible to them;
  any other device refuses the whole booking with 403. Admins skip the check. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`create_new_reservation`, `_fetch_visible_device_ids`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_non_admin_invisible_device_rejected`, `test_create_reservation_admin_skips_visibility`)
- **RES-CREATE-8.** When inventory's visible-devices lookup errors, answers non-200, or
  answers a misshapen body, the router skips the RES-CREATE-7 pre-check (by decision,
  docstring of `_fetch_visible_device_ids`), but the booking still cannot include an invisible
  device: the device fetch of RES-CREATE-9 goes through inventory's
  `POST /devices/batch`, which omits every device outside a non-admin's visibility and
  fails closed, so such a device is refused as not found (422). \
  Enforced in: `services/reservations/app/routers/reservations.py` (`_fetch_visible_device_ids`); `services/reservations/app/services/reservation_service.py` (`_fetch_devices`); `services/inventory/app/routers/devices.py` (`get_devices_batch`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_visibility_fetch_failure_allows`, `test_create_visibility_precheck_still_skips_on_a_misshapen_answer`); `services/reservations/tests/test_coverage_gaps.py` (`test_visible_devices_non_200_logs_and_fails_open`); `services/reservations/tests/test_reservation_service_unit.py` (`test_fetch_devices_missing_from_batch_raises_value_error`); `services/inventory/tests/test_devices.py` (`test_batch_non_admin_gets_only_visible_with_passwords_redacted`)
- **RES-CREATE-9.** Every requested device must exist in inventory, read in one batch
  call with the caller's JWT; a missing device refuses the booking with 422, and an
  unreachable inventory fails closed with 503. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_fetch_devices`, `create_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_fetch_devices_missing_from_batch_raises_value_error`); `services/reservations/tests/test_reservations.py` (`test_inventory_unreachable_returns_503`)
- **RES-CREATE-10.** All booked devices must share one topology type (physical and
  cloud never mix); a dynamic-only booking is `CLOUD`. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_mixed_topology_type_rejected`); `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_only_booking_lands_pending_provision_cloud`)
- **RES-CREATE-11.** A booking that starts now (within the grace) requires every
  exclusive device to be `AVAILABLE` and every non-exclusive device to be `AVAILABLE` or
  `RESERVED` at request time. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_device_not_available`); `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_non_exclusive_reserved_ok`)
- **RES-CREATE-17.** Once a create lands `ACTIVE`, the fork is created best-effort
  (RES-FORK-1); its failure never changes the response. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `_create_reservation_fork_best_effort`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_invokes_fork_when_topology_present`, `test_fork_best_effort_swallows_exhausted_retries`)
- **RES-CREATE-18.** `owner_name` is copied from the JWT `username` claim at creation
  and never refreshed. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`create_new_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_owner_name_in_list`)

**Out of scope.** The AI-generated "commit a proposal" flow that creates a topology and
then a reservation lives in ai-orchestrator (`ai-features.md`). The create modal's live
purpose suggestion is an ai-orchestrator call (`ai-features.md`).

### 8.2 Conflict detection

**What it does.** Two reservations can never hold the same exclusive device over
overlapping time. Shared (non-exclusive) devices can be booked by anyone at any time.

**Surfaces.** Routes `POST /` and `PATCH /{id}`.

**Rules.**

- **RES-CONFLICT-1.** A requested exclusive device conflicts with any other reservation
  holding it in `PENDING`, `PENDING_PROVISION`, or `ACTIVE` whose window overlaps the
  requested one; the booking is refused with 409. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_check_conflicts`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_conflict_detection`); `services/reservations/tests/test_reservation_service_unit.py` (`test_check_conflicts_pending_provision_visible`, `test_check_conflicts_cancelled_not_conflicting`)
- **RES-CONFLICT-2.** Windows are half-open: a reservation ending at T and one starting
  at T do not conflict. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_check_conflicts`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_adjacent_reservations_allowed`, `test_create_reservation_exact_time_overlap`)
- **RES-CONFLICT-3.** Non-exclusive devices are never conflict-checked. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_check_conflicts`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_non_exclusive_device_no_conflict`, `test_mixed_exclusive_non_exclusive`)
- **RES-CONFLICT-4.** Before the conflict check, a create takes one transaction-scoped
  Postgres advisory lock per requested device, in sorted id order, so concurrent creates
  on overlapping device sets serialize; the new row commits before the locks release.
  On SQLite the lock is a no-op. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_acquire_device_locks`) \
  Pinned by: `services/reservations/tests/test_coverage_gaps.py` (`test_acquire_device_locks_postgres_issues_advisory_locks`); `services/reservations/tests/test_reservation_service_unit.py` (`test_acquire_device_locks_sqlite_noop`)
- **RES-CONFLICT-5.** A committed `PENDING_PROVISION` row blocks a second create for the
  same exclusive device while the first is still flipping inventory. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `_check_conflicts`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_pending_provision_blocks_concurrent_create`)

**Out of scope.** Port-level conflicts between forks are cabling's (`topology.md`,
`provisioning-and-wiring.md`).

### 8.3 Topology-backed creation

**What it does.** A reservation made from a topology is checked against the physical
cabling before it is booked: every drawn connection must have a real path, every device
on the canvas must be part of the booking, and any routing intent must be valid.

**Surfaces.** User interface `frontend/src/components/reservations/CreateReservationModal.tsx`
(Reserve Topology); route `POST /` with `topology_id`, which calls cabling
`POST /topologies/{id}/validate/internal` (section 10).

**Rules.**

- **RES-TOPO-1.** With a `topology_id`, create calls cabling's internal validate route
  with the internal token (not the caller's JWT) and a 20 second timeout; without one,
  no call is made. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_topology_connectivity`, `_VALIDATE_TOPOLOGY_TIMEOUT_SECONDS`) \
  Pinned by: `services/reservations/tests/test_coverage_gaps.py` (`test_validate_topology_connectivity_uses_20s_timeout`); `services/reservations/tests/test_reservations.py` (`test_create_reservation_validation_skipped_without_topology`)
- **RES-TOPO-2.** A canvas device outside the booking's `device_ids` refuses the booking
  with a structured 422, checked before connectivity. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_topology_connectivity`, `TopologyDeviceNotMember`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_blocked_by_topology_device_not_member`); `services/reservations/tests/test_coverage_gaps.py` (`test_validate_topology_connectivity_membership_checked_before_connectivity`)
- **RES-TOPO-3.** Unreachable edges with no route problem refuse the booking with a
  plain-string 422 naming at most five edges. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_topology_connectivity`, `_summarize`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_blocked_by_invalid_topology`); `services/reservations/tests/test_coverage_gaps.py` (`test_validate_topology_connectivity_invalid_edges_and_routes_combined_first_five`)
- **RES-TOPO-4.** Any invalid routing intent refuses the booking with a structured 422
  carrying cabling's raw `invalid_routes`. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`TopologyRoutingIntentInvalid`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_blocked_by_invalid_routing_intent`)
- **RES-TOPO-5.** A cabling 404 (topology gone) is treated as "nothing to validate" and
  the booking proceeds with that `topology_id`. When it activates, cabling creates the
  fork with an empty (null) canvas, so nothing is wired from the missing topology. By
  decision (the comment at the 404 branch of `_validate_topology_connectivity`); see
  section 13. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_topology_connectivity`); `services/cabling/app/services/fork_service.py` (`_resolve_parent_canvas`) \
  Pinned by: `services/reservations/tests/test_coverage_gaps.py` (`test_validate_topology_connectivity_404_is_noop`)
- **RES-TOPO-6.** Any other cabling status of 400 or above, or a transport error, fails
  closed with 503. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_topology_connectivity`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_create_reservation_cabling_unreachable_is_503`); `services/reservations/tests/test_coverage_gaps.py` (`test_validate_topology_connectivity_503_from_cabling_raises_runtime`)

**Out of scope.** What makes an edge or a route valid is cabling's rule (`topology.md`).

### 8.4 Dynamic requests on a booking

**What it does.** A booking can ask for virtual instances that a hypervisor creates for
it. The reservation waits in `PENDING_PROVISION` until execution reports that every
instance exists, then goes live with the new devices attached; if creation fails, the
reservation fails.

**Surfaces.** User interface `frontend/src/components/reservations/CreateReservationModal.tsx`
(Dynamic instances block); route `POST /` with `dynamic_requests`; internal route
`POST /internal/{id}/provision-result`; event `herd.reservations.provision_requested`.
The transitions are RES-DYN-4 to RES-DYN-9 and RES-SWEEP-6 (section 4).

**Rules.**

- **RES-DYN-1.** Each dynamic request's template must exist in inventory and have
  `template_type` `dynamic`; inventory unreachable fails closed with 503. The template is
  read with the caller's JWT, so a dynamic template hidden from a non-admin
  (`dynamic-resources.md`, DYN-REQ-9) is refused with the same `Template <id> not found in
  inventory` as an unknown id (issue #1053). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_dynamic_requests`, `_fetch_dynamic_templates`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_unknown_template_422_wording`, `test_dynamic_booking_non_dynamic_template_422_wording`, `test_dynamic_booking_inventory_unreachable_503_wording`); `services/reservations/tests/test_dynamic_gating.py` (`test_hidden_dynamic_template_refused_exactly_like_an_unknown_id`)
- **RES-DYN-2.** A booking carries at most 50 dynamic requests; the create form's
  `MAX_DYNAMIC_REQUESTS` mirrors the cap. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`ReservationCreate`); `frontend/src/components/reservations/CreateReservationModal.tsx` (`MAX_DYNAMIC_REQUESTS`) \
  Pinned by: `services/reservations/tests/test_schema_bounds.py` (`test_dynamic_requests_at_cap_accepted`, `test_dynamic_requests_over_cap_rejected`); `tests/unit/test_dynamic_request_cap_parity.py` (`test_dynamic_request_cap_is_fifty_on_both_sides`)
- **RES-DYN-3.** Listing a template N times books N instances, one row each; there is
  no dedupe. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_creates_one_row_per_instance`)
- **RES-DYN-10.** After the template checks, each distinct hypervisor of the requested
  templates is read once through inventory's internal route; a disabled hypervisor
  refuses the booking with 422 naming it and its templates, a hypervisor that no longer
  exists with 422, and a check that cannot be answered (no internal token, transport
  error, non-200 other than 404, a body without a boolean `enabled`) fails closed with 503
  (issue #1033). A hidden template is refused before this check, so its hypervisor's
  name is never revealed. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_dynamic_requests`, `_fetch_hypervisor_internal`) \
  Pinned by: `services/reservations/tests/test_dynamic_gating.py` (`test_disabled_hypervisor_refused_with_422_naming_it`, `test_enabled_hypervisor_books`, `test_each_distinct_hypervisor_is_read_once`, `test_missing_hypervisor_refused_with_422`, `test_unanswerable_hypervisor_check_fails_closed_with_503`, `test_hypervisor_check_without_an_internal_token_fails_closed`, `test_hidden_template_never_reaches_the_hypervisor_check`)

**Out of scope.** Instance creation, the instance ledger, keyed destroy, and teardown on
`reservation.failed` or cancel are execution's (`dynamic-resources.md`).

### 8.5 Device holds in inventory

**What it does.** While a reservation holds an exclusive device, inventory shows that
device as `RESERVED`, and it goes back to `AVAILABLE` when the hold ends, unless another
reservation now holds it. No error from this feature reaches a caller: every release
runs after the commit.

**Surfaces.** Inventory `POST /devices/{id}/status` and `GET /devices/{id}/internal`
(section 10).

**Rules.**

- **RES-HOLD-1.** An exclusive device is held by a reservation in `PENDING_PROVISION` or
  `ACTIVE`; a `PENDING` reservation holds nothing, so cancelling it or editing its
  devices writes no inventory status. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_DEVICE_HOLDING_STATUSES`, `cancel_reservation`, `update_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_hold_invariant.py` (`test_cancel_writes_inventory_only_when_row_held_devices`, `test_patch_add_writes_inventory_only_when_row_holds_devices`, `test_patch_remove_writes_inventory_only_when_row_holds_devices`); `services/reservations/tests/test_reservation_patch_hold.py` (`test_pending_add_accepts_a_device_reserved_now_but_free_in_the_window`)
- **RES-HOLD-2.** Non-exclusive devices are never written to inventory by any
  reservation path. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `_release_exclusive_devices_best_effort`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_non_exclusive_device_status_not_changed`, `test_cancel_non_exclusive_skips_status_update`)
- **RES-HOLD-3.** Cancel, release, provision failure, the dynamic timeout, the restart
  backstop's revert, auto-complete, and lost-activation reverts skip any device another `PENDING_PROVISION` or `ACTIVE`
  reservation holds, logging `release_skipped_device_held`; so does the create-path
  flip-failure revert (RES-CREATE-15). PATCH removal (RES-PATCH-10) does not use this
  check. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`release_devices_not_held_by_others`) \
  Pinned by: `services/reservations/tests/test_reservation_hold_invariant.py` (`test_cancel_skips_a_device_another_live_row_holds`, `test_release_skips_a_device_another_live_row_holds`); `services/reservations/tests/test_expiration_hold_invariant.py` (`test_release_skips_device_a_pending_provision_row_holds`); `services/reservations/tests/test_reservation_patch_hold.py` (`test_active_remove_skips_a_device_another_live_row_holds`)
- **RES-HOLD-4.** If the holder lookup itself fails, the release proceeds for every
  device (fail open toward releasing). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`release_devices_not_held_by_others`) \
  Pinned by: `services/reservations/tests/test_reservation_hold_invariant.py` (`test_holder_lookup_failure_falls_back_to_releasing`)
- **RES-HOLD-5.** Cancel, release, provision failure, the dynamic timeout, and the
  restart backstop's revert share one release helper: a device whose exclusivity cannot
  be read is treated as exclusive, and a device inventory answers 404 for is dropped
  from the release set. Auto-complete reads exclusivity itself and follows RES-HOLD-9
  instead. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_release_exclusive_devices_best_effort`, `_DeviceGoneFromInventory`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_cancel_fetch_failure_falls_back_to_exclusive`); `services/reservations/tests/test_reservation_service_unit.py` (`test_release_exclusive_devices_drops_404_fetch_result`)
- **RES-HOLD-6.** A 404 from inventory when setting a device `AVAILABLE` counts as
  success; a 404 when setting it `RESERVED` is a failure. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_update_device_statuses`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_update_device_statuses_404_on_release_is_success`, `test_update_device_statuses_404_on_reserved_still_raises`)
- **RES-HOLD-7.** Releases after a terminal transition run after the commit, with three
  attempts on cancel, release, provision failure, and the timeout path, and a single
  attempt on auto-complete; a release that still fails is logged and the terminal
  status stands. PATCH writes follow the same pattern (RES-PATCH-10). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_release_exclusive_devices_best_effort`); `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_cancel_reservation_retries_and_logs_when_release_fails`, `test_release_reservation_retries_and_logs_when_inventory_fails`)
- **RES-HOLD-8.** Without `INTERNAL_API_TOKEN` configured, no inventory status write is
  attempted at all. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_update_device_statuses`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_update_device_statuses_no_token`)
- **RES-HOLD-9.** Auto-complete treats every device whose record it cannot read, a 404
  included, as exclusive and posts `AVAILABLE` for it (holder-aware, RES-HOLD-3); a 404
  on that post counts as success (RES-HOLD-6). \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`) \
  Pinned by: `services/reservations/tests/test_expiration.py` (`test_expiration_releases_all_device_ids_without_exclusivity_check`)

**Out of scope.** Wiring teardown on the hardware is execution's
(`provisioning-and-wiring.md`).

### 8.6 Cancel and release

**What it does.** The owner, or an admin, can cancel a reservation that has not finished;
its devices are freed. The owner can release an active reservation early, which ends it
as completed.

**Surfaces.** User interface `frontend/src/pages/ReservationsPage.tsx` (row buttons) and
`frontend/src/components/reservations/ReservationDetailModal.tsx`, gated by
`frontend/src/lib/reservationStatus.ts`; routes `DELETE /{id}` and `PUT /{id}/release`.
The transitions are RES-CANCEL-3 to RES-CANCEL-5 and RES-RELEASE-3 to RES-RELEASE-4
(section 4).

**Rules.**

- **RES-CANCEL-1.** The owner may cancel; an admin may cancel any reservation; anyone
  else gets 404, never 403. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`cancel_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_non_admin_cannot_cancel_other_users_reservation`, `test_admin_cancel_any_records_canceller`)
- **RES-CANCEL-2.** `cancelled_by` is set to the admin only when an admin cancels a
  reservation they do not own; an owner's cancel leaves it null. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`cancel_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_admin_cancel_any_records_canceller`, `test_cancel_reservation_self_cancel_leaves_cancelled_by_null`)
- **RES-CANCEL-6.** The UI offers Cancel on `PENDING`, `PENDING_PROVISION`, and `ACTIVE`
  to the owner or an admin, and Release on `ACTIVE` to the owner only; the row buttons,
  the detail modal, and the bulk actions share these two functions. \
  Enforced in: `frontend/src/lib/reservationStatus.ts` (`canCancelAs`, `canReleaseAs`) \
  Pinned by: `frontend/src/test/lib/reservationStatus.test.ts` (`canCancel`, `canRelease`); `frontend/src/test/pages/ReservationsPage.test.tsx` (`an admin sees Cancel but not Release on another user's row`)
- **RES-RELEASE-1.** Only the owner may release; any other caller gets 404. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`release_reservation`, `get_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_user_b_cannot_release_user_a_reservation`)
- **RES-RELEASE-2.** An admin who does not own the reservation gets 404 on release too.
  By decision ([ROLES.md](../ROLES.md); issue #843 aligned the UI to it). \
  Enforced in: `services/reservations/app/routers/reservations.py` (`release_reservation_early`) \
  Pinned by: `services/reservations/tests/test_owner_scoped_access.py` (`test_non_owner_admin_release_is_404_and_changes_nothing`)

**Out of scope.** Hardware teardown triggered by the terminal events is execution's
(`provisioning-and-wiring.md`, `dynamic-resources.md`).

### 8.7 Automatic expiration (the sweep)

**What it does.** A background loop starts scheduled reservations at their start time,
ends active ones at their end time, warns owners shortly before the end, and repairs
anything a crash or an outage left half-done. No error from the sweep reaches a caller.

**Surfaces.** `expiration_loop` every `EXPIRATION_INTERVAL_SECONDS` (default 60) and
`purpose_classify_loop` every `PURPOSE_CLASSIFY_INTERVAL_SECONDS` (default 60), both in
`services/reservations/app/tasks/expiration.py`, started by
`services/reservations/app/main.py`. The transitions the sweep makes are RES-SWEEP-1 to
RES-SWEEP-4 and RES-SWEEP-6 to RES-SWEEP-8 (section 4); the fork reconcile and
pending-prune rules are in section 8.12, the purpose reconciler rules in section 8.11.

One expiration tick runs, in order: the expiration cycle (claim due `PENDING` rows, fail
elapsed `PENDING` rows, auto-complete, both provisioning backstops, commit, release
completed rows' devices, activate claimed rows, release timed-out rows' devices), the
reminder cycle, the fork reconcile (archive, wiring heal, missing-fork backstop), and the
pending-prune reconcile.

**Rules.**

- **RES-SWEEP-5.** Within one tick, completed rows' devices are released before claimed
  rows are activated, so back-to-back bookings (`R2.start == R1.end`) both end correct. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_expiration_cycle`) \
  Pinned by: `services/reservations/tests/test_expiration_hold_invariant.py` (`test_adjacent_reservations_due_in_one_tick_end_reserved`)
- **RES-SWEEP-9.** An `ACTIVE` row whose `end_time` is in the future but within
  `EXPIRY_REMINDER_LEAD_SECONDS` (default 3600) gets exactly one
  `reservation.expiring_soon`, deduped by `expiry_reminder_sent_at` stamped in the same
  transaction; 0 disables reminders. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_reminder_cycle`) \
  Pinned by: `services/reservations/tests/test_expiry_reminder.py` (`test_reminder_not_repeated_across_ticks`, `test_reminder_stamp_and_event_commit_together`, `test_lead_window_zero_disables_reminder`)
- **RES-SWEEP-10.** A failure in one step of a tick does not stop the loop or the
  following steps. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`expiration_loop`) \
  Pinned by: `services/reservations/tests/test_coverage_gaps.py` (`test_expiration_loop_runs_both_cycles_and_handles_errors`)
- **RES-SWEEP-11.** Purpose classification runs on its own task and interval, never
  inside the expiration loop. \
  Enforced in: `services/reservations/app/main.py` (`lifespan`); `services/reservations/app/tasks/expiration.py` (`purpose_classify_loop`) \
  Pinned by: `services/reservations/tests/test_main_task_lifecycle.py` (`test_lifespan_purpose_classify_task_uses_its_own_interval_setting`); `services/reservations/tests/test_coverage_gaps.py` (`test_expiration_loop_no_longer_drives_purpose_classify_reconcile`)

**Out of scope.** What execution does with the events.

### 8.8 Live editing (PATCH)

**What it does.** The owner can change the end time, the purpose text, and the device
list of a pending or active reservation. Removing a device frees it and releases its
wiring; adding a device books it but wires nothing until the fork is committed.

**Surfaces.** User interface `frontend/src/components/reservations/EditDevicesModal.tsx`
and the Schedule tab of `frontend/src/components/reservations/ReservationDetailModal.tsx`;
route `PATCH /{id}` (body `ReservationUpdate`); events `herd.reservations.updated` and,
on removal from an `ACTIVE` row, `herd.reservations.wiring_changed` via the prune.

**Rules.**

- **RES-PATCH-1.** Only `ACTIVE` and `PENDING` reservations can be edited; any other
  status answers 400 with no side effect. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_update_completed_reservation_rejected`); `services/reservations/tests/test_reservation_hold_invariant.py` (`test_patch_on_pending_provision_is_refused_with_zero_inventory_calls`)
- **RES-PATCH-2.** Only the owner can edit; every other caller, an admin included, gets
  404. By decision ([ROLES.md](../ROLES.md): owner-scoped, no admin bypass). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`, `get_reservation`) \
  Pinned by: `services/reservations/tests/test_owner_scoped_access.py` (`test_non_owner_admin_patch_is_404_and_changes_nothing`, `test_non_owner_user_patch_is_404_and_changes_nothing`, `test_owner_patch_still_succeeds`)
- **RES-PATCH-3.** A new `end_time` must be after `start_time` and in the future. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`) \
  Pinned by: `services/reservations/tests/test_coverage_gaps.py` (`test_update_reservation_end_time_not_after_start_rejected`, `test_update_reservation_end_time_in_past_rejected`)
- **RES-PATCH-4.** Extending the end time conflict-checks the added span
  `[old end, new end)` for the booked exclusive devices; when inventory cannot say which
  devices are exclusive, all are checked. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_update_reservation_conflict_on_extension`); `services/reservations/tests/test_coverage_gaps.py` (`test_update_reservation_extend_fetch_failure_falls_back_to_exclusive`)
- **RES-PATCH-5.** A new `end_time` is judged against `RESERVATION_MAX_DURATION_SECONDS`
  over the effective window (the stored `start_time` to the new end) by the same check
  create uses (RES-CREATE-5), with the same wording; exactly the cap passes, one second
  more is refused, 0 disables the cap. The refusal is 400, the PATCH convention for its
  service-level refusals (section 13). Only a PATCH that sets `end_time` is judged, so a
  row already longer than the cap stays editable in its purpose and devices; setting a
  new end that is still over the cap is refused. PATCH cannot change `start_time`. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`check_max_duration`); `services/reservations/app/services/reservation_service.py` (`update_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_patch_hold.py` (`test_patch_end_time_is_judged_against_the_cap`, `test_patch_cap_uses_the_stored_start_on_an_active_row`, `test_over_cap_legacy_row_stays_editable_outside_its_window`, `test_over_cap_legacy_row_cannot_set_a_window_still_over_the_cap`, `test_cap_zero_disables_the_patch_check`); `services/reservations/tests/test_reservations.py` (`test_update_reservation_over_max_duration_returns_400`)
- **RES-PATCH-6.** A new `device_ids` must be non-empty (at most 200, deduped), every
  device must exist, and the booked devices must share one topology type. The instance
  devices the reservation already holds (a held device whose template is one of the
  reservation's dynamic request templates) are left out of the type check, so a booking
  of physical devices plus instances can change its physical devices while keeping its
  instances (issue #1030, decided 2026-10-07). A device not yet held is always judged. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`device_ids_not_empty`); `services/reservations/app/services/reservation_service.py` (`update_reservation`, `_held_instance_device_ids`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_update_reservation_empty_device_ids_rejected`, `test_update_reservation_topology_mismatch_rejected`); `services/reservations/tests/test_schema_bounds.py` (`test_update_device_ids_over_cap_rejected`); `services/reservations/tests/test_dynamic_gating.py` (`test_mixed_reservation_adds_a_device_and_keeps_its_instance`, `test_mixed_reservation_can_drop_a_physical_device_and_keep_its_instance`, `test_a_cloud_device_the_reservation_does_not_hold_is_still_refused`, `test_a_held_cloud_device_of_another_template_is_still_refused`, `test_without_dynamic_requests_the_whole_set_is_judged`, `test_mixed_patch_through_the_route_is_200`)
- **RES-PATCH-7.** A non-admin's new `device_ids` must all be visible to them (403
  otherwise). When the visibility lookup cannot be answered the router skips this
  pre-check, as on create (RES-CREATE-8), and the device fetch of RES-PATCH-6 still
  refuses an invisible device as not found (400). The calendar, which returns rows,
  fails closed instead (RES-CAL-3). \
  Enforced in: `services/reservations/app/routers/reservations.py` (`update_reservation_by_id`, `_fetch_visible_device_ids`); `services/reservations/app/services/reservation_service.py` (`_fetch_devices`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_update_reservation_non_admin_invisible_device_rejected`)
- **RES-PATCH-8.** On a topology-backed reservation, a device-set change re-runs
  cabling's validation for connectivity and membership with the routing pass skipped
  (`l3=0`). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`, `_validate_topology_connectivity`) \
  Pinned by: `services/reservations/tests/test_coverage_gaps.py` (`test_update_reservation_device_change_revalidates_topology`); `services/reservations/tests/test_reservations.py` (`test_update_reservation_device_change_breaks_topology_rejected`)
- **RES-PATCH-9.** An added exclusive device must not conflict over
  `[max(now, start), end)`, checked by the same refusal create uses, with create's
  wording (`... already reserved in the requested window`). On an `ACTIVE` reservation it
  must also be `AVAILABLE` now, because the row is about to hold it. On a `PENDING`
  reservation the current status is not consulted, exactly like a create for a future
  window (RES-CREATE-12): the row holds nothing until activation, which sets the device
  `RESERVED` as usual. By decision (issue #999, 2026-10-05). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`, `_assert_no_window_conflicts`) \
  Pinned by: `services/reservations/tests/test_reservation_patch_hold.py` (`test_pending_add_accepts_a_device_reserved_now_but_free_in_the_window`, `test_pending_add_refuses_a_device_booked_over_the_window_with_creates_wording`, `test_active_add_still_refuses_a_device_that_is_not_available_now`, `test_device_added_to_a_pending_row_is_reserved_at_activation`); `services/reservations/tests/test_reservations.py` (`test_update_reservation_conflict_on_added_device`)
- **RES-PATCH-10.** The edit commits under a status guard: a self-transition
  compare-and-swap on the status the PATCH read. If a cancel, release, auto-complete, or
  the sweep's activation claim moved the row during the PATCH, nothing of the edit is
  kept (no device-set change, no event, no inventory write) and the PATCH answers 409
  `Reservation changed status during the update (<read> to <now>); nothing was changed`
  (log action `reservation_update_lost_race`). Inventory is written only after the
  commit and only on an `ACTIVE` row: added exclusive devices are set `RESERVED` with
  three attempts, then the status is read again and, if the row has left `ACTIVE`
  meanwhile, the devices this call set are put back to `AVAILABLE` holder-aware
  (`reservation_update_hold_reverted`); removed devices go through the shared release
  helper (RES-HOLD-3, RES-HOLD-5) with three attempts. A write that still fails is logged
  (`reservation_update_hold_failed`, `reservation_update_release_failed`) and the
  committed edit stands, by decision: no double booking follows, because the conflict
  check reads reservations' own rows, and the row's next transition writes the device
  again. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`, `ReservationStatusChanged`, `_hold_added_devices_after_edit`, `_release_exclusive_devices_best_effort`) \
  Pinned by: `services/reservations/tests/test_reservation_patch_hold.py` (`test_active_add_reserves_only_after_the_edit_committed`, `test_active_add_inventory_failure_is_retried_logged_and_the_edit_stands`, `test_active_remove_inventory_failure_is_retried_logged_and_the_edit_stands`, `test_commit_failure_writes_nothing_to_inventory`, `test_patch_losing_its_status_guard_keeps_nothing`, `test_cancel_committing_after_the_edit_reverts_the_added_hold`, `test_patch_route_answers_409_when_the_status_guard_loses`); `services/reservations/tests/test_reservation_patch_race_live_pg.py` (`test_cancel_during_the_patch_checks_keeps_nothing_of_the_patch`, `test_cancel_during_the_post_commit_flip_leaves_the_added_device_released`, `test_patch_versus_cancel_jittered_around_the_patch_commit`); `services/reservations/tests/test_coverage_gaps.py` (`test_update_reservation_add_exclusive_device_marks_reserved`, `test_update_reservation_remove_exclusive_device_marks_available`)
- **RES-PATCH-11.** Removing devices from an `ACTIVE` reservation records them in
  `pending_fork_prune_device_ids` in the edit's transaction, unioned with any ids still
  pending, then asks cabling to prune them from the fork (RES-FORK-16). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`, `_prune_removed_devices_from_fork_best_effort`) \
  Pinned by: `services/reservations/tests/test_pending_fork_prune.py` (`test_patch_remove_writes_marker_with_the_edit`, `test_patch_remove_unions_into_existing_marker`)
- **RES-PATCH-12.** Adding a device wires nothing; its connections are built only by a
  later fork save. By decision (ADR 0009 Decision 6). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`) \
  Pinned by: `tests/integration/test_device_set_patch_wiring.py` (`test_patch_add_wires_nothing_until_fork_save_and_remove_releases`)
- **RES-PATCH-13.** Every successful edit stages `reservation.updated` in its
  transaction, with `end_time` set only when the end time actually changed. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`update_reservation`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_update_reservation_metadata_only_omits_end_time`, `test_update_reservation_changed_end_time_is_flagged_and_carried`)

**Out of scope.** The fork prune itself and the wiring release are cabling's and
execution's (`provisioning-and-wiring.md`).

### 8.9 Listing, sorting, search, and filters

**What it does.** Users see their own reservations; admins can switch to everyone's. The
list sorts from its column headings, searches purpose text or an id prefix, and filters
by status, category, and period. Choices are remembered per user.

**Surfaces.** User interface `frontend/src/pages/ReservationsPage.tsx`, saved filter in
`frontend/src/lib/reservationFilters.ts`; route `GET /` (its parameters are listed under
section 5).

**Rules.**

- **RES-LIST-1.** Without `all`, the list holds only the caller's own reservations;
  `all=true` lists everyone's and is admin-only. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_my_reservations`); `services/reservations/app/services/reservation_service.py` (`list_user_reservations`, `list_all_reservations`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_list_all_true_forbidden_for_non_admin`, `test_list_all_true_admin_sees_every_user`, `test_user_b_cannot_list_user_a_reservations`)
- **RES-LIST-2.** Every filter is ANDed after the visibility clause, so no filter or
  sort can reveal a row outside the caller's set. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_apply_list_filters`) \
  Pinned by: `services/reservations/tests/test_reservation_list_filters.py` (`test_search_by_id_never_reveals_another_users_row`, `test_visibility_enumeration_non_admin_never_sees_foreign_rows`)
- **RES-LIST-3.** `sort_by` is one of `start_time`, `end_time`, `status`,
  `purpose_category`, `user_id`, `created_at` (default `created_at`) and `sort_dir` one of
  `asc`, `desc` (default `desc`); anything else is 422. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`ReservationSortField`, `ReservationSortDir`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_sort_default_matches_todays_ordering`, `test_sort_unknown_sort_by_is_422`, `test_sort_unknown_sort_dir_is_422`)
- **RES-LIST-4.** Every ordering is tiebroken by id ascending, so pages never overlap or
  skip. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_reservation_order_by`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_sort_tiebreak_keeps_pagination_stable`, `test_sort_pagination_disjoint_and_ordered`)
- **RES-LIST-5.** `status` sorts alphabetically by the status name on every database. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_reservation_order_by`) \
  Pinned by: `services/reservations/tests/test_reservation_sort_live_pg.py` (`test_sort_by_status_ascending_is_alphabetical_on_postgres`)
- **RES-LIST-6.** `search` (at most 200 characters) is a case-insensitive substring
  match on purpose, with `%` and `_` literal; a term of 8 to 32 hex digits, hyphens
  ignored, also matches ids starting with it. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_apply_list_filters`, `_id_prefix_term`, `ID_PREFIX_MIN_HEX`) \
  Pinned by: `services/reservations/tests/test_reservation_list_filters.py` (`test_search_is_case_insensitive_substring_on_purpose`, `test_search_treats_like_metacharacters_literally`, `test_search_matches_short_id_prefix_and_full_id`, `test_id_prefix_term`)
- **RES-LIST-7.** `purpose_category` must be a configured category or `none` (no
  category); any other value is 422. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_my_reservations`) \
  Pinned by: `services/reservations/tests/test_reservation_list_filters.py` (`test_purpose_category_value_and_none`, `test_purpose_category_unknown_is_422_with_pinned_message`)
- **RES-LIST-8.** Time bounds must carry a timezone; `*_after` is inclusive and
  `*_before` exclusive, compared by instant. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_apply_list_filters`); `services/reservations/app/routers/reservations.py` (`_as_utc`) \
  Pinned by: `services/reservations/tests/test_reservation_list_filters.py` (`test_window_bounds_are_half_open`, `test_window_bad_or_naive_timestamp_is_422`, `test_window_bound_with_offset_is_compared_by_instant`)
- **RES-LIST-9.** `total` is the filtered total. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`list_user_reservations`, `list_all_reservations`) \
  Pinned by: `services/reservations/tests/test_reservation_list_filters.py` (`test_total_is_filtered_total_across_pages_and_sort`)
- **RES-LIST-10.** The page maps Period to bounds from one anchor instant fixed when a
  filter changes: Upcoming is `starts_after`, Current is `starts_before` plus
  `ends_after`, Past is `ends_before`. \
  Enforced in: `frontend/src/lib/reservationFilters.ts` (`reservationListFilters`) \
  Pinned by: `frontend/src/test/lib/reservationFilters.test.ts` (`maps each period onto the half-open window at the anchor`); `frontend/src/test/pages/ReservationsPageFilters.test.tsx` (`paging keeps the period anchor fixed`)
- **RES-LIST-11.** A saved filter value that no longer exists (status, period, or a
  removed category) falls back to All and is never sent. \
  Enforced in: `frontend/src/lib/reservationFilters.ts` (`parseSavedReservationFilter`, `effectivePurposeCategory`) \
  Pinned by: `frontend/src/test/lib/reservationFilters.test.ts` (`drops a stale status or period and a non-string field`); `frontend/src/test/pages/ReservationsPageFilters.test.tsx` (`never sends a stale saved value`)
- **RES-LIST-12.** A heading click cycles ascending, descending, then back to the
  default, and the choice persists per user. \
  Enforced in: `frontend/src/pages/ReservationsPage.tsx` (`ReservationsPage`) \
  Pinned by: `frontend/src/test/pages/ReservationsPage.test.tsx` (`cycles a heading through ascending, descending, and back to the default on a third click`, `persists the chosen sort through the preferences store`)

**Out of scope.** The `/api/v1` facade forwards only `skip` and `limit` by decision
(`integration.md`).

### 8.10 One reservation and the calendar

**What it does.** The owner opens a reservation by id. Anyone signed in sees the
calendar of everyone's reservations in a window, limited for non-admins to reservations
whose devices they can all see.

**Surfaces.** User interface `frontend/src/components/reservations/ReservationDetailModal.tsx`
and `frontend/src/pages/ReservationCalendarPage.tsx`; routes `GET /{id}` and
`GET /calendar`.

**Rules.**

- **RES-VIEW-1.** `GET /{id}` answers only the owner; anyone else gets 404. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`get_reservation`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_user_b_cannot_get_user_a_reservation`)
- **RES-VIEW-2.** That includes an admin who does not own the reservation. By decision
  ([ROLES.md](../ROLES.md): owner only for every role). \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_reservation_by_id`) \
  Pinned by: `services/reservations/tests/test_owner_scoped_access.py` (`test_non_owner_admin_get_by_id_is_404`)
- **RES-CAL-1.** The calendar returns every user's reservations overlapping
  `[range_start, range_end)`. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`list_calendar_reservations`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_calendar_returns_cross_user_reservations`, `test_calendar_boundary_exclusion`)
- **RES-CAL-2.** A window wider than `CALENDAR_MAX_SPAN_DAYS` (default 366) is 422; 0
  disables the cap. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`list_calendar_reservations`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_calendar_span_over_max_rejected`); `services/reservations/tests/test_reservation_service_unit.py` (`test_list_calendar_span_guard_disabled_when_zero`)
- **RES-CAL-3.** For a non-admin the calendar keeps only reservations all of whose
  devices are visible to them. When the visibility lookup cannot be answered (transport
  error, non-200, or a body that is not `{"device_ids": [<str>, ...]}`) the calendar
  fails closed: 503 `Could not verify device visibility; reservations were not returned.
  Retry the request.` and no rows. Admins are unfiltered and never trigger the lookup.
  By decision (issue #1000, 2026-10-05, superseding the fail-open accepted in #131). The
  calendar page shows a could-not-load state on the refusal, not the empty-range
  message. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_calendar_reservations`, `_fetch_visible_device_ids_strict`); `frontend/src/pages/ReservationCalendarPage.tsx` (`ReservationCalendarPage`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_calendar_non_admin_visibility_filtering`, `test_calendar_non_admin_fails_closed_when_visibility_is_unanswerable`, `test_calendar_admin_is_unfiltered_and_never_asks_for_visibility`, `test_calendar_non_admin_with_a_real_answer_is_filtered`); `services/reservations/tests/test_reservation_service_unit.py` (`test_calendar_visibility_subset_and_empty_set`); `frontend/src/test/pages/ReservationCalendarPage.test.tsx` (`ReservationCalendarPage`)

**Out of scope.** Utilization reports (`GET /reports/utilization` and its CSV) live in
this service but belong to `operations-and-observability.md`.

### 8.11 Purpose category and purpose classification

**What it does.** A reservation can carry a purpose category from a configured list. The
owner or an admin sets it at any time, even after the reservation ends. When a
reservation finishes, a background job asks the AI orchestrator to suggest a category;
admins review suggestions on the Purpose Review page and accept, override, or dismiss
them.

**Surfaces.** User interface `frontend/src/components/reservations/ReservationDetailModal.tsx`
(category and Classify now) and `frontend/src/pages/admin/PurposeReviewPage.tsx`; routes
`GET /purpose-categories`, `PATCH /{id}/purpose-category`, and the five `/admin/purpose`
routes in section 5; background work `purpose_classify_loop` every
`PURPOSE_CLASSIFY_INTERVAL_SECONDS`.

**Rules.**

- **RES-PURPOSE-1.** A category, at create or by PATCH, must be in `PURPOSE_CATEGORIES`;
  the list is a plain string list, so a stored value survives being removed from it. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`validate_purpose_category`) \
  Pinned by: `services/reservations/tests/test_purpose_category.py` (`test_create_reservation_with_unknown_purpose_category_is_422`, `test_patch_purpose_category_unknown_category_is_422`)
- **RES-PURPOSE-2.** The owner or an admin may set or clear the category in any status,
  including terminal; anyone else gets 403 (not 404). \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`set_purpose_category`) \
  Pinned by: `services/reservations/tests/test_purpose_category.py` (`test_patch_purpose_category_by_admin`, `test_patch_purpose_category_allowed_on_completed_reservation`, `test_patch_purpose_category_by_third_user_is_403`)
- **RES-PURPOSE-3.** A null category clears the category, its setter, and its time
  together. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`set_purpose_category`) \
  Pinned by: `services/reservations/tests/test_purpose_category.py` (`test_patch_purpose_category_null_clears_all_three_fields`)
- **RES-PURPOSE-4.** `purpose_classify_requested_at` is stamped, once, in the same
  transaction as every terminal transition, seven today: cancel, release,
  auto-complete, provision-result failure, the dynamic timeout, the elapsed-window
  failure, and the create-path flip failure (RES-CREATE-15). Only a stamped row is ever
  classified by the sweep. A unit test enumerates every terminal status
  compare-and-swap in the service and fails when one has no matching stamp. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`stamp_purpose_classify_requested`) \
  Pinned by: `services/reservations/tests/test_purpose_classify_marker.py` (`test_cancel_reservation_stamps_marker`, `test_release_reservation_stamps_marker`, `test_expiry_autocomplete_stamps_marker`, `test_provision_result_failed_stamps_marker`, `test_timeout_backstop_failed_stamps_marker`, `test_stamp_is_idempotent_on_cancel`, `test_create_flip_failure_stamps_marker`, `test_every_terminal_status_write_stamps_the_marker`, `test_no_terminal_status_is_written_outside_the_cas_helpers`); `services/reservations/tests/test_expiration_hold_invariant.py` (`test_expired_pending_row_is_failed_not_activated`)
- **RES-PURPOSE-5.** Each sweep tick takes up to `PURPOSE_CLASSIFY_BATCH_SIZE` stamped
  rows with no suggestion and fewer than `PURPOSE_CLASSIFY_MAX_ATTEMPTS` attempts,
  oldest stamp first. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_purpose_classify_reconcile`) \
  Pinned by: `services/reservations/tests/test_purpose_classify_reconcile.py` (`test_reconcile_skips_rows_at_attempt_cap`, `test_reconcile_respects_batch_size`, `test_reconcile_oldest_requested_first`, `test_reconcile_ignores_rows_not_yet_requested`)
- **RES-PURPOSE-6.** A feature-off answer (403 with the disabled marker, or 404), a
  transient answer (429, 502, 503, 504, or a transport error other than a timeout), or
  a 403 without the marker ends the tick and counts no attempt. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_PURPOSE_CLASSIFY_TICK_ENDING_OUTCOMES`); `services/reservations/app/services/purpose_service.py` (`classify_purpose_one`) \
  Pinned by: `services/reservations/tests/test_purpose_classify_reconcile.py` (`test_reconcile_403_structured_marker_ends_tick_without_touching_any_row`, `test_reconcile_transient_status_ends_tick_without_bump`, `test_reconcile_403_bad_token_is_not_feature_off`)
- **RES-PURPOSE-7.** A timeout, any other non-200, or an unparseable body counts one
  attempt on that row and the tick continues. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`classify_purpose_one`, `_bump_purpose_classify_attempts`) \
  Pinned by: `services/reservations/tests/test_purpose_classify_reconcile.py` (`test_reconcile_timeout_bumps_attempts_and_does_not_end_tick`, `test_reconcile_500_increments_attempts`)
- **RES-PURPOSE-8.** A 200 stores the orchestrator's body verbatim as the suggestion; it
  never writes `purpose_category`. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`classify_purpose_one`) \
  Pinned by: `services/reservations/tests/test_purpose_classify_reconcile.py` (`test_reconcile_stores_suggestion_on_200`)
- **RES-PURPOSE-9.** The review list holds rows with an undismissed suggestion whose
  category is unset or differs from the suggestion's `top_category`. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`list_purpose_review_items`) \
  Pinned by: `services/reservations/tests/test_purpose_review.py` (`test_review_list_excludes_confirmed_agreeing_row`, `test_review_list_includes_confirmed_disagreeing_row`, `test_review_list_excludes_dismissed_row`)
- **RES-PURPOSE-10.** Accept with a null body takes the suggestion's `top_category`;
  with a value, that value (validated); the accepting admin becomes the setter. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`accept_purpose_suggestion`) \
  Pinned by: `services/reservations/tests/test_purpose_review.py` (`test_accept_with_null_uses_top_category`, `test_accept_with_chosen_value_overrides_top_category`)
- **RES-PURPOSE-11.** Dismiss sets only `purpose_suggestion_dismissed_at` and keeps the
  suggestion. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`dismiss_purpose_suggestion`) \
  Pinned by: `services/reservations/tests/test_purpose_review.py` (`test_dismiss_sets_dismissed_at_and_keeps_suggestion`)
- **RES-PURPOSE-12.** Backfill stamps every terminal row with no stamp and no
  suggestion, and resets the attempt count of every capped row with no suggestion; it
  returns the sum. \
  Enforced in: `services/reservations/app/services/purpose_service.py` (`backfill_purpose_classification`) \
  Pinned by: `services/reservations/tests/test_purpose_review.py` (`test_backfill_marks_terminal_rows_without_suggestion`, `test_backfill_resets_capped_rows_without_suggestion`, `test_backfill_counts_newly_marked_and_reset_rows_together`)
- **RES-PURPOSE-13.** Classify now runs the same single-row classifier immediately,
  ignores the attempt cap, and answers 200 with the outcome word for every outcome except
  feature-off. \
  Enforced in: `services/reservations/app/routers/purpose_review.py` (`trigger_purpose_classify`) \
  Pinned by: `services/reservations/tests/test_purpose_classify_trigger.py` (`test_trigger_ignores_the_attempt_cap`, `test_trigger_200_timeout_bumps_attempts`, `test_trigger_503_feature_off`)
- **RES-PURPOSE-14.** Every purpose review route is admin-only. \
  Enforced in: `services/reservations/app/routers/purpose_review.py` (`require_admin`) \
  Pinned by: `services/reservations/tests/test_purpose_review.py` (`test_review_list_is_admin_only`, `test_accept_is_admin_only`, `test_backfill_is_admin_only`); `services/reservations/tests/test_purpose_classify_trigger.py` (`test_trigger_is_admin_only`)

**Out of scope.** The classifier, its prompt, and its feature gate are
ai-orchestrator's (`ai-features.md`, [AI_PURPOSE_CLASSIFICATION.md](../AI_PURPOSE_CLASSIFICATION.md)).
Reporting by category is `operations-and-observability.md`.

### 8.12 The reservation's side of the topology fork

**What it does.** When a reservation goes live, HERD gives it a private, editable copy of
its topology. The API lets the owner or an admin read and edit it through the
reservation; the user interface offers it to the owner only. When the reservation ends,
the copy is frozen as the as-built record.

**Surfaces.** User interface `frontend/src/components/reservations/ReservationDetailModal.tsx`
(Edit topology, View as-built) and the live-edit mode of
`frontend/src/pages/TopologyEditorPage.tsx`; the seven `/{id}/fork` and `/{id}/wiring`
routes in section 5, which forward to cabling or execution with the internal token;
event `herd.reservations.wiring_changed`; the fork reconcile and pending-prune
reconcile on the expiration loop.

**Rules.**

- **RES-FORK-1.** On every transition into `ACTIVE`, a reservation with a parent
  topology asks cabling to create its fork with its own device set as
  `member_device_ids`, three attempts; a reservation with no topology gets no fork until
  first read. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_create_reservation_fork_best_effort`, `_create_reservation_fork`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_fork_posts_to_cabling`, `test_create_reservation_fork_skips_when_no_topology`); `services/reservations/tests/test_expiration.py` (`test_activation_threads_booking_user_into_fork`)
- **RES-FORK-2.** A cabling 409 or 422 on fork create is final: not retried, logged
  `reservation_fork_membership_refused`, and remembered in process memory so the sweep
  stops retrying it; the reservation stays `ACTIVE`, unwired. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`ForkMembershipRefused`, `_fork_membership_refused`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_fork_best_effort_membership_refused_is_not_retried`, `test_fork_best_effort_port_claim_409_is_not_retried`); `services/reservations/tests/test_fork_backstop_giveup.py` (`test_membership_refused_reservation_is_never_retried`)
- **RES-FORK-3.** After a successful fork create, a delta-less `reservation.wiring_changed`
  for the returned version is staged with the ledger advance, unless the ledger already
  holds that version or a later one. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_create_reservation_fork_best_effort`, `stage_wiring_changed`) \
  Pinned by: `services/reservations/tests/test_wiring_changed_staging.py` (`test_activation_stages_initial_wiring_heal_after_fork_create`, `test_activation_staging_is_ledger_guarded`, `test_activation_stages_nothing_when_fork_create_fails`)
- **RES-FORK-4.** Every fork and wiring route is owner-or-admin; any other caller gets
  404 and no upstream call is made. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`_load_owned_or_admin`) \
  Pinned by: `services/reservations/tests/test_fork_endpoints.py` (`test_get_fork_other_user_404_and_no_cabling_call`, `test_get_fork_admin_allowed_for_other_owner`); `services/reservations/tests/test_fork_version_endpoints.py` (`test_restore_other_user_404_and_no_cabling_call`)
- **RES-FORK-5.** Reading the fork works in any status; on a cabling 404 an `ACTIVE`
  reservation lazily creates the fork (also with no parent topology) and re-reads, while
  any other status answers 404 `Fork not found`. A failed or refused lazy create,
  including a cabling 409, answers 503. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_reservation_fork`); `services/reservations/app/services/reservation_service.py` (`_lazy_create_reservation_fork`) \
  Pinned by: `services/reservations/tests/test_fork_endpoints.py` (`test_get_fork_lazy_creates_on_active_miss`, `test_get_fork_lazy_creates_with_no_parent_topology`, `test_get_fork_ended_reservation_no_fork_404_and_no_lazy_create`)
- **RES-FORK-6.** Canvas PUT, save, and restore require `ACTIVE`; otherwise 409 and no
  upstream call. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`FORK_EDIT_REQUIRES_ACTIVE`, `FORK_SAVE_REQUIRES_ACTIVE`, `FORK_RESTORE_REQUIRES_ACTIVE`) \
  Pinned by: `services/reservations/tests/test_fork_endpoints.py` (`test_put_canvas_non_active_409_pinned_wording`, `test_save_non_active_409_pinned_wording`); `services/reservations/tests/test_fork_version_endpoints.py` (`test_restore_409_body_is_structured_error_shape`)
- **RES-FORK-7.** Save stamps `created_by` with the caller and `member_device_ids` with
  the reservation's devices (not from the client) and forwards with a 20 second timeout. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`save_reservation_fork`, `ForkSaveBody`) \
  Pinned by: `services/reservations/tests/test_fork_endpoints.py` (`test_save_active_forwards_and_stamps_created_by`, `test_save_forwards_at_fork_save_timeout_not_default`)
- **RES-FORK-8.** After cabling accepts a save, `reservation.wiring_changed` with
  cabling's `released` and `built` arrays is staged with the ledger advance in one
  commit; a staging failure is logged and the save still answers 200 (the sweep heals
  it). A refused save stages nothing. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`save_reservation_fork`) \
  Pinned by: `services/reservations/tests/test_fork_endpoints.py` (`test_save_stage_wiring_changed_failure_still_returns_200`); `services/reservations/tests/test_wiring_changed_staging.py` (`test_save_handler_relays_body_and_stages_event`, `test_save_handler_archived_409_stages_nothing`)
- **RES-FORK-9.** Restore forwards to cabling and stages no event. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`restore_reservation_fork_version`) \
  Pinned by: `services/reservations/tests/test_fork_version_endpoints.py` (`test_restore_does_not_stage_wiring_changed`)
- **RES-FORK-10.** A cabling 4xx is relayed with its status and unwrapped detail; a 5xx
  becomes 503 carrying cabling's JSON detail when there is one, else `Cabling service is
  unavailable`; a transport failure is 503. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`_relay_cabling_fork_response`, `_cabling_detail`) \
  Pinned by: `services/reservations/tests/test_fork_endpoints.py` (`test_save_port_conflict_409_structured_passthrough`, `test_get_fork_cabling_5xx_with_structured_json_relays_detail`, `test_get_fork_cabling_5xx_with_non_json_body_keeps_generic_message`, `test_get_fork_cabling_unreachable_maps_to_503`)
- **RES-FORK-11.** Every transition into `COMPLETED`, `CANCELLED`, or `FAILED` except
  the create-path flip failure and the elapsed-window failure asks cabling to archive the
  fork, three attempts, after the commit; failure is logged and never undoes the
  transition. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_archive_reservation_fork_best_effort`) \
  Pinned by: `services/reservations/tests/test_fork_archive_reconcile.py` (`test_release_reservation_archives_fork`, `test_expiry_autocomplete_archives_fork`, `test_archive_best_effort_swallows_and_retries`)
- **RES-FORK-12.** Each tick archives any fork cabling reports `ACTIVE` whose reservation
  is terminal; a fork for a reservation this service does not know is skipped and warned
  about once per process. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_fork_archive_reconcile`) \
  Pinned by: `services/reservations/tests/test_fork_archive_reconcile.py` (`test_reconcile_archives_terminal_skips_active_and_unknown`, `test_unknown_reservation_warns_once_per_process_across_ticks`)
- **RES-FORK-13.** Each tick stages a delta-less `reservation.wiring_changed` for every
  `ACTIVE` reservation whose latest fork version in cabling is greater than its ledger
  (missing ledger counts as 0). \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_heal_wiring_staging`) \
  Pinned by: `services/reservations/tests/test_wiring_changed_staging.py` (`test_heal_stages_when_cabling_exceeds_missing_ledger`, `test_no_heal_when_in_sync`, `test_terminal_reservation_is_not_healed`)
- **RES-FORK-14.** Each tick creates the fork for every `ACTIVE` reservation with a
  topology that cabling has no fork for, giving up after 5 failed ticks per reservation
  until the process restarts. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_backstop_missing_forks`, `_FORK_BACKSTOP_MAX_ATTEMPTS`) \
  Pinned by: `services/reservations/tests/test_wiring_changed_staging.py` (`test_sweep_creates_missing_fork_and_stages_initial_wiring`); `services/reservations/tests/test_fork_backstop_giveup.py` (`test_backs_off_after_cap`)
- **RES-FORK-15.** When cabling's active-fork listing fails, the whole fork reconcile
  (archive, heal, backstop) is skipped for that tick. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_fork_archive_reconcile`) \
  Pinned by: `services/reservations/tests/test_fork_archive_reconcile.py` (`test_reconcile_survives_fetch_failure`)
- **RES-FORK-16.** The removed-device prune stages cabling's `released` delta; a 404
  keeps the marker only while a fork can still appear (ACTIVE with a topology), a 409
  (archived) clears it, and a 5xx or transport error keeps it. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_prune_removed_devices_from_fork`, `_prune_removed_devices_from_fork_best_effort`, `_fork_may_still_appear`) \
  Pinned by: `services/reservations/tests/test_pending_fork_prune.py` (`test_wrapper_no_fork_keeps_marker_while_fork_may_appear`, `test_wrapper_archived_409_clears_marker`, `test_wrapper_failure_keeps_marker`); `services/reservations/tests/test_wiring_changed_staging.py` (`test_patch_remove_calls_prune_devices_and_stages_released_delta`)
- **RES-FORK-17.** Each tick retries up to 20 reservations with a pending prune marker,
  oldest `updated_at` first, one attempt each; a marker on a non-ACTIVE reservation is
  cleared without a prune. \
  Enforced in: `services/reservations/app/tasks/expiration.py` (`_run_pending_prune_reconcile`, `_PENDING_PRUNE_BATCH`) \
  Pinned by: `services/reservations/tests/test_pending_fork_prune.py` (`test_sweep_retries_pending_prune_and_clears_on_success`, `test_sweep_terminal_reservation_clears_marker_without_prune`)
- **RES-FORK-18.** Wiring status is readable in any status; wiring retry is refused with
  409 on `PENDING` and `PENDING_PROVISION`; execution's 4xx is relayed and its 5xx or
  absence is 503. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_reservation_wiring_status`, `retry_reservation_wiring`, `_relay_execution_response`) \
  Pinned by: `services/reservations/tests/test_wiring_proxy_endpoints.py` (`test_status_allowed_for_completed_reservation`, `test_retry_pre_provision_409_pinned_wording`, `test_retry_execution_frozen_409_passthrough`, `test_retry_execution_unreachable_maps_to_503`)
- **RES-FORK-19.** The detail modal offers Edit topology (an `ACTIVE` reservation) and
  View as-built (an ended one) only to the owner, and only when the reservation has a
  `topology_id`; an admin who does not own the reservation sees neither, although the
  API would serve them (RES-FORK-4). \
  Enforced in: `frontend/src/components/reservations/ReservationDetailModal.tsx` (`canEditFork`, `canViewAsBuilt`) \
  Pinned by: `frontend/src/test/components/ReservationDetailModalEditTopology.test.tsx` (`shows Edit topology for the owner of an ACTIVE reservation with a topology_id`, `hides both affordances for a non-owner even with a topology_id`, `hides both affordances when topology_id is null`)

**Out of scope.** Fork contents, save reconcile, port claims, version history, restore
semantics, and the wiring ledgers are cabling's and execution's (`topology.md`,
`provisioning-and-wiring.md`).

### 8.13 Bulk cancel and release

**What it does.** On the Reservations list a user ticks rows (or all rows on the page)
and cancels or releases them together. One confirmation says how many will run and how
many will be skipped and why; the result reports successes and failures, and failed rows
stay selected.

**Surfaces.** User interface `frontend/src/pages/ReservationsPage.tsx`, logic in
`frontend/src/lib/reservationBulk.ts`, fan-out in `frontend/src/api/reservations.ts`
(`useBulkReservationAction`). There is no bulk route: each selected row is one
`DELETE /{id}` or `PUT /{id}/release`, and each row's outcome is whatever that call
answers.

**Rules.**

- **RES-BULK-1.** A row is eligible by the same `canCancelAs` and `canReleaseAs` used
  by the single-row buttons; ineligible rows are skipped with the reason `finished`,
  `not_active`, or `not_yours` and are never sent. \
  Enforced in: `frontend/src/lib/reservationBulk.ts` (`partitionSelection`) \
  Pinned by: `frontend/src/test/lib/reservationBulk.test.ts` (`agrees with the single-row gates for every status, owner and caller kind`, `lets an admin cancel across owners but release only their own`)
- **RES-BULK-2.** The eligible calls run in parallel with `Promise.allSettled`; every
  fulfilled id leaves the selection and every rejected id stays with its reason. A row
  the backend answers 200 or 204 for without acting (RES-CANCEL-3, RES-RELEASE-3)
  counts as a success. \
  Enforced in: `frontend/src/api/reservations.ts` (`useBulkReservationAction`); `frontend/src/lib/reservationBulk.ts` (`applySettled`) \
  Pinned by: `frontend/src/test/lib/reservationBulk.test.ts` (`keeps failed ids selected with the server's reason`); `frontend/src/test/pages/ReservationsPage.test.tsx` (`keeps failed rows selected, reports the counts and reason, and invalidates once`)
- **RES-BULK-3.** Select-all covers the current page only, and the selection clears when
  the page, sort, any filter, or the All toggle changes. \
  Enforced in: `frontend/src/pages/ReservationsPage.tsx` (`ReservationsPage`) \
  Pinned by: `frontend/src/test/pages/ReservationsPage.test.tsx` (`select-all takes the current page only and goes indeterminate on a partial selection`, `clears the selection when the page changes`, `clears the selection when the sort changes`); `frontend/src/test/pages/ReservationsPageFilters.test.tsx` (`clears the selection on a filter change`)
- **RES-BULK-4.** An admin can bulk-cancel another user's reservation through the real
  stack. \
  Enforced in: `frontend/src/lib/reservationBulk.ts` (`partitionSelection`) \
  Pinned by: `tests/e2e/test_reservations_bulk_playwright.py` (`test_admin_bulk_cancels_another_users_reservation`)

**Out of scope.** There is no bulk endpoint, by decision (issue #843).

### 8.14 Event delivery

**What it does.** Every lifecycle change is announced on NATS so other services react;
the subjects and payloads are in section 6. No error from event delivery reaches a
caller.

**Surfaces.** The `outbox` table and the relay started in
`services/reservations/app/main.py`.

**Rules.**

- **RES-EVENT-1.** Every event is written to the outbox in the same database
  transaction as the state change it describes; none is published directly from request
  code. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`enqueue_event`); `services/reservations/app/tasks/expiration.py` (`enqueue_event`) \
  Pinned by: `services/reservations/tests/test_reservation_service_unit.py` (`test_create_reservation_exclusive_enqueues_created_event_in_txn`); `services/reservations/tests/test_expiry_reminder.py` (`test_reminder_stamp_and_event_commit_together`)
- **RES-EVENT-2.** `reservation.wiring_changed` exists if and only if the fork wiring
  ledger advanced: both are written in one commit. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`stage_wiring_changed`) \
  Pinned by: `services/reservations/tests/test_wiring_changed_staging.py` (`test_atomicity_neither_persists_on_failure_between_writes`)
- **RES-EVENT-3.** A heal or activation `wiring_changed` carries `released` and `built`
  as null, not empty lists. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_wiring_changed_payload`) \
  Pinned by: `services/reservations/tests/test_wiring_changed_staging.py` (`test_heal_staging_carries_null_delta`)
- **RES-EVENT-4.** NATS being unreachable at startup is logged and does not stop the
  service or its background tasks: the first connect is bounded
  (`operations-and-observability.md`, OPS-NATS-1) and its failure leaves the service
  running with no NATS connection. Events wait in the outbox, unpublished, until the
  service is restarted with the broker reachable; a connection that was established and
  then lost is recovered by the client's unlimited reconnect. \
  Enforced in: `services/reservations/app/main.py` (`lifespan`); `services/common/herd_common/jetstream.py` (`connect_nats`) \
  Pinned by: `services/reservations/tests/test_main_task_lifecycle.py` (`test_lifespan_starts_without_nats_using_the_real_client`, `test_lifespan_creates_and_cancels_expiration_and_purpose_classify_tasks`)

**Out of scope.** The relay, deduplication headers, and consumer behavior are shared
infrastructure (`operations-and-observability.md`); what consumers do is in their own
areas.

### 8.15 Answers to other services

**What it does.** Other services ask reservations questions about bookings without a
user in the flow. The routes, callers, and answers are in section 7.

**Surfaces.** The `/internal` routes in section 7, all guarded by `X-Internal-Token`.

**Rules.**

- **RES-INTERNAL-1.** A wrong internal token answers 403; a missing header answers 422. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`internal_token_matches`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_internal_status_bad_token_rejected`, `test_internal_status_missing_token_rejected`, `test_internal_active_bad_token_rejected`, `test_active_users_requires_valid_internal_token`)
- **RES-INTERNAL-2.** `is_active` is true only when the status is `ACTIVE` and now is
  within `[start_time, end_time]`. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_reservation_internal_status`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_internal_status_active`, `test_internal_status_outside_window_is_not_active`, `test_internal_status_cancelled_is_not_active`)
- **RES-INTERNAL-3.** `owns_active` is true only when the user owns an `ACTIVE`
  reservation that contains the device and whose window contains now. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`owns_active_reservation_for_device`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_internal_active_owner_with_device_returns_true`, `test_internal_active_non_owner_returns_false`, `test_internal_active_outside_window_returns_false`, `test_internal_active_cancelled_returns_false`)
- **RES-INTERNAL-4.** `active-users` lists, once each, the owners of `ACTIVE`
  reservations containing the device whose window contains now. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`list_active_reservation_users_for_device`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_active_users_dedupes_same_user_multiple_reservations`, `test_active_users_filters_by_time_window`, `test_active_users_filters_by_status`)
- **RES-INTERNAL-5.** `by-topology` and `by-device` return every reservation that
  references the topology or contains the device, in every status and for every owner,
  without pagination. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`list_reservations_for_topology`, `list_reservations_for_device`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_internal_by_topology_returns_matching_reservation`, `test_internal_by_device_returns_matching_reservation`)
- **RES-INTERNAL-6.** The literal `active`, `active-users`, `held-devices`,
  `by-topology`, and `by-device` segments are never parsed as a reservation id. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_reservation_internal_status`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_internal_active_no_collision_with_int_status_path`, `test_internal_status_no_collision_with_user_get`)
- **RES-INTERNAL-7.** `purpose_classification_pending` is true only when
  `purpose_classify_requested_at` is set, `purpose_suggestion` is null, and
  `purpose_classify_attempts` is below `PURPOSE_CLASSIFY_MAX_ATTEMPTS`: the same
  predicate the purpose sweep selects on, cap included (issue #1067). A capped row is
  therefore not pending; the admin backfill resets its attempts and makes it pending
  again, and the manual Classify now ignores the cap either way. The AI orchestrator's
  idle-conversation sweeper reads it to keep a transcript the classifier has yet to read
  (issue #1039, `ai-features.md` AI-CONV-13). \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_reservation_internal_status`); `services/reservations/app/schemas/reservation.py` (`ReservationInternalStatus`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_internal_status_reports_purpose_classification_pending`, `test_internal_status_pending_is_false_once_the_attempt_cap_is_reached`, `test_internal_status_pending_again_after_backfill_resets_a_capped_row`)

- **RES-INTERNAL-8.** `held-devices` lists, once each and sorted, every device of the
  user's `PENDING_PROVISION` and `ACTIVE` reservations (the statuses that hold devices),
  and nothing of a `PENDING` or terminal row or of another user's (issue #1030). \
  Enforced in: `services/reservations/app/routers/reservations.py` (`list_devices_held_by_user`) \
  Pinned by: `services/reservations/tests/test_dynamic_gating.py` (`test_held_devices_lists_only_the_users_live_reservations`, `test_held_devices_for_a_user_with_nothing_live_is_empty`, `test_held_devices_refuses_a_wrong_internal_token`)

**Out of scope.** How each caller uses the answer is in that caller's area.

## 9. Errors

FastAPI validation errors (422) carry `detail` as a list of `{loc, msg, type}`; every
other error carries `detail` as a string or as the object shown. Device holds, the
sweep, and event delivery never return an error to a caller.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 400 | `Cannot update a <STATUS> reservation` | PATCH on a status other than `ACTIVE` or `PENDING` | RES-PATCH-1 |
| 400 | `end_time must be after start_time` or `end_time must be in the future` | PATCH with a bad end time | RES-PATCH-3 |
| 400 | the plain-string detail create uses for the same condition | PATCH: missing device, mixed types, unavailable device, unreachable edges | RES-PATCH-6, RES-PATCH-7, RES-PATCH-8, RES-PATCH-9 |
| 400 | `{"error": "topology_device_not_member", "device_ids": [...]}` | PATCH device set leaves a canvas device unbooked | RES-PATCH-8 |
| 403 | `You do not have access to one or more requested devices` | a non-admin names an invisible device on create or PATCH | RES-CREATE-7, RES-PATCH-7 |
| 403 | `Only admins can list all reservations` | `GET /?all=true` as a non-admin | RES-LIST-1 |
| 403 | `Only the reservation owner or an admin may set its purpose category` | purpose-category PATCH by a third user | RES-PURPOSE-2 |
| 403 | `Admin or superadmin role required` | a purpose review route as a non-admin | RES-PURPOSE-14 |
| 403 | `Invalid internal token` | an internal route with a wrong token | RES-INTERNAL-1 |
| 404 | `Reservation not found` | `GET /{id}`, `PATCH /{id}`, or release by anyone but the owner; cancel by a non-owner non-admin; any route on an unknown id | RES-VIEW-1, RES-VIEW-2, RES-PATCH-2, RES-CANCEL-1, RES-RELEASE-1, RES-RELEASE-2, RES-PURPOSE-2, RES-PURPOSE-13 |
| 404 | `Reservation not found` | a fork or wiring route by anyone but the owner or an admin | RES-FORK-4 |
| 404 | `Reservation not found` | an internal status lookup or provision result for an unknown id | RES-INTERNAL-2, RES-DYN-6 |
| 404 | `Fork not found` | reading the fork of a non-`ACTIVE` reservation that has none | RES-FORK-5 |
| 409 | `Time conflict: devices [<ids>] already reserved in the requested window` | create overlaps another live booking of an exclusive device | RES-CONFLICT-1 |
| 409 | `Time conflict: devices [<ids>] already reserved in the extended window` | PATCH extension overlaps another live booking | RES-PATCH-4 |
| 409 | `Time conflict: devices [<ids>] already reserved in the requested window` | PATCH-add overlaps another live booking (create's wording) | RES-PATCH-9 |
| 409 | `Reservation changed status during the update (<read> to <now>); nothing was changed` | PATCH lost its status guard to a concurrent transition | RES-PATCH-10 |
| 409 | `Reservation has no suggestion to accept` | accept or dismiss with no suggestion (same text on dismiss) | RES-PURPOSE-10, RES-PURPOSE-11 |
| 409 | `{"error": "not_eligible"}` | Classify now on a row not yet stamped | RES-PURPOSE-13 |
| 409 | `{"error": "already_suggested"}` | Classify now on a row with a suggestion | RES-PURPOSE-13 |
| 409 | `Fork editing requires an ACTIVE reservation` | canvas PUT on a non-`ACTIVE` reservation | RES-FORK-6 |
| 409 | `Fork save requires an ACTIVE reservation` | save on a non-`ACTIVE` reservation | RES-FORK-6 |
| 409 | `{"error": "reservation_not_active"}` | restore on a non-`ACTIVE` reservation | RES-FORK-6 |
| 409 | `Wiring retry requires a provisioned reservation` | wiring retry on `PENDING` or `PENDING_PROVISION` | RES-FORK-18 |
| relayed 4xx | cabling's or execution's `detail`, unwrapped | the upstream refused a forwarded fork or wiring call | RES-FORK-10, RES-FORK-18 |
| 422 | validation list | create schema violation: device cap, empty booking, window, past start, duration, purpose length, more than 50 dynamic requests | RES-CREATE-1 to RES-CREATE-6, RES-DYN-2 |
| 422 | validation list | PATCH with an empty or over-cap `device_ids` | RES-PATCH-6 |
| 422 | validation list | list: unknown sort field or direction, unknown status, naive or bad timestamp, search over 200 characters | RES-LIST-3, RES-LIST-6, RES-LIST-8 |
| 422 | validation list | calendar without `range_start` or `range_end`; internal route without the token header | RES-CAL-1, RES-INTERNAL-1 |
| 422 | `Unknown purpose_category '<value>'; allowed: <list>` | an unknown category on create, purpose-category PATCH, accept, or the list filter | RES-PURPOSE-1, RES-PURPOSE-10, RES-LIST-7 |
| 422 | `Device <id> not found in inventory` | create names a device that does not exist or that the caller cannot see | RES-CREATE-8, RES-CREATE-9 |
| 422 | `All devices must share the same topology type. Found: ...` | create mixes physical and cloud devices | RES-CREATE-10 |
| 422 | `The following devices are not available: <names>` | create that starts now names a device not available | RES-CREATE-11 |
| 422 | `{"error": "topology_device_not_member", "device_ids": [...]}` | create leaves a canvas device unbooked | RES-TOPO-2 |
| 422 | `Topology has unreachable edges in the cabling graph: <edge> (<reason>), ...` | create on a topology with unreachable edges and no route problem | RES-TOPO-3 |
| 422 | `Topology failed validation (no details reported)` | cabling answered `valid: false` with no details | RES-TOPO-3 |
| 422 | `{"error": "topology_routing_intent_invalid", "invalid_routes": [...], "message": "..."}` | create on a topology with invalid routing intent | RES-TOPO-4 |
| 422 | `Template <id> not found in inventory` | a dynamic request names an unknown template | RES-DYN-1 |
| 422 | `The following templates are not dynamic templates: <ids>` | a dynamic request names a non-dynamic template | RES-DYN-1 |
| 422 | `Hypervisor '<name>' is disabled; these dynamic templates cannot be booked until an admin enables it: <template names>` | a dynamic request names a template whose hypervisor is disabled | RES-DYN-10 |
| 422 | `The hypervisor of these dynamic templates no longer exists: <template names>` | the hypervisor a dynamic template names is gone | RES-DYN-10 |
| 422 | `Calendar window cannot exceed <N> days (requested ...)` | calendar window too wide | RES-CAL-2 |
| 503 | `Failed to contact inventory service: ...` | inventory unreachable on create, PATCH, or the dynamic template check | RES-CREATE-9, RES-PATCH-6, RES-DYN-1 |
| 503 | `Failed to contact inventory service while checking the hypervisor of a dynamic template` | the hypervisor check of a dynamic request cannot be answered | RES-DYN-10 |
| 503 | `Could not verify device visibility; reservations were not returned. Retry the request.` | non-admin calendar while the visibility lookup cannot be answered | RES-CAL-3 |
| 503 | `Failed to reserve devices in inventory after retries: ...` | the create-path inventory flip exhausted its attempts | RES-CREATE-15 |
| 503 | `Cabling validation returned <status>: ...` or `Failed to contact cabling service: ...` | topology validation failed on create or PATCH | RES-TOPO-6, RES-PATCH-8 |
| 503 | `{"error": "purpose_classification_disabled"}` | Classify now with the feature off | RES-PURPOSE-13 |
| 503 | cabling's JSON detail, or `Cabling service is unavailable` | cabling answered 5xx to a forwarded fork call | RES-FORK-10 |
| 503 | `Cabling fork-create returned <status>: ...` | a lazy fork create was refused or failed | RES-FORK-5 |
| 503 | `Failed to contact cabling service: ...` or the execution equivalent | the upstream of a fork or wiring route is unreachable | RES-FORK-10, RES-FORK-18 |

Codes that are success but do nothing: cancel on a finished reservation answers 204
(RES-CANCEL-3); release on a non-`ACTIVE` reservation answers 200 with the unchanged
reservation (RES-RELEASE-3); a provision result on a non-`PENDING_PROVISION`
reservation answers 200 with `applied: false` (RES-DYN-6).

## 10. Interactions with other services

Calls into this area are in section 7; events are in section 6.

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out | inventory | `GET /device-groups/visible-devices` (caller's JWT) | non-admin visibility on create, PATCH, calendar | Create and PATCH: the pre-check is skipped, and an invisible device is still refused through the batch fetch below (RES-CREATE-8, RES-PATCH-7). Calendar: fail closed, 503 and no rows (RES-CAL-3) |
| Out | inventory | `POST /devices/batch` (caller's JWT) | device existence, visibility, type, exclusivity, status on create and PATCH | Fail closed: 503 on create and on PATCH device changes; on a PATCH end-time extension every device is treated as exclusive |
| Out | inventory | `GET /templates/{id}` (caller's JWT) | dynamic template check | Fail closed: 503 |
| Out | inventory | `GET /devices/{id}/internal` (internal token) | exclusivity on release and scheduled activation | Fail toward exclusive: an unreadable device is treated as exclusive; a 404 drops it from a release, except on auto-complete (RES-HOLD-5, RES-HOLD-9) |
| Out | inventory | `POST /devices/{id}/status` (internal token) | set `RESERVED` or `AVAILABLE` | Create path: three attempts then `FAILED` and 503. Scheduled activation: three attempts then back to `PENDING`. Releases: logged, terminal status stands. PATCH: after the commit, three attempts, logged, edit stands (RES-PATCH-10) |
| Out | cabling | `POST /topologies/{id}/validate/internal` (internal token) | topology connectivity, membership, routing intent | 404: treated as nothing to validate (RES-TOPO-5). Other error or transport: fail closed, 503 |
| Out | cabling | `POST /internal/forks` (internal token) | fork create at activation, by the sweep, or lazily on read | Activation and sweep: three attempts, logged, reservation stays `ACTIVE`; 409 or 422 final. Lazy read: 503 to the caller |
| Out | cabling | `GET`, `PUT`, `POST` on `/internal/forks/{id}/...` (internal token) | the forwarded fork routes | 4xx relayed; 5xx and transport 503 |
| Out | cabling | `POST /internal/forks/{id}/archive` (internal token) | freeze the as-built record | Three attempts, logged; the sweep retries every tick while cabling reports the fork `ACTIVE` |
| Out | cabling | `POST /internal/forks/{id}/prune-devices` (internal token) | release removed devices' wiring | Retried by the pending-prune marker every tick until converged or terminal |
| Out | cabling | `GET /internal/forks` (internal token, paged by 200) | sweep's active-fork listing | The tick's archive, heal, and missing-fork steps are skipped |
| Out | execution | `GET /internal/reservations/{id}/wiring-status`, `POST .../wiring/retry` (internal token, 30 s) | wiring proxies | 4xx relayed; 5xx and transport 503 |
| Out | ai-orchestrator | `POST /internal/classify-purpose` (internal token, `PURPOSE_CLASSIFY_TIMEOUT_SECONDS`) | purpose suggestion | Never raises; outcome classes in RES-PURPOSE-6 and RES-PURPOSE-7 |
| Out | NATS | `HERD_RESERVATIONS` via the outbox relay | lifecycle events (section 6) | Events wait in the outbox until a NATS connection exists (at-least-once, RES-EVENT-4) |

Utilization reporting additionally calls inventory, auth, execution, and cabling; it
belongs to `operations-and-observability.md`.

## 11. Configuration

All are reservations-service environment variables; [ENV_VARS.md](../ENV_VARS.md) has
the full list.

| Setting | Default | Effect |
|---|---|---|
| `EXPIRATION_INTERVAL_SECONDS` | `60` | Expiration loop period (the dev and test override pins `5`) |
| `RESERVATION_START_GRACE_SECONDS` | `300` | Past-start tolerance, and the scheduled versus immediate boundary |
| `RESERVATION_MAX_DURATION_SECONDS` | `2592000` | Longest window at create; `0` disables |
| `PROVISION_TIMEOUT_SECONDS` | `900` | Deadline for both provisioning backstops; `0` disables both |
| `EXPIRY_REMINDER_LEAD_SECONDS` | `3600` | Reminder lead window; `0` disables |
| `CALENDAR_MAX_SPAN_DAYS` | `366` | Widest calendar window; `0` disables |
| `PURPOSE_CATEGORIES` | `qa_regression,support_case_replication,feature_development,customer_demo_poc,training,performance_benchmark,other` | Category list, comma-separated |
| `PURPOSE_CLASSIFY_INTERVAL_SECONDS` | `60` | Purpose loop period (the dev and test override pins `5`) |
| `PURPOSE_CLASSIFY_BATCH_SIZE` | `20` | Rows classified per tick |
| `PURPOSE_CLASSIFY_MAX_ATTEMPTS` | `3` | Per-row attempt cap for the sweep |
| `PURPOSE_CLASSIFY_TIMEOUT_SECONDS` | `30.0` | Per-call classifier timeout |
| `NATS_STREAM_MAX_AGE_SECONDS` | `604800` | `HERD_RESERVATIONS` retention; `0` means no cap |
| `INTERNAL_API_TOKEN` | empty | Service-to-service token; empty disables every inventory status write, fork call, and archive |

Fixed in code, not configurable: three attempts with 0.5 s initial delay doubling to a
5 s cap for inventory flips, fork create, prune, and archive; 20 s timeout for topology
validation and fork save; 10 s for other inventory and cabling calls; 5 failed ticks
before the missing-fork backstop gives up; 20 pending prunes per tick.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/reservations/tests/` (in-memory SQLite); `frontend/src/test/lib/reservationStatus.test.ts`, `frontend/src/test/lib/reservationBulk.test.ts`, `frontend/src/test/lib/reservationFilters.test.ts` | Advisory locks and `SKIP LOCKED` are no-ops on SQLite, so concurrency below the status CAS is not exercised here |
| Functional (through the service API) | `services/reservations/tests/test_reservations.py`, `test_fork_endpoints.py`, `test_dynamic_requests.py`, `test_reservation_list_filters.py` (httpx against the app); the `*_live_pg.py` suites against a real Postgres: `test_reservation_status_cas_live_pg.py`, `test_reservation_sort_live_pg.py`, `test_reservation_list_filters_live_pg.py`, `test_sweep_claim_cancel_race_live_pg.py` | The live suites run in the `make master` and `make everything` gates |
| Integration (running stack) | `tests/integration/test_reservation_lifecycle.py`, `test_reservation_patch.py`, `test_reservation_list_filters.py`, `test_provisioning_failed.py`, `test_dynamic_resources.py`, `test_reservation_fork_flow.py`, `test_device_set_patch_wiring.py`, `test_purpose_category_flow.py`, `test_purpose_review_flow.py` | `test_overlapping_reservation_is_rejected` accepts 409 or 422; the purpose review flow needs an AI provider and never runs in CI |
| Stress and load | `tests/load/locustfile.py` (`ReservationUser`: list, calendar, create, release) | Create contention on one device is intended; no load test covers the sweep, PATCH, or the fork routes |
| Browser end-to-end | `tests/e2e/test_reservations.py`, `test_reservation_cancel_ui.py`, `test_reservation_detail.py`, `test_reservations_bulk_playwright.py`, `test_reservations_filters_playwright.py`, `test_reservations_sort_playwright.py`, `test_live_edit_reservation_topology.py` | E2E runs nightly and in the `make master` and `make everything` gates, not per pull request |

Not run for this document: nothing here was checked against a running stack. The
live-Postgres suites, the integration suite, and the browser suite were read, not run.
The purpose classification integration test needs an AI provider and never runs in CI,
so RES-PURPOSE-5 to RES-PURPOSE-8 are proven by unit tests only.

## 13. Known limits and gaps

### Open defects

None at present.

### Limits by decision

- An admin may cancel any reservation but may not read by id, edit, or release one they
  do not own (RES-VIEW-2, RES-PATCH-2, RES-RELEASE-2). Recorded in
  [ROLES.md](../ROLES.md) (the reservations rows of the endpoint matrix and the notes
  under it); issue #843 aligned the UI's Release gate to it.
- The same refusal answers 422 on create and 400 on PATCH (missing device, mixed types,
  unavailable device, unreachable edges, canvas device not booked). ROLES.md records the
  split for the canvas membership refusal (section "Reservation Topology Fork"); the
  router maps every other PATCH refusal of that kind the same way.
- A cabling 404 during topology validation is treated as nothing to validate
  (RES-TOPO-5), recorded at the 404 branch of `_validate_topology_connectivity`. The
  topology delete guard of issue #977 bounds it: a topology a `PENDING`,
  `PENDING_PROVISION`, or `ACTIVE` reservation references cannot be deleted, so the 404
  arises only for a topology deleted before the booking is submitted, and that booking's
  fork is an empty canvas.
- A bulk action counts a row the backend answers 200 or 204 for without acting as a
  success (RES-BULK-2). Issue #843 chose per-row calls that the backend's status
  compare-and-swap decides; the page skips finished rows before sending, so this only
  happens to a row that changed after the list loaded.
- The fork refusal set and the missing-fork give-up counters live in process memory and
  reset on restart (RES-FORK-2, RES-FORK-14). Recorded in issue #448 (item 1) and the
  comments on `_fork_membership_refused` and `_FORK_BACKSTOP_MAX_ATTEMPTS`.
- A definitive cabling refusal of a lazy fork create (409) reaches the user as 503
  (RES-FORK-5). Recorded in the docstring of `_lazy_create_reservation_fork`, which
  keeps the generic error mapping on purpose.

### Rules with no test

None: every rule names a test.
