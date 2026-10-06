# Dynamic resources specification

| | |
|---|---|
| Area prefix | `DYN` (used in rule identifiers, for example `DYN-CREATE-1`) |
| Verified at | commit `9562b88e` (`v0.6.0-91-g9562b88e`), 2026-10-06; the execution, reservations, and inventory code is unchanged since `fd589e50` |
| Owning services | execution (`services/execution/`: the `provision_requested` handler, the `dynamic_instances` ledger, teardown, the recipe result rules); the booking surfaces in `frontend/` |
| Other services involved | reservations (the `reservation_dynamic_requests` rows, the `provision_requested` event, the provision-result callback, the timeout backstop), inventory (dynamic templates, the hypervisor registry, the internal device create and delete routes, recipe packages), secrets (the hypervisor credential), the Hypervisor recipe driver run in execution's sandbox |
| Design records | [ADR 0004](../design/0004-dynamic-resources.md) |
| Related guides | [USER_GUIDE.md](../USER_GUIDE.md) (Dynamic resources), [TOPOLOGY_EDITOR.md](../TOPOLOGY_EDITOR.md), [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md), [DRIVERS.md](../DRIVERS.md) (Hypervisor driver contract), [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) (`dynamic_instance_keyed_destroy_failed`), [ROLES.md](../ROLES.md), [ENV_VARS.md](../ENV_VARS.md) |

This document depends on four others. The reservation status machine, the dynamic
request validation at booking, the provision-result callback, and the timeout backstop
are specified in `reservations.md` (rules RES-DYN-1 to RES-DYN-9, RES-SWEEP-6,
RES-SWEEP-8, RES-STATUS-1). The shared event consumer (corroboration gate, heartbeat,
NAK schedule, dead-letter queue) is specified in `provisioning-and-wiring.md` (rules
WIRE-GATE-1 to WIRE-GATE-5, WIRE-CONSUME-6, WIRE-CONSUME-9 to WIRE-CONSUME-13,
WIRE-DISPATCH-2, WIRE-DISPATCH-3). Dynamic templates and the hypervisor registry are in
`inventory.md`, and so are the two inventory routes that materialize and delete an
instance device, `POST /devices/internal` and `DELETE /devices/{device_id}/internal`
(rules INV-DYN-1 to INV-DYN-9, INV-DEL-9, INV-INT-1, INV-STATUS-3); this document
specifies only what execution sends them and how it treats each answer. The driver
sandbox, driver loading, and the package validator's dry run are in
`device-configuration.md`. This document specifies only what is particular to dynamic
instances.

## 1. Purpose

Some lab resources do not exist until someone books them: a virtual machine or a
container that a hypervisor creates for one reservation and destroys when it ends. A
user books such an instance from a dynamic template; HERD runs the template's recipe
against the registered hypervisor, records the instance in a ledger, adds it to
inventory as a device the reservation holds, and destroys it when the reservation
ends. This area does not place instances across hypervisors, enforce capacity or
quotas, network instances to each other, or let a live reservation gain new instances.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). Rules beyond role are numbered in
section 8.

| Actor | May | May not |
|---|---|---|
| User | Book any dynamic template on a new reservation, alone or with devices (DYN-REQ-6); see the booked requests on their reservation; plan instances as canvas placeholders | Change a reservation's dynamic requests after booking (DYN-REQ-3); create a template or register a hypervisor; see the instance's device unless a user group they belong to has permission on the "No Pool" device group (DYN-DEVICE-10) |
| Admin | Everything a user may; author dynamic templates and register hypervisors (`inventory.md`) | Create a dynamic instance device through the admin device routes (`inventory.md`, INV-DYN-8) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Execution: create and delete instance devices in inventory (section 7), read templates, hypervisors, and secret values, post the provision result | Delete a non-dynamic device through the internal delete route (`inventory.md`, INV-DYN-7) |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Dynamic template | A template with `template_type` `dynamic`, naming a recipe driver (`driver_id`) and a hypervisor (`hypervisor_id`); its fields are instance parameters | inventory | `device_templates` (`inventory.md`) |
| Hypervisor | A registered endpoint with a type string and a bare `secret_id` into the secrets service | inventory | `hypervisors` (`inventory.md`) |
| Recipe | A driver package whose connection type is `Hypervisor` | inventory (stored), execution (run) | driver package storage |
| Dynamic request | One requested instance on a booking. Its `id` is the request id; `template_id` is a bare inventory id | reservations | `reservation_dynamic_requests` (`ReservationDynamicRequest` in `services/reservations/app/models/reservation.py`) |
| Request id | The dynamic request's `id`, minted once at booking. Execution keys ledger idempotency and inventory keys device-create idempotency on it; the recipe names the instance from it | reservations | as above |
| Ledger row | Execution's record of one instance: `request_id` (unique), `reservation_id`, `template_id`, `hypervisor_id`, `device_id` (null until materialized), `instance_ref` (null until the recipe returns one), `status`, `error`. All ids are bare, no foreign key. No code path at this commit writes a non-null `error` | execution | `dynamic_instances` (`DynamicInstance` in `services/execution/app/models/dynamic_instance.py`) |
| Instance ref | The hypervisor-side identity `create_instance` returned (for example a VM id) | the hypervisor, recorded by execution | `dynamic_instances.instance_ref` |
| Instance device | The inventory device an instance is materialized as: `CLOUD`, created `RESERVED`, carrying `request_id` | inventory | `devices` |
| Keyed destroy | `destroy_instance` called with `instance_ref=None` and `HERD_request_id` in the context; the recipe finds the instance by the name it derives from the request id | execution (caller), recipe (implementer) | none |
| Placeholder | A canvas node standing for N instances of one dynamic template; never persisted | frontend | the editor's in-memory store |

## 4. State model

This section covers the ledger row's status in execution. The reservation's own
statuses, including how dynamic requests hold a reservation in `PENDING_PROVISION`, are
in `reservations.md` (RES-DYN-4 to RES-DYN-9, RES-SWEEP-6).

**Statuses.**

- `CREATING`: a create was started; an instance may or may not exist. `instance_ref` is
  set once the recipe returned one.
- `ACTIVE`: the instance exists and is materialized as the row's device.
- `DESTROYED`: the recipe destroyed the instance, or confirmed none exists, and any
  device was deleted. Terminal.

`CREATING` and `ACTIVE` are the live statuses (`LIVE_STATUSES`).

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | `CREATING` | `provision_requested` handler (`insert_or_get_creating`) | no row with this request id | nothing | DYN-LEDGER-1, DYN-LEDGER-2 |
| `CREATING`, `ACTIVE` | same status, `instance_ref` recorded | `provision_requested` handler (`set_instance_ref`) | row live | nothing | DYN-LEDGER-3 |
| `CREATING`, `ACTIVE` | `ACTIVE` | `provision_requested` handler (`mark_active`) | row live | nothing | DYN-LEDGER-4 |
| `CREATING`, `ACTIVE` | `DESTROYED` | teardown on a terminal or `updated` event (`mark_destroyed`) | row live, and `instance_ref` and `device_id` equal what teardown read | nothing | DYN-LEDGER-5, DYN-LEDGER-8 |
| `DESTROYED` | anything | nothing | none | nothing | DYN-LEDGER-6 |

Execution publishes no event for any ledger transition.

**Concurrency.** The database is the arbiter. A duplicate insert loses on the unique
`request_id` (DYN-LEDGER-2). Every later write is a compare-and-swap `UPDATE` that
reports whether it matched one row, and a writer that loses acts on that result: a
create that loses undoes what it made (DYN-COMP-1, DYN-COMP-2), and a teardown that loses
re-reads the row and tears down what it now holds (DYN-DESTROY-14). No row lock is
taken, and none of these writes is retried blindly.

**Rules.**

- **DYN-LEDGER-1.** A create inserts a `CREATING` row for its request id; when a row
  already exists, in any status, that row is returned unchanged and nothing is inserted. \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`insert_or_get_creating`, `get_by_request_id`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_ledger_insert_is_idempotent_on_request_id`)
- **DYN-LEDGER-2.** A concurrent insert of the same request id that fails the unique
  constraint rolls back and returns the winner's row; if that re-read finds no row, the
  integrity error is raised. \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`insert_or_get_creating`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_insert_or_get_creating_concurrent_race_returns_winner_row`, `test_insert_or_get_creating_reraises_when_no_row_found_after_integrity_error`)
- **DYN-LEDGER-3.** Recording an `instance_ref` is a compare-and-swap that matches only
  a live row; it returns true when it wrote, and false, writing nothing, for a
  `DESTROYED` or absent row. \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`set_instance_ref`, `LIVE_STATUSES`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_ledger_cas_refuses_destroyed_row`, `test_ledger_cas_wins_on_creating_row`, `test_set_instance_ref_is_noop_for_unknown_request_id`)
- **DYN-LEDGER-4.** Marking a row `ACTIVE` writes `device_id`, `instance_ref`, and the
  status, and is a compare-and-swap with the same live-row guard and return value as
  DYN-LEDGER-3. \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`mark_active`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_ledger_cas_wins_on_creating_row`, `test_ledger_cas_refuses_destroyed_row`, `test_ledger_active_then_destroyed_transition`)
