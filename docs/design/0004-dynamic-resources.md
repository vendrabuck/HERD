# Decision: Dynamic Resources, Hypervisor-Backed Templates, Issue #32

Status: Accepted 2026-07-03; all six decision points below were resolved to
their recommended defaults. No code in this doc. Context verified against
the live HERD-public tree on 2026-07-03 (main at the secrets-service merge).

## Context

Every reservable thing in HERD today is a pre-existing device row in the
inventory schema. Issue #32 asks for a dynamic template type whose instances
are materialized when a reservation starts and destroyed when it ends, backed
by a registered hypervisor, with the create and teardown steps running as
sandboxed jobs under the existing execution model. The encrypted credential
store this depends on shipped as the `secrets` service (ADR 0003), which
built its internal retrieval surface explicitly as this feature's contract.

Relevant existing fabric, verified:

- Template discriminator: `device_templates.template_type` already
  discriminates `"device"` and `"port"`
  (`services/inventory/app/models/template.py`); the Pydantic literal in
  `TemplateCreate` (`services/inventory/app/schemas/template.py`) is the
  first place a new value must land. Drivers attach to templates
  (`DeviceTemplate.driver_id`), never to devices; execution keys its package
  cache on the `driver_sha256` the device payload carries.
- Sandbox: `execute_driver_method`
  (`services/execution/app/services/driver_sandbox.py`) runs a `Driver`
  class method in a subprocess under `setrlimit` caps, passing context via a
  temp file with `password_keys` excluded from the child environment.
  Package validation is per connection type via `REQUIRED_METHODS`
  (`services/execution/app/services/driver_loader.py`), cached per SHA256 in
  `DriverCache`.
- Applied-state ledgers: L2 `VlanAssignment` and L3 `RouteAssignment` are
  the pattern for teardown that drives only from recorded state, releasing a
  row only after clean removal (the issue #244 discipline in
  `services/execution/app/services/nats_consumer.py`).
- Events: the transactional outbox (`herd_common/outbox.py`) stamps
  `event_id` for consumer dedupe; the execution pull consumer runs
  `max_deliver=5` with a backoff schedule, `PermanentEventError` routes to
  the DLQ on first delivery, and DLQ subjects are 4-token so consumers never
  re-consume them.
- Activation hook precedent: at activation, reservations calls cabling's
  idempotent `POST /internal/forks` with `retry_with_backoff`, fail-open
  (`_create_reservation_fork_best_effort`,
  `services/reservations/app/services/reservation_service.py`). ADR 0001.
- Secrets: `GET /internal/secrets/{id}/value` and
  `GET /internal/secrets/by-name/{name}/value` (X-Internal-Token,
  `services/secrets/app/routers/internal.py`) exist with zero consumers
  today.

Two verified gaps this design must fill; neither has an analogue to reuse:

1. Inventory has no internal device-create or device-delete endpoint.
   `POST /devices` is admin-JWT only, and `create_device` rejects any
   template whose `template_type` is not `"device"`
   (`services/inventory/app/services/inventory_service.py`).
2. Execution never reports provisioning outcomes to reservations. The
   `FAILED` state and `reservation.failed` event fire only from
   reservations' own retried inventory status flip
   (`reservation_service.py`, create path); driver-level outcomes in the
   NATS consumer are recorded as `ExecutionRun` rows and go no further. A
   reservation can be `ACTIVE` with its physical provisioning silently
   failed. Issue #32's acceptance criteria (create failure lands the
   reservation in `FAILED`) cannot be met without a new feedback path.

## Decision

### A recipe is a driver package with a new connection type

A service recipe is an ordinary driver package (`driver.py` with a `Driver`
class, optional `driver_metadata.json`) whose `DriverPackage.connection_type`
is a new `ConnectionType.HYPERVISOR = "Hypervisor"`, with

    REQUIRED_METHODS["Hypervisor"] = [login, logout, create_instance,
                                      destroy_instance, status]

This reuses upload, storage, SHA256 caching, sandbox execution, metadata,
and the mock-driver test pattern wholesale; no parallel execution path.
`create_instance` returns `{"success": bool, "instance_ref": str,
"field_data": dict}` where `instance_ref` is the hypervisor-side identity
(VM id) and `field_data` carries instance attributes (management address,
etc.) for the materialized device. `destroy_instance` must be idempotent:
destroying an already-absent instance returns success.

### The hypervisor registry lives in inventory