- **DYN-LEDGER-5.** Marking a row `DESTROYED` matches only a live row whose
  `instance_ref` and `device_id` both equal the values the caller passes (a null matches
  only a null); both are required keyword arguments, and the call returns whether it
  wrote. \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`mark_destroyed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_mark_destroyed_cas_matches_status_ref_and_device`, `test_mark_destroyed_is_noop_for_unknown_request_id`)
- **DYN-LEDGER-6.** A `DESTROYED` row is never written again: every ledger update
  matches live statuses only. By decision (issue #896; the ledger section of ADR 0004). \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`LIVE_STATUSES`, `set_instance_ref`, `mark_active`, `mark_destroyed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_ledger_cas_refuses_destroyed_row`, `test_mark_destroyed_cas_matches_status_ref_and_device`)
- **DYN-LEDGER-7.** A reservation's teardown candidates are its `CREATING` and `ACTIVE`
  rows; `DESTROYED` rows are never returned. \
  Enforced in: `services/execution/app/services/dynamic_instance_service.py` (`list_teardown_candidates`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_list_teardown_candidates_excludes_destroyed`)
- **DYN-LEDGER-8.** Teardown marks a row `DESTROYED` only after the recipe's
  `destroy_instance` reported success for it (DYN-RESULT-1) and, when the row has a
  device, the device delete succeeded; a row whose create outcome is unknown is never
  retired without a destroy call. By decision (issue #937; the ledger section of ADR
  0004). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_happy_path_destroys_and_marks_destroyed`, `test_teardown_driver_failure_leaves_active_and_acks`, `test_teardown_creating_without_instance_ref_runs_keyed_destroy`, `test_keyed_destroy_driver_failure_leaves_row_creating_and_logs`)

## 5. API surface

None of its own. Dynamic requests ride the reservations routes: `POST /` accepts
`dynamic_requests` and `GET /`, `GET /{id}` return them (`reservations.md`, section 5);
this document adds the request-level rules DYN-REQ-1 to DYN-REQ-8. Hypervisor and
template administration is in `inventory.md`.

## 6. Events

This area publishes no event of its own. Dead-letter copies of a failed
`provision_requested` go to `herd.reservations.dlq.execution`, which
`provisioning-and-wiring.md` owns (WIRE-CONSUME-9, WIRE-CONSUME-11).

### Events consumed

| Subject | Published by | Consumer | What this area does | Rules |
|---|---|---|---|---|
| `herd.reservations.provision_requested` | reservations | execution | creates every requested instance, materializes each as a device, then posts the provision result | DYN-CREATE-1 to DYN-CREATE-29, DYN-DLQ-1 to DYN-DLQ-4 |
| `herd.reservations.cancelled`, `completed`, `failed` | reservations | execution | tears down every live ledger row of the reservation | DYN-DESTROY-1, DYN-DESTROY-3 to DYN-DESTROY-17 |
| `herd.reservations.updated` | reservations | execution | tears down the live rows whose device is in `removed_device_ids` | DYN-DESTROY-2 |

The `provision_requested` payload keys are in `reservations.md` (section 6); this area
reads `reservation_id`, `user_id`, and `dynamic_requests[].id` and `.template_id`.

## 7. Internal API

None of its own. Execution calls two inventory routes that exist only for this area,
`POST /devices/internal` and `DELETE /devices/{device_id}/internal`; their behavior,
answers, and errors are specified in `inventory.md` (section 7, rules INV-DYN-1 to
INV-DYN-9). What execution sends them and how it treats each answer are DYN-CREATE-21
to DYN-CREATE-23, DYN-DEVICE-9, DYN-DESTROY-6, DYN-DESTROY-11, DYN-DESTROY-12, and
DYN-COMP-3. The reservations callback `POST /internal/{reservation_id}/provision-result`
is specified in `reservations.md` (section 7).

## 8. Features

### 8.1 Booking dynamic instances

**What it does.** When creating a reservation, a user can ask for instances of one or
more dynamic templates, with or without ordinary devices. The reservation then waits
until the instances exist before it goes live.

**Surfaces.** User interface
`frontend/src/components/reservations/CreateReservationModal.tsx` (section 8.9); route
`POST /` with `dynamic_requests` (`reservations.md`, RES-DYN-1 to RES-DYN-4).

**Rules.**

- **DYN-REQ-1.** Each requested instance gets its own request row whose `id` is minted
  at booking; the same `id` appears in the response's `dynamic_requests` and in the
  `provision_requested` payload. \
  Enforced in: `services/reservations/app/models/reservation.py` (`ReservationDynamicRequest`); `services/reservations/app/services/reservation_service.py` (`create_reservation`, `_provision_requested_event`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_stages_provision_requested_payload_exactly`, `test_dynamic_booking_creates_one_row_per_instance`)
- **DYN-REQ-2.** The request rows are written with the booking in every starting status,
  a scheduled `PENDING` booking included. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_scheduled_future_stays_pending`)
- **DYN-REQ-3.** A reservation's dynamic requests never change after booking: the edit
  body has no `dynamic_requests` field and no other path adds or removes a request row. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`ReservationUpdate`) \
  Pinned by: none
- **DYN-REQ-4.** The reservation response carries `dynamic_requests` as a list of
  `{id, template_id}`, empty when the booking has none. \
  Enforced in: `services/reservations/app/schemas/reservation.py` (`ReservationResponse`, `DynamicRequestResponse`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_lands_pending_provision`, `test_zero_dynamic_regression_activates_immediately`)
- **DYN-REQ-5.** Dynamic requests take no part in conflict detection: two dynamic-only
  bookings over the same window are both accepted. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_only_conflict_detection_unaffected`)
- **DYN-REQ-6.** A booking with devices and dynamic requests takes its topology type from
  the devices, and the instances attached on success are `CLOUD` devices (`inventory.md`,
  INV-STATUS-3), so a booking of physical devices plus instances holds devices of both
  types. Known gap, see #1030. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `apply_provision_result`); `services/inventory/app/services/inventory_service.py` (`_insert_dynamic_device`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_booking_stages_provision_requested_payload_exactly`); `services/inventory/tests/test_devices_internal.py` (`test_internal_create_generates_name`)
- **DYN-REQ-7.** The template check at booking (RES-DYN-1) reads each distinct template
  with the caller's JWT and checks only that it exists and is `dynamic`; no device-group
  visibility or ACL check applies to a dynamic request. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_validate_dynamic_requests`); `services/reservations/app/routers/reservations.py` (`create_new_reservation`) \
  Pinned by: none
- **DYN-REQ-8.** A booking with dynamic requests and no exclusive device writes no
  inventory status at booking or when it fails. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`create_reservation`, `apply_provision_result`) \
  Pinned by: `services/reservations/tests/test_dynamic_requests.py` (`test_dynamic_only_booking_flips_no_devices`, `test_dynamic_only_callback_failure_no_device_release`, `test_dynamic_only_timeout_backstop_no_device_release`)

**Out of scope.** The template check's error responses, the 50-request cap, the
`PENDING_PROVISION` gate, and the callback are reservations' (`reservations.md`).
Template and hypervisor authoring is `inventory.md`.

### 8.2 Creating instances

**What it does.** When a reservation with dynamic requests is ready, execution runs the
recipe once per requested instance against the template's hypervisor, materializes each
new instance as an inventory device, and reports the device ids back so the reservation
goes live.

**Surfaces.** `_handle_provision_requested` and `_provision_one_instance` in
`services/execution/app/services/nats_consumer.py`, reached through the shared consumer
(`provisioning-and-wiring.md`, section 8.1); event
`herd.reservations.provision_requested`.

**Rules.**

- **DYN-CREATE-1.** A `provision_requested` event runs only while reservations reports
  the reservation `PENDING_PROVISION` (WIRE-GATE-1); for any other status it is acked
  with no recipe call and no ledger change, logged `nats_event_unverified`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_requested_event_corroborated`, `_EVENT_CORROBORATION_RULES`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_redelivery_after_reservation_left_pending_provision_creates_nothing`, `test_provision_requested_under_pending_provision_still_runs`); `services/execution/tests/test_nats_consumer_event_verification.py` (`test_event_status_table`)
- **DYN-CREATE-2.** When reservations answers that check with a 5xx or not at all, the
  event is nacked with no create attempted (WIRE-GATE-3); once that lasts through the
  fifth delivery the event is dead-lettered and the failure callback (DYN-DLQ-1) is
  attempted, which is lost while reservations is down, so the reservation fails by the
  timeout backstop (RES-SWEEP-6). By decision (CHANGELOG, issue #937; the
  provision_requested gate outage entry in `docs/TROUBLESHOOTING.md`). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_verify_reservation_event`, `process_reservation_message`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_reservations_5xx_on_the_provision_gate_naks`, `test_reservations_outage_through_max_deliver_dead_letters_without_a_create`)
- **DYN-CREATE-3.** The reservation's status is checked once, before the event's first
  request; requests are then processed one at a time in payload order with no further
  status check, so a request that had no ledger row when the reservation's teardown read
  its rows still creates its instance after the reservation ended. Known gap, see
  #1028. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_handle_provision_requested`) \
  Pinned by: none
- **DYN-CREATE-4.** An event with no `reservation_id` is logged and acked with no work. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_handle_provision_requested`) \
  Pinned by: none
- **DYN-CREATE-5.** A request whose row is `ACTIVE` with a device is skipped with no
  recipe call, and its existing device id is reported. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_redelivery_skips_active_row_and_still_reports_success`, `test_provision_over_every_ledger_state`)
- **DYN-CREATE-6.** A request whose row is `DESTROYED` is refused with no recipe call,
  logged `dynamic_instance_resurrection_refused`, and the whole event is abandoned
  (DYN-CREATE-26). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`, `_refuse_resurrection`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_over_every_ledger_state`)
- **DYN-CREATE-7.** A request with no row, a `CREATING` row (with or without an
  `instance_ref`), or an `ACTIVE` row without a device runs the full create again; the
  recipe is required to return the same instance for the same request id (DYN-CONTRACT-3). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_over_every_ledger_state`)
- **DYN-CREATE-8.** The template is read from inventory with the internal token; a
  missing template raises `PermanentEventError`, so the event is dead-lettered on its
  first delivery (WIRE-CONSUME-9). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_recipe_deps`, `_fetch_template`, `_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_missing_template_is_permanent_dlq_with_failure_callback`, `test_missing_template_raises_permanent_directly`)
- **DYN-CREATE-9.** The template's current `hypervisor_id` and that hypervisor's
  `secret_id` are then read from inventory and the secrets service; a missing hypervisor
  or secret raises `PermanentEventError` the same way. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_recipe_deps`, `_fetch_hypervisor`, `_fetch_secret_value`, `_provision_one_instance`) \
  Pinned by: none
- **DYN-CREATE-10.** A 5xx or transport error on any of those reads raises
  `TransientUpstreamError`, so the event is nacked and retried. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_template`, `_fetch_hypervisor`, `_fetch_secret_value`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_transient_5xx_naks`, `test_fetch_hypervisor_5xx_raises_transient`, `test_fetch_secret_value_5xx_raises_transient`)
- **DYN-CREATE-11.** The `CREATING` row is written before the recipe package is loaded
  and before any recipe call, so every later failure leaves a row for teardown. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_broken_package_leaves_row_creating_for_teardown`)
- **DYN-CREATE-12.** A recipe package that cannot load (`DriverPackageError`) raises
  `PermanentEventError`, dead-lettering the whole event on its first delivery with no
  recipe call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_broken_package_raises_permanent_with_diagnosable_message`, `test_broken_package_dlqs_on_first_delivery_with_failure_callback`); `tests/integration/test_dynamic_resources.py` (`test_broken_recipe_package_dead_letters_on_first_delivery`)
- **DYN-CREATE-13.** A recipe package download failure is transient: the event is
  nacked and retried. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`); `services/execution/app/services/driver_loader.py` (`load_driver`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_recipe_download_failure_still_naks`)
- **DYN-CREATE-14.** The recipe runs `login`, `create_instance`, and `logout`, in that
  order, each as its own sandbox call, each recorded as an execution run whose device id
  is the hypervisor id, with the reservation id and the event's `user_id`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`, `_run_recipe_step`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_happy_path_records_runs_ledger_device_and_callback`); `tests/integration/test_dynamic_resources.py` (`test_dynamic_reservation_materializes_device_and_activates`)
- **DYN-CREATE-15.** Every recipe call, in create, teardown, and compensation, runs
  under `RECIPE_TIMEOUT_SECONDS` (default 300) instead of the driver default. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_run_recipe_step`); `services/execution/app/config.py` (`recipe_timeout_seconds`) \
  Pinned by: none
- **DYN-CREATE-16.** `logout` runs after `create_instance` whatever create returned;
  when `login` fails, neither `create_instance` nor `logout` runs and the event is
  nacked. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: none
- **DYN-CREATE-17.** `login` and `logout` are judged by the sandbox's transport flag
  only: a `login` that returns `{"success": false}` without raising counts as a
  successful login. Known gap, see #1027. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`, `_teardown_attempt`, `_destroy_orphaned_instance`); `services/execution/app/services/recipe_result.py` (`recipe_reported_success`) \
  Pinned by: none
- **DYN-CREATE-18.** A `create_instance` that fails (DYN-RESULT-1) leaves the row
  `CREATING` with no `instance_ref` and raises, so the event is nacked and the create is
  retried on each delivery up to the fifth (WIRE-CONSUME-10, WIRE-CONSUME-11). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_create_instance_driver_failure_naks_row_stays_creating`, `test_create_side_effect_then_failure_is_destroyed_by_teardown`); `tests/integration/test_dynamic_resources.py` (`test_create_failure_lands_failed_with_no_orphans`)
- **DYN-CREATE-19.** A `create_instance` that reports success without a usable
  `instance_ref` (DYN-RESULT-2) is a failed create: logged
  `dynamic_instance_create_missing_ref` with the request and reservation ids, the row
  stays `CREATING` with no ref, no device is created, and `PermanentEventError`
  dead-letters the event on its first delivery. By decision (issue #937 review;
  CHANGELOG). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_create_success_without_instance_ref_is_a_failed_create`, `test_create_success_without_instance_ref_dead_letters_on_first_delivery`)
- **DYN-CREATE-20.** A successful create records its `instance_ref` on the row
  (DYN-LEDGER-3) before the inventory device is created, so a later failure leaves an
  addressable instance for teardown. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_between_instance_ref_and_active_flip_leaves_no_orphan`)
- **DYN-CREATE-21.** The instance is materialized through inventory
  `POST /devices/internal` with the body `template_id`, `reservation_id`, `field_data`
  (the create result's `field_data`, or an empty object), and `request_id`, and no
  `name`, so inventory generates the name (`inventory.md`, INV-DYN-2). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_create_dynamic_device`, `_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_create_dynamic_device_maps_status_codes`, `test_provision_happy_path_records_runs_ledger_device_and_callback`)
- **DYN-CREATE-22.** A 5xx or transport error from that call raises
  `TransientUpstreamError`; a 201 returns the device, whether inventory created it or
  returned the one the request id already has (`inventory.md`, INV-DYN-4); any other
  status, a 200 or a 409 included, returns nothing. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_create_dynamic_device`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_create_dynamic_device_maps_status_codes`, `test_create_dynamic_device_raises_on_transport_error`)
- **DYN-CREATE-23.** When inventory refuses the device create, the handler raises
  `PermanentEventError`; the row keeps its `instance_ref` in `CREATING`, so teardown
  destroys the instance by ref once the reservation fails. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: none
- **DYN-CREATE-24.** After the device exists the row is marked `ACTIVE` with the device
  id (DYN-LEDGER-4) and the device id is the request's result. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_happy_path_records_runs_ledger_device_and_callback`)
- **DYN-CREATE-25.** Only when every request returned a device id does execution post
  the success callback `{succeeded: true, device_ids, error: null}`, with up to three
  attempts and then a logged error; a callback that never lands is covered by the
  timeout backstop (RES-SWEEP-6). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_handle_provision_requested`, `_post_provision_result_best_effort`, `_post_provision_result`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_happy_path_records_runs_ledger_device_and_callback`, `test_callback_retries_then_succeeds`, `test_callback_persistent_failure_is_swallowed`, `test_post_provision_result_posts_body_and_raises_for_status`)
- **DYN-CREATE-26.** A request that is refused (DYN-CREATE-6) or undone (DYN-COMP-1,
  DYN-COMP-2, DYN-COMP-4) abandons the event: no callback is posted,
  `dynamic_provision_abandoned` is logged, and the event is acked. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_handle_provision_requested`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_provision_over_every_ledger_state`, `test_teardown_between_create_and_instance_ref_destroys_created_instance`)
- **DYN-CREATE-27.** Instances created earlier in an event are not undone when a later
  request fails; their rows stay `ACTIVE` for a redelivery to reuse (DYN-CREATE-5) or for
  teardown. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_handle_provision_requested`) \
  Pinned by: none
- **DYN-CREATE-28.** An abandoned event processes none of its remaining requests. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_handle_provision_requested`) \
  Pinned by: none
- **DYN-CREATE-29.** The hypervisor's `enabled` flag is read by neither the booking nor
  the create: a template whose hypervisor is disabled is booked and its instances are
  created like any other. Known gap, see #1033. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_recipe_deps`); `services/reservations/app/services/reservation_service.py` (`_validate_dynamic_requests`) \
  Pinned by: none

**Out of scope.** The consumer's ack, nak, and dead-letter mechanics
(`provisioning-and-wiring.md`); what reservations does with the callback (RES-DYN-6 to
RES-DYN-9); driver loading and the sandbox (`device-configuration.md`).

### 8.3 The recipe context

**What it does.** Every recipe call receives the instance's parameters, the
hypervisor's address and credentials, and the ids that identify the request, without
the credentials leaking into the process environment or the run records.

**Surfaces.** `_build_recipe_context` in
`services/execution/app/services/nats_consumer.py`.

**Rules.**

- **DYN-CTX-1.** Each field of the dynamic template's sections becomes
  `HERD_<key>` valued from the field's `default`; a field without a `key` is skipped. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_build_recipe_context`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_build_recipe_context_carries_hypervisor_and_ids`, `test_build_recipe_context_skips_field_with_no_key`)
- **DYN-CTX-2.** The context carries `HERD_hypervisor_endpoint`,
  `HERD_hypervisor_type`, `HERD_request_id`, `HERD_reservation_id`, and `HERD_user_id`
  (the event's `user_id`, the reservation owner). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_build_recipe_context`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_build_recipe_context_carries_hypervisor_and_ids`)
- **DYN-CTX-3.** Every key of the hypervisor's secret value becomes
  `HERD_secret_<key>` and is passed as a password key, so it reaches the recipe through
  the context file and never through the child process environment. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_build_recipe_context`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_recipe_context_secret_keys_excluded_from_child_env`)