A new `hypervisors` table in the inventory schema: id, name (unique),
description, endpoint, hypervisor_type (free string in v1: proxmox, vsphere,
libvirt), `secret_id` (bare UUID into the secrets service, validated at
registration time via the internal value endpoint), enabled flag, audit
columns. Admin CRUD at `/hypervisors`, plus
`GET /hypervisors/{id}/internal` (X-Internal-Token) for execution.
Credentials never appear inline in hypervisor rows or template fields.

### A dynamic template names a recipe and a hypervisor

`template_type` gains `"dynamic"`. A dynamic template requires `driver_id`
(which must reference a Hypervisor-type package; the recipe rides the
existing template-to-driver association) and a new nullable `hypervisor_id`
FK. Its `sections`/`FieldDefinition` schema describes instance parameters
(image, cpu, memory) exactly as device templates describe fields; recipes
receive them as the usual `HERD_<field>` context. The admin `POST /devices`
path continues to reject non-`"device"` templates; dynamic instances enter
inventory only through the internal path below.

### Booking carries dynamic requests; instances materialize as real devices

- Reservations: a new `reservation_dynamic_requests` table (reservation_id
  FK cascade-delete, template_id bare UUID, one row per requested instance),
  populated from a new optional `dynamic_requests` list on
  `ReservationCreate`. A reservation with any dynamic request always books
  through `PENDING_PROVISION`, never straight to `ACTIVE`.
- Inventory: new internal endpoints `POST /devices/internal` and
  `DELETE /devices/{id}/internal` (X-Internal-Token) that accept dynamic
  templates. The created device gets the template's port sub-templates,
  status `RESERVED`, a generated unique name
  (`<template-name>-<reservation-id-prefix>-<n>`), and `field_data` merged
  from the request parameters and the `create_instance` result.
- Execution creates the device row only after `create_instance` succeeds.
  Invariant: a dynamic device row implies a live instance beneath it.

### Lifecycle wiring: a new event out, a new callback in