- **DYN-CTX-4.** Run records store the context with every secret key and every
  template field of type `password` replaced by `***REDACTED***`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`, `_teardown_attempt`); `services/execution/app/services/execution_service.py` (`redact_context_for_logging`, `extract_password_keys`) \
  Pinned by: none (issue #1032)
- **DYN-CTX-5.** Teardown and the compensating destroy build the same context, so the
  keyed destroy receives `HERD_request_id`; teardown reads the template by the row's
  `template_id` and the hypervisor by the row's `hypervisor_id`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`, `_fetch_recipe_deps`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_creating_without_instance_ref_runs_keyed_destroy`, `test_compensation_without_instance_ref_runs_keyed_destroy`)

**Out of scope.** How the sandbox writes the context file and applies resource limits
(`device-configuration.md`).

### 8.4 Judging a recipe's result

**What it does.** HERD decides whether a hypervisor instance was really created or
destroyed by the recipe's own answer, and by a stricter rule than for physical drivers,
because an instance that cannot be proven created must not be recorded as if it were.

**Surfaces.** `services/execution/app/services/recipe_result.py`, shared by the
consumer and the package validator.

**Rules.**

- **DYN-RESULT-1.** A `create_instance` or `destroy_instance` result succeeds only when
  the sandbox call completed and the recipe returned an object whose `success` is true;
  a missing `success` key, or no object at all, is a failure. \
  Enforced in: `services/execution/app/services/recipe_result.py` (`recipe_reported_success`); `services/execution/app/services/execution_service.py` (`driver_result_failed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_bare_data_output_diverges_on_missing_success_key`, `test_output_none_diverges_on_missing_success_key`, `test_explicit_output_failure_agrees_on_both_helpers`, `test_explicit_output_success_agrees_on_both_helpers`, `test_transport_failure_agrees_on_both_helpers`)
- **DYN-RESULT-2.** A successful create's `instance_ref` must be a string with at least
  one non-space character; anything else counts as no ref. \
  Enforced in: `services/execution/app/services/recipe_result.py` (`created_instance_ref`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_create_success_without_instance_ref_is_a_failed_create`)
- **DYN-RESULT-3.** The package validator judges `create_instance` and both
  `destroy_instance` steps with the same two functions. \
  Enforced in: `services/execution/app/services/package_validator.py` (`recipe_reported_success`, `created_instance_ref`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_validator_shares_the_consumer_predicates`, `test_create_that_the_consumer_would_reject_fails_validation`, `test_destroy_without_a_success_key_fails_validation`)
- **DYN-RESULT-4.** A run record's status is `SUCCESS` whenever the sandbox call
  completed, also when the recipe reported failure; the recipe's verdict is in the
  stored output. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_run_recipe_step`) \
  Pinned by: none

**Out of scope.** The validator's dry-run lifecycle (`device-configuration.md`); the
physical drivers' looser rule (`provisioning-and-wiring.md`).

### 8.5 The instance as an inventory device

**What it does.** A created instance becomes an ordinary inventory device so the
reservation can hold it and the rest of HERD can address it; the device is deleted when
the instance is destroyed.

**Surfaces.** `_create_dynamic_device` and `_delete_dynamic_device` in
`services/execution/app/services/nats_consumer.py`, calling inventory's
`POST /devices/internal` and `DELETE /devices/{device_id}/internal` (section 7). What
those routes do (the generated name, the `No Pool` group, `field_data` validation, the
`RESERVED` status, their 422 and 409 answers, and the delete's refusal of a
non-dynamic device) is specified in `inventory.md` (INV-DYN-1 to INV-DYN-9, INV-DEL-9,
INV-STATUS-3); this section keeps execution's side and the consequences for the
reservation, which is why its numbering starts at DYN-DEVICE-9.

**Rules.**

- **DYN-DEVICE-9.** Execution treats a delete answer of 204 or 404 as done (404 meaning
  already gone), raises `TransientUpstreamError` on a 5xx or transport error, and treats
  any other answer, a 409 included, as not deleted. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_delete_dynamic_device`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_delete_dynamic_device_maps_status_codes`, `test_delete_dynamic_device_raises_on_transport_error`)
- **DYN-DEVICE-10.** The instance device joins the "No Pool" device group like every
  new device (`inventory.md`, INV-DYN-6), so a non-admin owner sees it, and may name it
  in a reservation edit (RES-PATCH-7), only when one of their user groups has permission
  on that group. Known gap, see #1030. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`); `services/inventory/app/services/device_group_service.py` (`add_device_to_no_pool`, `get_visible_device_ids`); `services/reservations/app/routers/reservations.py` (`update_reservation_by_id`) \
  Pinned by: none
- **DYN-DEVICE-11.** Reservations treats an instance device like any exclusive device
  when the reservation ends or the device is removed: it is set `AVAILABLE`
  (RES-HOLD-5, RES-PATCH-10) independently of execution's delete, so it is `AVAILABLE`
  in inventory from that release until the delete, and stays so when teardown leaves its
  row live. \
  Enforced in: `services/reservations/app/services/reservation_service.py` (`_release_exclusive_devices_best_effort`); `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: none
**Out of scope.** The admin device routes, device groups, and visibility
(`inventory.md`, `identity-and-access.md`).

### 8.6 Undoing a create that lost to teardown

**What it does.** If a reservation ends while one of its instances is still being
created, the create notices it lost the race and destroys what it just made, so no
instance or device is left behind without a ledger row.

**Surfaces.** `_destroy_orphaned_instance` and the compare-and-swap branches of
`_provision_one_instance` in `services/execution/app/services/nats_consumer.py`.

**Rules.**

- **DYN-COMP-1.** When recording the `instance_ref` loses because teardown retired the
  row, the create destroys the instance by that ref, creates no device, logs
  `dynamic_instance_create_lost_to_teardown`, and abandons the event (DYN-CREATE-26). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`, `_destroy_orphaned_instance`, `_log_lost_to_teardown`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_between_create_and_instance_ref_destroys_created_instance`)
- **DYN-COMP-2.** When the `ACTIVE` flip loses, the create deletes the device it just
  created, destroys the instance by ref, logs `dynamic_instance_create_lost_to_teardown`,
  and abandons the event. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`, `_delete_dynamic_device`, `_destroy_orphaned_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_between_instance_ref_and_active_flip_leaves_no_orphan`)
- **DYN-COMP-3.** In that branch the device delete's answer is not checked: an answer
  other than 204 or 404 still continues to the destroy and the abandon, and a 5xx or
  transport error raises before the destroy, so the event is nacked. Known gap, see
  #1028. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: none
- **DYN-COMP-4.** A create that failed or returned no ref re-reads its row; when
  teardown retired it meanwhile, the create runs a keyed compensating destroy, logs
  `dynamic_instance_create_lost_to_teardown`, and abandons the event instead of raising. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_failed_create_after_teardown_retired_the_row_is_compensated`)
- **DYN-COMP-5.** The compensating destroy runs `login`, then `destroy_instance` with
  the ref or, with none, `instance_ref=None`, then `logout`; it logs
  `dynamic_instance_compensated` on success and `dynamic_instance_compensation_failed`
  otherwise, and never raises. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_destroy_orphaned_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_compensation_without_instance_ref_runs_keyed_destroy`, `test_compensation_keyed_destroy_failure_is_logged_not_raised`, `test_failed_compensating_destroy_is_logged_not_raised`)
- **DYN-COMP-6.** The compensating destroy often repeats a destroy teardown already
  made; that is safe only because `destroy_instance` must be idempotent. By decision
  (Determinism contract in [DRIVERS.md](../DRIVERS.md); the comment at the `ACTIVE`-flip
  branch of `_provision_one_instance`). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_between_instance_ref_and_active_flip_leaves_no_orphan`)
- **DYN-COMP-7.** When the compensation's `login` fails, neither `destroy_instance` nor
  `logout` runs, and `dynamic_instance_compensation_failed` is logged. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_destroy_orphaned_instance`) \
  Pinned by: none

**Out of scope.** Instances the recipe created under a name not derived from the request
id: HERD cannot find them (DYN-CONTRACT-3).

### 8.7 Tearing instances down

**What it does.** When a reservation ends, or a user removes an instance's device from
a live reservation, execution destroys the instance on the hypervisor and deletes its
device. When it cannot prove the instance is gone, it keeps the ledger row as a record
that the instance may still exist and tells the operator.

**Surfaces.** `_execute_dynamic_teardown`, `_teardown_one_instance`, and
`_teardown_attempt` in `services/execution/app/services/nats_consumer.py`, called from
`handle_reservation_event` after the wiring teardown (WIRE-DISPATCH-3). Operator
guidance: [TROUBLESHOOTING.md](../TROUBLESHOOTING.md).

**Rules.**

- **DYN-DESTROY-1.** `reservation.cancelled`, `completed`, and `failed` tear down every
  live row of the reservation (DYN-LEDGER-7). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`, `DYNAMIC_TEARDOWN_EVENTS`, `_execute_dynamic_teardown`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_handle_cancelled_event_dispatches_ledger_teardown`); `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_happy_path_destroys_and_marks_destroyed`); `tests/integration/test_dynamic_resources.py` (`test_cancel_tears_down_the_dynamic_instance`)
- **DYN-DESTROY-2.** `reservation.updated` tears down only the live rows whose device
  id is in `removed_device_ids`; a row with no device is never selected this way. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`handle_reservation_event`, `_execute_dynamic_teardown`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_updated_only_removed_devices`); `services/execution/tests/test_nats_consumer.py` (`test_updated_removed_devices_drive_dynamic_teardown_only`)
- **DYN-DESTROY-3.** With no candidate row, teardown makes no call. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_execute_dynamic_teardown`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_destroyed_row_is_noop`)
- **DYN-DESTROY-4.** For each row, teardown reads the row's template, hypervisor, and
  secret, loads the recipe, and runs `login`, `destroy_instance`, and `logout`; `logout`
  runs whatever `destroy_instance` returned. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_happy_path_destroys_and_marks_destroyed`)
- **DYN-DESTROY-5.** `destroy_instance` always receives the `instance_ref` keyword: the
  row's ref, or `None` for a row without one in any status (the keyed destroy, with
  `HERD_request_id` in the context). There is no capability flag, by decision
  ([DRIVERS.md](../DRIVERS.md), Determinism contract; CHANGELOG, issue #937). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_happy_path_destroys_and_marks_destroyed`, `test_teardown_creating_without_instance_ref_runs_keyed_destroy`, `test_keyed_destroy_on_active_row_without_ref_destroys_and_deletes_device`); `tests/integration/test_dynamic_resources.py` (`test_failed_create_is_destroyed_by_keyed_teardown`)
- **DYN-DESTROY-6.** After a successful destroy, a row with a device has the device
  deleted through the internal route (DYN-DEVICE-9); then the row is marked `DESTROYED`
  against the snapshot teardown read (DYN-LEDGER-5). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_happy_path_destroys_and_marks_destroyed`, `test_keyed_destroy_on_active_row_without_ref_destroys_and_deletes_device`); `tests/integration/test_dynamic_resources.py` (`test_cancel_tears_down_the_dynamic_instance`)
- **DYN-DESTROY-7.** A missing template, hypervisor, or secret, a recipe that will not
  load for any reason, a failed `login`, a failed or raising `destroy_instance`, or a
  device delete that answers neither 204 nor 404 leaves the row in its status and the
  event is acked; nothing is raised. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_driver_failure_leaves_active_and_acks`, `test_keyed_destroy_that_cannot_run_leaves_row_creating`, `test_keyed_destroy_login_failure_leaves_row_creating`, `test_legacy_recipe_keyed_destroy_raises_row_stays_creating`); `tests/integration/test_dynamic_resources.py` (`test_failed_keyed_destroy_leaves_ledger_row_creating`)
- **DYN-DESTROY-8.** A recipe package download failure during teardown is handled as in
  DYN-DESTROY-7 (row left live, event acked), unlike the create path, which retries it
  (DYN-CREATE-13). Known gap, see #1029. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: none
- **DYN-DESTROY-9.** A row without a ref left live logs
  `dynamic_instance_keyed_destroy_failed` with `request_id`, `reservation_id`,
  `ledger_status`, and a `reason` of `recipe_config_missing`, `recipe_load_failed`,
  `login_failed`, or `destroy_failed`, and never the driver's text. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`, `_log_keyed_destroy_failed`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_keyed_destroy_driver_failure_leaves_row_creating_and_logs`, `test_keyed_destroy_that_cannot_run_leaves_row_creating`, `test_keyed_destroy_login_failure_leaves_row_creating`, `test_legacy_recipe_keyed_destroy_raises_row_stays_creating`)
- **DYN-DESTROY-10.** A row with a ref left live logs a plain error with no fixed log
  action. By decision (docstring of
  `test_by_ref_destroy_failure_does_not_emit_the_keyed_action`). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_by_ref_destroy_failure_does_not_emit_the_keyed_action`)
- **DYN-DESTROY-11.** A device delete that fails after a successful destroy logs a plain
  error with no fixed log action, for a row without a ref too. Known gap, see #1027. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: none
- **DYN-DESTROY-12.** A 5xx or transport error deleting the device after a successful
  destroy raises `TransientUpstreamError`, so the event is nacked and the row is left as
  it was. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`, `_delete_dynamic_device`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_transient_delete_raises`)
- **DYN-DESTROY-13.** When the `DESTROYED` compare-and-swap loses, teardown re-reads the
  row: a row that is gone or no longer live ends the loop, otherwise it tears down what
  the row now holds. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_one_instance`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_loses_cas_to_a_landing_create_then_destroys_what_it_holds`)
- **DYN-DESTROY-14.** After three passes that each lose the compare-and-swap, the row is
  left live and `dynamic_instance_teardown_contended` is logged with the request id,
  reservation id, ledger status, and attempt count. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_one_instance`, `DYNAMIC_TEARDOWN_MAX_ATTEMPTS`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_that_keeps_losing_the_cas_leaves_the_row_live`)
- **DYN-DESTROY-15.** Nothing retries a row teardown left live on a timer; only a
  redelivered or re-published terminal event runs teardown again, and it does run again,
  since recipe steps are not deduplicated. By decision (`docs/TROUBLESHOOTING.md`,
  `dynamic_instance_keyed_destroy_failed`; issue #937, option B not taken). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_execute_dynamic_teardown`, `_run_recipe_step`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_teardown_rerun_retries_a_failed_keyed_destroy`)
- **DYN-DESTROY-16.** Teardown runs as the event's `user_id`, and its run records carry
  the row's `hypervisor_id` as device id. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_teardown_attempt`) \
  Pinned by: none