- New outbox event `herd.reservations.provision_requested`, published when
  a dynamic-carrying reservation enters `PENDING_PROVISION` (both the
  immediate-booking path and the scheduler's claim path). Physical-only
  reservations are untouched and keep today's flow.
- The execution consumer handles it: per request row, fetch the template,
  hypervisor, and secret value (all internal endpoints), load the recipe
  package, run login, create_instance, logout in the sandbox, record the
  ledger row, create the inventory device.
- New internal callback on reservations:
  `POST /internal/{reservation_id}/provision-result`, X-Internal-Token,
  body `{succeeded, device_ids, error}`, idempotent per reservation. On
  success reservations attaches the device ids to `ReservationDevice`,
  transitions to `ACTIVE`, and publishes the existing `reservation.created`
  event, so L1/L2/L3 physical provisioning and fork creation proceed
  exactly as today, after the dynamic devices exist. On failure it
  transitions to `FAILED` and publishes `reservation.failed`, which now
  triggers instance teardown alongside the existing #244 teardown.
- Retries ride the consumer discipline: transient upstream errors NAK
  through the backoff schedule; exhaustion or a `PermanentEventError`
  dead-letters the message and best-effort posts a failed provision-result.
- Timeout backstop: the expiration scheduler transitions any
  `PENDING_PROVISION` reservation with dynamic requests older than
  `provision_timeout_seconds` (default 900) to `FAILED` and publishes
  `reservation.failed`. Invariant: no reservation stays in
  `PENDING_PROVISION` unbounded, even if the callback is lost.
- Teardown: `reservation.completed`, `.cancelled`, `.failed`, and
  `.updated` (removed devices) map to destroy for each ACTIVE ledger row of
  that reservation: `destroy_instance`, then internal device delete, then
  mark the row DESTROYED. A driver-result failure ACKs and leaves the row
  ACTIVE as an accurate may-still-exist record (the L3 discipline); a
  transient upstream error NAKs for redelivery. Redelivery is idempotent
  via the ledger plus `action_already_succeeded`.

### An instance ledger in the execution schema

A `dynamic_instances` table: id, reservation_id, template_id,
hypervisor_id, device_id (nullable until materialized), instance_ref,
status (CREATING, ACTIVE, DESTROYED), error, timestamps. This is the
applied-state ledger teardown drives from, the direct peer of
`VlanAssignment` and `RouteAssignment`.

### Secrets delivery and sandbox limits

Execution resolves the hypervisor's secret via
`GET /internal/secrets/{secret_id}/value` and merges the returned `data`
into the recipe context under keys listed in `password_keys`, so secret
values travel only in the context temp file and never in the child process
environment (the existing exclusion mechanism). A test pins that plaintext
never lands in `ExecutionRun` rows or logs across a full create cycle.

Instance creation takes minutes, not the 30s driver default, so recipe
actions get `recipe_timeout_seconds` (default 300). `RLIMIT_CPU` stays at
60s; waiting on a hypervisor API is not CPU time.

## Decision points (resolved 2026-07-03 to the recommended defaults)

1. Activation gate: gate `ACTIVE` on the create-step callback for
   dynamic-carrying reservations (default) versus activating immediately
   and failing post-hoc. The default keeps `ACTIVE` meaning "usable".
   Physical-only reservations are unchanged; extending the gate to physical
   driver provisioning is a separate future issue, not this one.
2. Callback transport: internal HTTP endpoint on reservations (default)
   versus a new outbox event from execution consumed by reservations. The
   default mirrors the fork-hook precedent and avoids giving reservations
   its first NATS consumer; the timeout backstop covers a lost callback
   either way.
3. Recipe attachment: the recipe rides `template.driver_id` (default)
   versus attaching it to the hypervisor row. The default lets different
   instance kinds share one hypervisor (a Linux VM and a virtual router on
   the same Proxmox need different recipes).
4. Instance representation: materialize as a real inventory device via new
   internal endpoints (default) versus keeping instances only in the
   execution schema. The default is what makes the instance visible and
   usable: topology, health polling, ACLs, and the AI tools all key off
   devices.
5. Secret reference: the hypervisor row stores the secret UUID (default)
   versus the secret name. The default validates once at registration and
   is immune to renames; the by-name internal endpoint remains available.
6. v1 surface: API-only (admin CRUD plus the booking field) with UI as a
   follow-up (default), versus shipping the UI in the same release. This
   follows the secrets-service precedent of proving the API first.

## Testing

- Unit (SQLite in-memory, no stack): state-machine transitions including
  the timeout backstop; provision-result endpoint auth (403 wording) and
  duplicate-callback idempotency; the `"dynamic"` template literal and
  cross-field validation; hypervisor CRUD including the
  secret-existence check; ledger transitions; `REQUIRED_METHODS`
  validation for Hypervisor packages.
- Functional: booking with dynamic requests lands `PENDING_PROVISION` and
  stages exactly one `provision_requested` outbox row; a success callback
  attaches devices and activates; a failure callback lands `FAILED` and
  stages `reservation.failed`.
- Integration (live stack): a `drivers/mock_hypervisor/` package with the
  `HERD_mock_*` knobs drives the happy path to `ACTIVE` with a real device
  row; create failure to `FAILED` with applied-state-only teardown;
  teardown on complete and cancel; redelivery idempotency; DLQ retention;
  the timeout backstop via a sleeping recipe; the inventory internal-create
  fault seam.
- Contract: new snapshots for inventory (hypervisors, internal device
  endpoints) and reservations (provision-result); regenerate affected.
- Load and e2e: load skipped deliberately (creation is hypervisor-bound,
  not a hot path); Selenium e2e lands with the UI follow-up per decision
  point 6.

## Phasing

Four PRs, each independently green and inert until the last wires the
event: (1) inventory: hypervisors table, dynamic template type, internal
device endpoints; (2) execution: Hypervisor connection type, instance
ledger, secrets client, consumer handler behind the not-yet-published
event; (3) reservations: dynamic requests, gated activation, callback
endpoint, timeout backstop, and the `provision_requested` publication that
turns the feature on; (4) `mock_hypervisor` driver, integration suite, and
the docs sweep (ARCHITECTURE, ROLES, ENV_VARS, FEATURES/PLANNED_FEATURES
status flip).

## Out of scope

- AI-generate or assistant proposals of dynamic resources, and AI-assisted
  recipe authoring (#28).
- Virtual networking between instances; dynamic devices participate in
  cabling and L1/L2/L3 provisioning only as ordinary device rows where
  physically meaningful.
- Hypervisor capacity, quotas, or scheduling across multiple hypervisors.
- Real hypervisor backend recipes beyond the mock; a reference recipe
  (e.g. Proxmox) ships separately once the contract is proven.
- Health-polling defaults for dynamic instances beyond what their template
  sets.