- **DYN-DESTROY-17.** A 5xx or transport error reading the row's template, hypervisor,
  or secret during teardown raises `TransientUpstreamError` the same way. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_fetch_recipe_deps`, `_teardown_attempt`) \
  Pinned by: none

**Out of scope.** The wiring freeze and wiring teardown that run first
(`provisioning-and-wiring.md`); the device release reservations makes
(`reservations.md`).

### 8.8 A provisioning event that cannot be processed

**What it does.** When execution gives up on a `provision_requested` event, it tells
reservations that provisioning failed so the reservation fails at once instead of
waiting for the timeout, and leaves the cleanup to the normal teardown.

**Surfaces.** `_maybe_post_provision_failure` in
`services/execution/app/services/nats_consumer.py`, called from
`process_reservation_message` on both dead-letter paths.

**Rules.**

- **DYN-DLQ-1.** A dead-lettered `provision_requested` (first-delivery permanent error,
  or the fifth delivery) posts one failure callback
  `{succeeded: false, device_ids: [], error: "provisioning failed: <ClassName>"}`; it is
  attempted once and any error is logged and swallowed. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`, `_maybe_post_provision_failure`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_missing_template_is_permanent_dlq_with_failure_callback`, `test_maybe_post_provision_failure_posts_failed_callback`, `test_maybe_post_provision_failure_swallows_callback_error`, `test_reservations_outage_through_max_deliver_dead_letters_without_a_create`); `services/execution/tests/test_nats_consumer.py` (`test_process_message_permanent_error_sanitizes_provision_failure_reason`, `test_process_message_max_deliver_sanitizes_provision_failure_reason`)
- **DYN-DLQ-2.** The reason names only the exception class; the exception's text stays
  in this service's log record. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`process_reservation_message`) \
  Pinned by: `services/execution/tests/test_nats_consumer.py` (`test_process_message_permanent_error_sanitizes_provision_failure_reason`); `services/execution/tests/test_nats_consumer_dynamic.py` (`test_broken_package_dlqs_on_first_delivery_with_failure_callback`)
- **DYN-DLQ-3.** No callback is posted for any other dead-lettered event, or for a
  `provision_requested` without `reservation_id`. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_maybe_post_provision_failure`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_maybe_post_provision_failure_ignores_other_events`, `test_maybe_post_provision_failure_ignores_missing_reservation_id`)
- **DYN-DLQ-4.** Dead-lettering destroys nothing: rows stay as they are until the
  `reservation.failed` that the callback causes tears them down. By decision (docstring
  of `_maybe_post_provision_failure`: tearing down here would race the teardown
  handler). \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_maybe_post_provision_failure`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_broken_package_leaves_row_creating_for_teardown`); `tests/integration/test_dynamic_resources.py` (`test_failed_create_is_destroyed_by_keyed_teardown`)

**Out of scope.** The dead-letter publish itself (`provisioning-and-wiring.md`,
WIRE-CONSUME-9, WIRE-CONSUME-11).

### 8.9 The Hypervisor recipe contract

**What it does.** A recipe is an ordinary driver package of connection type
`Hypervisor`. HERD checks only that it has the right methods; the rest of the contract
is a set of obligations on the recipe author that make redelivery and teardown safe.

**Surfaces.** `REQUIRED_METHODS` in `services/execution/app/services/driver_loader.py`;
the contract text in [DRIVERS.md](../DRIVERS.md) (Hypervisor driver contract); the
reference recipe `drivers/mock_hypervisor/`.

**Rules.**

- **DYN-CONTRACT-1.** A `Hypervisor` package must define `login`, `logout`,
  `create_instance`, `destroy_instance`, and `status`; a package missing one fails
  validation. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`REQUIRED_METHODS`, `validate_driver`) \
  Pinned by: `services/execution/tests/test_nats_consumer_dynamic.py` (`test_required_methods_registers_hypervisor`, `test_valid_hypervisor_recipe_passes_validation`, `test_recipe_missing_destroy_instance_fails_validation`)
- **DYN-CONTRACT-2.** The dynamic flows never call `status`, and call `create_instance`
  with no keyword arguments. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: none
- **DYN-CONTRACT-3.** A recipe must name its instance from `HERD_request_id`, reuse an
  instance that already carries that name, resolve a keyed destroy by the same name, and
  report success for an instance that does not exist; HERD cannot check this and relies
  on it (DYN-CREATE-7, DYN-COMP-6, DYN-DESTROY-5). By decision
  ([DRIVERS.md](../DRIVERS.md), Determinism contract). \
  Enforced in: `drivers/mock_hypervisor/driver.py` (`_instance_name`, `create_instance`, `destroy_instance`) \
  Pinned by: `tests/unit/test_mock_hypervisor_driver.py` (`test_create_instance_is_deterministic_per_request_id`, `test_keyed_destroy_derives_the_create_name_from_request_id`, `test_destroy_instance_succeeds_and_is_idempotent`)
- **DYN-CONTRACT-4.** The reference recipe names its instance `mock-vm-<request id>`
  and returns `{success, instance_ref, field_data}`, where `field_data` holds a
  management address derived from the request id and the `HERD_image` default when one
  is set; without `HERD_request_id` it reports failure. \
  Enforced in: `drivers/mock_hypervisor/driver.py` (`create_instance`, `_derive_mgmt_address`) \
  Pinned by: `tests/unit/test_mock_hypervisor_driver.py` (`test_create_instance_success_shape`, `test_create_instance_echoes_template_image_field`, `test_create_instance_without_request_id_reports_failure`)
- **DYN-CONTRACT-5.** The reference recipe's keyed destroy reports
  `{success: true, instance_ref: <derived name>, keyed: true}`, a destroy by ref carries
  no `keyed` flag, and a keyed destroy without `HERD_request_id` reports failure. \
  Enforced in: `drivers/mock_hypervisor/driver.py` (`destroy_instance`) \
  Pinned by: `tests/unit/test_mock_hypervisor_driver.py` (`test_keyed_destroy_derives_the_create_name_from_request_id`, `test_destroy_by_ref_is_not_flagged_keyed`, `test_keyed_destroy_without_request_id_reports_failure`)
- **DYN-CONTRACT-6.** The reference recipe reads the test seams `HERD_mock_fail_actions`
  (named actions return `{success: false}`), `HERD_mock_raise_actions` (named actions
  raise), and `HERD_mock_sleep_ms` (every call sleeps), from template field defaults. \
  Enforced in: `drivers/mock_hypervisor/driver.py` (`_maybe_inject`) \
  Pinned by: `tests/unit/test_mock_hypervisor_driver.py` (`test_fail_injection_returns_unsuccessful_result`, `test_raise_injection_raises`, `test_sleep_injection_delays_each_call`, `test_keyed_destroy_honors_dry_run_and_fail_injection`)
- **DYN-CONTRACT-7.** Because of DYN-CREATE-17, naming `login` in
  `HERD_mock_fail_actions` does not fail a create or a teardown; only
  `HERD_mock_raise_actions` makes `login` fail there. Known gap, see #1027. \
  Enforced in: `drivers/mock_hypervisor/driver.py` (`_maybe_inject`); `services/execution/app/services/nats_consumer.py` (`_provision_one_instance`) \
  Pinned by: none (issue #1032)
- **DYN-CONTRACT-8.** The reference recipe declares `supports_dry_run`, flags every
  result `simulated` under dry run, and its `status` never raises. \
  Enforced in: `drivers/mock_hypervisor/driver.py` (`_flag_simulated`, `status`); `drivers/mock_hypervisor/driver_metadata.json` (`supports_dry_run`) \
  Pinned by: `tests/unit/test_mock_hypervisor_driver.py` (`test_metadata_declares_hypervisor_and_dry_run`, `test_dry_run_flags_simulated`, `test_status_reports_reachable_and_never_raises`)

**Out of scope.** The stricter rules for AI-drafted recipes and the validator's dry run
(`ai-features.md`, `device-configuration.md`).

### 8.10 Booking instances in the browser

**What it does.** In the Create Reservation dialog a user adds rows of "template and
count"; in the topology editor a user can drag a dynamic template onto the canvas as a
placeholder, set its count, and reserve the topology with those instances included.
Admins author dynamic templates in the template editor.

**Surfaces.** `frontend/src/components/reservations/CreateReservationModal.tsx`,
`frontend/src/components/reservations/ReservationDetailModal.tsx`,
`frontend/src/components/equipment-browser/EquipmentBrowser.tsx`,
`frontend/src/components/topology-editor/nodes/DynamicPlaceholderNode.tsx`,
`frontend/src/pages/TopologyEditorPage.tsx`, `frontend/src/lib/canvasNodes.ts`,
`frontend/src/stores/topologyStore.ts`, `frontend/src/pages/TemplateEditorPage.tsx`,
`frontend/src/pages/TemplatesPage.tsx`.

**Rules.**

- **DYN-UI-1.** The dialog's Dynamic instances block adds rows of template and count;
  with no dynamic template the add button is disabled and "No dynamic templates
  available" is shown. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`CreateReservationModal`) \
  Pinned by: `frontend/src/test/components/CreateReservationModal.test.tsx` (`disables the add button and shows a hint when no dynamic templates exist`)
- **DYN-UI-2.** A row's count is clamped to whole numbers from 1 to 50, prefilled counts
  included. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`clampCount`, `MAX_DYNAMIC_REQUESTS`) \
  Pinned by: `frontend/src/test/components/CreateReservationModal.test.tsx` (`clamps the instance count to the 1..50 backend bounds`, `clamps an out-of-bounds prefilled count`)
- **DYN-UI-3.** When the rows total more than 50 instances, an alert reads "A
  reservation can include at most 50 dynamic instances" and Create is disabled. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`CreateReservationModal`, `handleSubmit`) \
  Pinned by: `frontend/src/test/components/CreateReservationModal.test.tsx` (`disables Create and warns when the total dynamic requests exceed 50`)
- **DYN-UI-4.** Create is disabled while the dialog has no device and no dynamic row. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`CreateReservationModal`) \
  Pinned by: `frontend/src/test/components/CreateReservationModal.test.tsx` (`disables Create when there are no devices and no dynamic requests`)
- **DYN-UI-5.** On submit each row becomes `count` repeated `{template_id}` items, and
  `dynamic_requests` is left out of the body when there are none. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`handleSubmit`) \
  Pinned by: `frontend/src/test/components/CreateReservationModal.test.tsx` (`submits a dynamic-only booking with empty device_ids and expanded dynamic_requests`, `submits a mixed booking with both device_ids and dynamic_requests`); `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`omits dynamic_requests entirely when the canvas has no placeholders`)
- **DYN-UI-6.** `initialDynamicEntries` prefills the rows when the dialog mounts. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`CreateReservationModal`) \
  Pinned by: `frontend/src/test/components/CreateReservationModal.test.tsx` (`prefills dynamic entries from initialDynamicEntries and expands them on submit`)
- **DYN-UI-7.** The reservation detail shows "Dynamic instances (N)" with one line per
  template and its count, the template's name when it resolves and the first eight
  characters of its id otherwise; with no request the section is absent. \
  Enforced in: `frontend/src/components/reservations/ReservationDetailModal.tsx` (`groupDynamicRequests`, `ReservationDetailModal`) \
  Pinned by: `frontend/src/test/components/ReservationDetailModal.test.tsx` (`renders dynamic requests grouped by template with resolved names (issue #473)`, `renders no dynamic instances section when the field is absent`, `renders no dynamic instances section when the array is empty`)
- **DYN-UI-8.** The equipment browser lists dynamic templates in their own collapsible
  section, absent when there are none; dragging one sets the
  `application/herd-dynamic-template` payload `{id, name, icon}`. \
  Enforced in: `frontend/src/components/equipment-browser/EquipmentBrowser.tsx` (`DynamicTemplateCard`, `EquipmentBrowser`) \
  Pinned by: `frontend/src/test/components/EquipmentBrowser.test.tsx` (`renders dynamic templates in their own section as drag sources`, `collapses and re-expands the dynamic templates section`, `omits the dynamic templates section when no dynamic templates exist`)
- **DYN-UI-9.** Dropping a dynamic template on the canvas adds one placeholder with
  count 1; dropping a template that already has a placeholder does nothing. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`onDrop`, `dynamicPrefill`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`dropping a dynamic template creates one placeholder with count 1; re-dropping the same template is a no-op`)
- **DYN-UI-10.** Dropping a dynamic template on a live reservation's canvas is refused
  with the toast "Dynamic instances are set when the reservation is created". \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`onDrop`) \
  Pinned by: none
- **DYN-UI-11.** A placeholder shows the template, a DYNAMIC tag, and a count field
  clamped to 1 to 50 that writes the count back to the canvas store. \
  Enforced in: `frontend/src/components/topology-editor/nodes/DynamicPlaceholderNode.tsx` (`DynamicPlaceholderNode`, `clampCount`); `frontend/src/stores/topologyStore.ts` (`setDynamicPlaceholderCount`) \
  Pinned by: `frontend/src/test/components/DynamicPlaceholderNode.test.tsx` (`renders the template name, DYNAMIC tag, and dashed purple ghost styling`, `writes an edited count back to the store node`, `clamps the count to the 1..50 backend bounds`)
- **DYN-UI-12.** A connection to or from a placeholder is refused with the toast
  "Dynamic placeholders have no ports until the reservation activates", and no edge is
  added. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`isValidConnection`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`refuses a connection to a placeholder with a toast and creates no edge`)
- **DYN-UI-13.** Saving the parent topology leaves placeholder nodes out of
  `canvas_data` and keeps them on the open canvas; the success toast then reads
  "Topology saved. Dynamic placeholders are not saved; reserve to keep them". By
  decision (issue #472, the comment in `handleSave`). \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`persistableCanvas`, `handleSave`); `frontend/src/lib/canvasNodes.ts` (`isDynamicPlaceholder`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`saving the parent topology excludes placeholder nodes from canvas_data`, `saving without placeholders keeps the plain success toast`)
- **DYN-UI-14.** Every persisted canvas also drops edges that touch a placeholder, and
  the fork save and fork autosave send the same placeholder-free canvas. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`persistableCanvas`) \
  Pinned by: none
- **DYN-UI-15.** Reserve Topology is enabled for a canvas holding only placeholders,
  and opens the dialog with one row per placeholder carrying its count. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`dynamicPrefill`, `TopologyEditorPage`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`enables Reserve for a placeholder-only canvas (dynamic-only booking)`, `reserving sends device_ids plus dynamic_requests expanded count-per-template`)
- **DYN-UI-16.** The template editor offers the type "Dynamic (Hypervisor)"; for it the
  driver list, labelled Recipe Driver, shows only `Hypervisor` drivers, a Hypervisor
  selector appears, and the Hardware Identity block is hidden. \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`TemplateEditorPage`) \
  Pinned by: `frontend/src/test/pages/TemplateEditorPage.test.tsx` (`offers a Dynamic (Hypervisor) type option`, `dynamic type: shows a Recipe Driver selector filtered to Hypervisor-type drivers`, `dynamic type: shows a Hypervisor selector`, `dynamic type: does not require vendor/model (identity)`)
- **DYN-UI-17.** Saving a dynamic template without a recipe driver, or without a
  hypervisor, is refused in the browser with "Dynamic templates must have a recipe
  driver" or "Dynamic templates must have a hypervisor"; inventory enforces both again
  (`inventory.md`). \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`handleSave`) \
  Pinned by: `frontend/src/test/pages/TemplateEditorPage.test.tsx` (`dynamic type: Save requires a recipe driver`, `dynamic type: Save requires a hypervisor once a driver is chosen`, `dynamic type: a fully filled form saves with driver_id and hypervisor_id`)
- **DYN-UI-18.** Changing the type clears the chosen driver, and leaving `dynamic`
  clears the chosen hypervisor. \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`TemplateEditorPage`) \
  Pinned by: `frontend/src/test/pages/TemplateEditorPage.test.tsx` (`switching from dynamic back to device clears the driver and hypervisor selection`)
- **DYN-UI-19.** The templates list filters by type Dynamic, and Copy carries
  `template_type`, `driver_id`, and `hypervisor_id` to the new template. \
  Enforced in: `frontend/src/pages/TemplatesPage.tsx` (`TYPE_FILTER_OPTIONS`, `handleTypeFilterChange`, `handleCopy`) \
  Pinned by: `frontend/src/test/pages/TemplatesPage.test.tsx` (`offers a Dynamic option and filters by it (issue #473)`, `copies a dynamic template with template_type, driver_id, and hypervisor_id (issue #473)`)
- **DYN-UI-20.** Against a running stack, a dynamic-only booking made in the dialog
  reaches `ACTIVE` and can be cancelled from the detail dialog. \
  Enforced in: `frontend/src/components/reservations/CreateReservationModal.tsx` (`handleSubmit`) \
  Pinned by: `tests/e2e/test_flows_effects_playwright.py` (`test_reservation_create_dynamic_via_modal`)
- **DYN-UI-21.** Against a running stack, an admin registers a hypervisor and creates a
  dynamic template in the browser. \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`handleSave`) \
  Pinned by: `tests/e2e/test_dynamic_template_authoring_playwright.py` (`test_hypervisor_registration_and_dynamic_template_authoring`)

**Out of scope.** The rest of the topology editor and the reservation dialog
(`topology.md`, `reservations.md`); the hypervisor admin page (`inventory.md`).

## 9. Errors

None of its own over HTTP. The internal device routes' errors are in `inventory.md`
(section 9), and how execution treats each answer is DYN-CREATE-22, DYN-CREATE-23, and
DYN-DEVICE-9. The booking errors for dynamic requests (an unknown or non-dynamic
template, inventory unreachable, more than 50 requests) are in `reservations.md`,
section 9.

Event outcomes. Execution answers no caller for an event; the outcome is the
acknowledgement and the log action.

| Outcome | Log action | When | Rule |
|---|---|---|---|
| acked, nothing run | `nats_event_unverified` | `provision_requested` for a reservation not `PENDING_PROVISION` | DYN-CREATE-1 |
| dead-lettered on first delivery, failure callback | `nats_dlq_permanent` | template, hypervisor, or secret missing; recipe package cannot load; inventory refused the device; create succeeded without a ref | DYN-CREATE-8, DYN-CREATE-9, DYN-CREATE-12, DYN-CREATE-19, DYN-CREATE-23, DYN-DLQ-1 |
| nacked with delay | `nats_message_nak` | 5xx or transport error; package download failure; failed login or create; reservations unreachable | DYN-CREATE-2, DYN-CREATE-10, DYN-CREATE-13, DYN-CREATE-16, DYN-CREATE-18 |
| dead-lettered at the fifth delivery, failure callback | `nats_dlq_exhausted` | any nacked failure that persists | DYN-CREATE-2, DYN-CREATE-18, DYN-DLQ-1 |
| acked, event abandoned, no callback | `dynamic_provision_abandoned` | a request refused or undone | DYN-CREATE-6, DYN-CREATE-26 |
| acked, ledger row left live | `dynamic_instance_keyed_destroy_failed`, `dynamic_instance_teardown_contended`, or a plain error | teardown could not prove the instance gone | DYN-DESTROY-7, DYN-DESTROY-9, DYN-DESTROY-10, DYN-DESTROY-14 |
| nacked with delay | `nats_message_nak` | teardown hit a 5xx or transport error | DYN-DESTROY-12, DYN-DESTROY-17 |

Browser refusals (toasts and inline alerts, no request sent):

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| client | `A reservation can include at most 50 dynamic instances` | rows total more than 50 | DYN-UI-3 |
| client | `Dynamic placeholders have no ports until the reservation activates` | a connection to a placeholder | DYN-UI-12 |
| client | `Dynamic instances are set when the reservation is created` | a drop on a live reservation's canvas | DYN-UI-10 |
| client | `Dynamic templates must have a recipe driver`, `Dynamic templates must have a hypervisor` | template save without either | DYN-UI-17 |

## 10. Interactions with other services

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| In (from reservations, at booking) | inventory | `GET /templates/{id}` (caller's JWT) | template check (RES-DYN-1) | Fail closed: 503 to the user (`reservations.md`) |
| Out | reservations | `GET /internal/{id}` (internal token, 10 s) | corroborate `provision_requested` | Fail closed: nack, then dead-letter at the fifth delivery with no create (DYN-CREATE-2) |
| Out | inventory | `GET /templates/{id}/internal` (internal token, 10 s) | template, recipe driver and checksum, hypervisor id, fields | Create: 404 dead-letters, 5xx and transport nack. Teardown: 404 leaves the row live, 5xx and transport nack |
| Out | inventory | `GET /hypervisors/{id}/internal` (internal token, 10 s) | endpoint, type, `secret_id` | As the template read |
| Out | secrets | `GET /internal/secrets/{id}/value` (internal token, 10 s) | the credential for the recipe | As the template read; a 200 whose body is not an object reads as no keys |
| Out | inventory | recipe package download (`load_driver`) | the recipe | Create: a broken package dead-letters, a download failure nacks. Teardown: any failure leaves the row live and acks (DYN-DESTROY-8) |
| Out | recipe driver | `login`, `create_instance`, `destroy_instance`, `logout` in the sandbox (`RECIPE_TIMEOUT_SECONDS`) | create and destroy the instance | Create: a failure nacks. Teardown: a failure leaves the row live and acks. Compensation: logged |
| Out | inventory | `POST /devices/internal` (internal token, 30 s) | materialize the instance | 5xx and transport nack; any other refusal dead-letters (DYN-CREATE-22, DYN-CREATE-23) |
| Out | inventory | `DELETE /devices/{id}/internal` (internal token, 10 s) | delete the instance device | 204 and 404 done; 5xx and transport nack; any other answer leaves the row live in teardown and is ignored in compensation (DYN-COMP-3) |
| Out | reservations | `POST /internal/{id}/provision-result` (internal token, 10 s) | report success or failure | Success: three attempts, then logged. Failure: one attempt, then logged. A lost callback is covered by the timeout backstop (RES-SWEEP-6) |

## 11. Configuration

[ENV_VARS.md](../ENV_VARS.md) has the full list.

| Setting | Default | Effect |
|---|---|---|
| `RECIPE_TIMEOUT_SECONDS` (execution) | `300` | Wall-clock limit for each recipe call |
| `DRIVER_RLIMIT_CPU_SECONDS` (execution) | `60` | CPU limit for the recipe process; waiting on the hypervisor does not count |
| `PROVISION_TIMEOUT_SECONDS` (reservations) | `900` | Deadline after which a dynamic reservation stuck in `PENDING_PROVISION` fails (RES-SWEEP-6) |
| `NATS_NAK_BACKOFF_SECONDS` (execution) | `1,5,15,60,120` | Delay before each redelivery of a nacked event; the dev and test override pins `0,1,1,1,1` |
| `NATS_ACK_WAIT_SECONDS` (execution) | `30` | In-flight window; the heartbeat keeps a long recipe call from being redelivered (WIRE-CONSUME-6) |
| `SECRETS_SERVICE_URL` (execution) | `http://secrets:8000` | Where the hypervisor credential is read |
| `INTERNAL_API_TOKEN` | empty | Service-to-service token for every internal call above |

Fixed in code, not configurable: five deliveries before a dead letter; three teardown
passes per row (`DYNAMIC_TEARDOWN_MAX_ATTEMPTS`); three success-callback attempts with a
0.5 s first delay doubling to a 10 s cap, and one failure-callback attempt; 10 s
timeouts for reads, deletes, and callbacks and 30 s for the device create; at most 50
requests per booking (RES-DYN-2) and per browser row.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/execution/tests/test_nats_consumer_dynamic.py` (in-memory SQLite, sandbox stubbed, plus a stateful recipe double run through the real sandbox); `services/execution/tests/test_nats_consumer.py`; `services/execution/tests/test_nats_consumer_event_verification.py`; `tests/unit/test_mock_hypervisor_driver.py`; the frontend tests named in section 8.10 | Compare-and-swap races are driven by interleaving inside one process, not by concurrent replicas |
| Functional (through the service API) | `services/reservations/tests/test_dynamic_requests.py` and `services/inventory/tests/test_devices_internal.py` (httpx against the app) | No live-Postgres suite covers the ledger |
| Integration (running stack) | `tests/integration/test_dynamic_resources.py` | Uses `drivers/mock_hypervisor/`. `test_provision_requested_redelivery_is_idempotent` replays the event while the reservation is `ACTIVE`, so the corroboration gate refuses it before the ledger is consulted; its docstring credits the ledger |
| Stress and load | None | `tests/load/locustfile.py` books no dynamic request; ADR 0004 skipped load testing because creation is bound by the hypervisor |
| Browser end-to-end | `tests/e2e/test_flows_effects_playwright.py` (`test_reservation_create_dynamic_via_modal`), `tests/e2e/test_dynamic_template_authoring_playwright.py` | The placeholder flow on the canvas has no browser test; nothing checks in a browser that the instance device is deleted after cancel |

Not run for this document: nothing here was checked against a running stack. The unit
and functional suites, the integration suite, and the browser suite were read, not run;
only `tests/unit/` was run.

## 13. Known limits and gaps

Four documents disagree with the code this specification describes, tracked as
documentation in #1031: `docs/ARCHITECTURE.md` (Topology separation) says physical and
cloud devices are never mixed in one reservation (DYN-REQ-6); ADR 0004 describes port
sub-templates, request parameters in `field_data`, a redelivery guard through
`action_already_succeeded`, and a redaction test, none of which exists;
[DRIVERS.md](../DRIVERS.md) gives `login` and `logout` a `success` result the flows do
not read (DYN-CREATE-17); and the replay remedy in
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md) has no caveat that the instance device is
`AVAILABLE` after release (DYN-DEVICE-11) and may have been booked again, while the
internal delete checks no reservation by decision (`inventory.md`, INV-DEL-9).

### Open defects

- #1027 (DYN-CREATE-17, DYN-CONTRACT-7, DYN-DESTROY-11): the create, teardown, and
  compensation flows judge a recipe `login` by the sandbox transport flag only, so a
  login that returns `{"success": false}` is followed by `create_instance` or
  `destroy_instance`; a device delete that fails after a successful destroy is logged
  with a plain error and no fixed log action.
- #1028 (DYN-CREATE-3, DYN-COMP-3): the status is checked once per event and then every
  request is created, so with several execution replicas a create can land after
  teardown listed the rows; the lost-`ACTIVE`-flip compensation discards the device
  delete's answer (a 5xx is raised before the compensating destroy, a 409 is logged as
  clean).
- #1029 (DYN-DESTROY-8): teardown catches every recipe load failure, a transient
  download failure included, and leaves the row live with the event acked, while the
  create path nacks the same error. Sibling of #1002.
- #1030 (DYN-REQ-6, DYN-DEVICE-10): mixed bookings are intended, and the `ACTIVE`
  device-set edit applies the type uniformity check to the whole set, so a mixed
  reservation cannot change its device set; the instance device joins No Pool, so a
  non-admin owner whose groups have no permission on No Pool cannot see it or keep it
  in a device-list edit.
- #1033 (DYN-CREATE-29): the hypervisor `enabled` flag is written but never read.

### Limits by decision

- A row teardown left live is retried only by a redelivered or re-published terminal
  event; there is no sweep (DYN-DESTROY-15). Recorded in `docs/TROUBLESHOOTING.md`
  (`dynamic_instance_keyed_destroy_failed`) and issue #937 (option B, a sweep, not
  taken).
- The keyed destroy is required of every recipe; there is no capability flag, so a
  recipe written before it leaves rows without a ref live on every teardown
  (DYN-DESTROY-5). Recorded in [DRIVERS.md](../DRIVERS.md) (Determinism contract) and
  the CHANGELOG entry for issue #937.
- `destroy_instance` must be idempotent, and the compensating destroy relies on it
  (DYN-COMP-6). Recorded in [DRIVERS.md](../DRIVERS.md) (Determinism contract).
- A create that succeeds without a ref dead-letters on its first delivery
  (DYN-CREATE-19). Recorded in the CHANGELOG entry for issue #937 and the docstring of
  `test_create_success_without_instance_ref_dead_letters_on_first_delivery`.
- Provisioning needs reservations to answer the corroboration check; an outage that
  outlasts the NAK schedule fails the reservation by timeout with no create attempted
  (DYN-CREATE-2). Recorded in the CHANGELOG entry for issue #937 and
  `docs/TROUBLESHOOTING.md`.
- A dead-lettered `provision_requested` destroys nothing itself (DYN-DLQ-4). Recorded in
  the docstring of `_maybe_post_provision_failure`.
- Placeholders are never saved with a topology (DYN-UI-13). Recorded in issue #472 and
  the comment in `handleSave` of `frontend/src/pages/TopologyEditorPage.tsx`.
- A teardown failure on a row with a ref has no fixed log action (DYN-DESTROY-10).
  Recorded in the docstring of
  `test_by_ref_destroy_failure_does_not_emit_the_keyed_action`.
- A `provision_requested` event for a reservation that is `PENDING_PROVISION` passes
  the corroboration check on its status alone, and the `dynamic_requests` it carries
  are not compared with reservations' rows (DYN-CREATE-1). Recorded in
  `provisioning-and-wiring.md` (WIRE-GATE-5), the comment above
  `_EVENT_CORROBORATION_RULES`, and [SECURITY.md](../../SECURITY.md).
- Hypervisor capacity, quotas, and scheduling across hypervisors are not handled.
  Recorded in ADR 0004 (Out of scope).

### Rules with no test

Issue #1032 tracks the tests for DYN-CTX-4 and DYN-CONTRACT-7, and the integration
redelivery test that proves the corroboration gate rather than the ledger (section 12).

- DYN-REQ-3: dynamic requests cannot change after booking.
- DYN-REQ-7: the booking-time template check has no visibility or ACL component.
- DYN-CREATE-3: the reservation status is checked once per event, not per request.
- DYN-CREATE-4: a `provision_requested` without `reservation_id`.
- DYN-CREATE-9: a missing hypervisor or secret on create.
- DYN-CREATE-15: the recipe timeout applied to recipe calls.
- DYN-CREATE-16: `logout` after any create result, and a failed `login` on create.
- DYN-CREATE-17: `login` and `logout` judged on the transport flag only.
- DYN-CREATE-23: inventory refusing the device create.
- DYN-CREATE-27: earlier instances of an event kept when a later one fails.
- DYN-CREATE-28: an abandoned event skips its remaining requests.
- DYN-CREATE-29: the hypervisor's `enabled` flag is not read.
- DYN-CTX-4: secret values redacted in run records.
- DYN-RESULT-4: run status `SUCCESS` on a recipe-reported failure.
- DYN-DEVICE-10: the instance device's visibility to its owner.
- DYN-DEVICE-11: the instance device `AVAILABLE` between release and delete.
- DYN-COMP-3: the unchecked device delete in the `ACTIVE`-flip compensation.
- DYN-COMP-7: a failed `login` in the compensating destroy.
- DYN-DESTROY-8: a package download failure during teardown.
- DYN-DESTROY-11: a device delete failure after a successful destroy.
- DYN-DESTROY-16: teardown's run user and run device id.
- DYN-DESTROY-17: a transient error reading the recipe's configuration in teardown.
- DYN-CONTRACT-2: `status` never called, `create_instance` called without arguments.
- DYN-CONTRACT-7: the `login` fail knob has no effect on the dynamic flows.
- DYN-UI-10: a drop on a live reservation's canvas.
- DYN-UI-14: placeholders left out of the fork save and autosave.
