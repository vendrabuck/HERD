# Inventory specification

| | |
|---|---|
| Area prefix | `INV` (used in rule identifiers, for example `INV-DEV-1`) |
| Verified at | commit `4f352698` (`v0.6.0-89-g4f352698`), 2026-10-06; the inventory code is unchanged since `fd589e50` |
| Owning services | inventory (`services/inventory/`) |
| Other services involved | auth (user group membership and names), reservations (device delete guard, device holds), cabling (device delete guard, device-group boundary and visibility lookups), secrets (hypervisor secret reference), execution (reads devices, templates, drivers, and hypervisors; creates and deletes dynamic-instance devices; health polling) |
| Design records | [ADR 0002](../design/0002-driver-published-config-schemas.md), [ADR 0004](../design/0004-dynamic-resources.md), [ADR 0014](../design/0014-first-class-layer-3-routing.md) |
| Related guides | [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md), [USER_GUIDE.md](../USER_GUIDE.md), [ROLES.md](../ROLES.md), [BULK_IMPORT_EXPORT.md](../BULK_IMPORT_EXPORT.md), [DRIVERS.md](../DRIVERS.md), [ENV_VARS.md](../ENV_VARS.md) |

All API paths below are the inventory service's own paths. Through the gateway they are
prefixed with `/api/inventory` (for example `GET /api/inventory/devices`).

## 1. Purpose

Inventory is the lab's catalogue: every device, its ports, the templates that define
what a device of a given kind records, the driver packages that know how to talk to it,
the hypervisors that can host virtual instances, and the device groups that decide which
users can see which devices. Admins maintain it; every signed-in user browses the part
of it their groups grant. Inventory stores device status, but it does not decide when a
device is held: reservations writes that (`reservations.md`). It does not load or run
drivers, poll health, or store cabling.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). How a token is verified and how the
role claim is read belongs to `identity-and-access.md`; every inventory route reads the
role from the JWT claim alone. Rules beyond role (device-group visibility, password
redaction) are numbered rules in section 8.

| Actor | May | May not |
|---|---|---|
| Unauthenticated caller | Nothing (every user-facing route answers 401) | Anything |
| User | List and read devices and ports visible through their device groups (password values masked); fetch visible devices in a batch; list and read templates; list, read, and download driver packages; read a driver's config schema; look up their own visible device ids; read the device groups of a visible device | Create, edit, or delete anything; see devices outside their groups; list device groups; export or import; see hypervisors |
| Admin | Everything a user may, on every device, with password values in clear; create, edit, and delete devices, ports, templates, driver packages, hypervisors, and device groups; manage group membership and grants; export and import devices and templates; look up any user's visible devices | Delete a device a live reservation depends on or a cable names (INV-DEL-1 to INV-DEL-6); delete a template, driver, or hypervisor something still references |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Set a device's status; read a device, template, or hypervisor in full; batch-read device identity and type; resolve device names; list resolved poll intervals; download a driver package; create and delete dynamic-instance devices; list the hypervisors that reference a secret (section 7) | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Device template | A kind of device or port: typed custom field definitions grouped in sections, an optional driver, hardware identity (vendor, model, part number), an icon, the exclusive flag, and an optional poll interval. `template_type` is `device`, `port`, or `dynamic` | inventory | `device_templates` (`DeviceTemplate` in `services/inventory/app/models/template.py`) |
| Field definition | One custom field: `key`, `label`, `type` (`string`, `number`, `boolean`, `dropdown`, `password`), `required`, optional `default`, and `options` for a dropdown | inventory | inside `device_templates.sections` (JSON) |
| Device | One piece of lab equipment (or one materialized virtual instance): name, template, topology type, status, `field_data` values, optional poll interval, audit names | inventory | `devices` (`Device` in `services/inventory/app/models/device.py`) |
| Topology type | `PHYSICAL` or `CLOUD`; reservations and cabling refuse to mix them | inventory (set per device) | `devices.topology_type` |
| Device status | `AVAILABLE`, `RESERVED`, `OFFLINE`, or `MAINTENANCE` (section 4) | inventory (written by admins and reservations) | `devices.status` |
| Exclusive flag | Whether one reservation at a time may hold a device of this template; read by reservations on every booking | inventory | `device_templates.exclusive`, echoed on every device read |
| Port | A named port on a device, created from a port template, with its own `field_data` | inventory | `ports` (`Port`) |
| Driver package | An admin-uploaded `.zip` or `.tar.gz` archive with a connection type, SHA256, and two capability flags read from `driver_metadata.json` | inventory | `driver_packages` (`DriverPackage`) plus the archive in local storage or MinIO |
| Connection type | `Management`, `Layer 1 Switch`, `Layer 2 Switch`, `Layer 3 Switch`, or `Hypervisor`. A device's connection type is its template's driver's; a device is a DUT when that is `Management` | inventory | `driver_packages.connection_type` |
| Hypervisor | A registered virtualization host: endpoint, type, enabled flag, and a reference to a credential. `secret_id` is a bare secrets-service id, no foreign key | inventory | `hypervisors` (`Hypervisor`) |
| Device group | A named set of devices, granted to user groups. `user_group_id` is a bare auth-service id, no foreign key | inventory | `device_groups`, `device_group_devices`, `device_group_permissions` |
| No Pool | The default device group every new device joins | inventory | a `device_groups` row named `No Pool` |
| Dynamic-instance device | A device materialized from a `dynamic` template by execution; `request_id` is the booking's dynamic-request id, a bare reservations id | inventory (created on execution's request) | `devices.request_id` |
| Config versions and apply jobs | Device configuration history and scheduled applies | inventory | `device_config_versions`, `device_config_apply_jobs`; specified in `device-configuration.md` |

## 4. State model

The device status is the only lifecycle inventory stores. A device's status records
who holds it; the rule for when a device is held is the reservations area's (its rules
RES-HOLD-1 to RES-HOLD-9 in `reservations.md`). Inventory itself enforces no transition
order: every writer below may set any of the four statuses.

**Statuses.**

- `AVAILABLE`: free to book now. The default for a new device.
- `RESERVED`: held by a live reservation (written by reservations), or a dynamic-instance
  device (written at creation).
- `OFFLINE`: set by an admin; no service sets it automatically.
- `MAINTENANCE`: set by an admin; no service sets it automatically.

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | any status (default `AVAILABLE`) | `POST /devices` | admin; device template | nothing | INV-STATUS-1, INV-STATUS-8, INV-STATUS-9 |
| (none) | any status (default `AVAILABLE`) | `POST /devices/import` (create row) | admin | nothing | INV-STATUS-8, INV-STATUS-9 |
| (none) | `RESERVED` | `POST /devices/internal` | internal token; dynamic template | nothing | INV-STATUS-3 |
| any | any | `PUT /devices/{id}` with `status` | admin | nothing | INV-STATUS-4, INV-STATUS-7, INV-STATUS-9 |
| any | any | `POST /devices/import` (update row) | admin | nothing | INV-STATUS-2, INV-STATUS-9 |
| any | any | `POST /devices/{id}/status` | internal token | nothing | INV-STATUS-5, INV-STATUS-6 |

**Concurrency.** None. Every status write reads the row and overwrites the column; there
is no compare-and-swap and no lock, so the last writer wins (INV-STATUS-7).

**Rules.**

- **INV-STATUS-1.** A device created without a status starts `AVAILABLE`. \
  Enforced in: `services/inventory/app/schemas/device.py` (`DeviceCreate`); `services/inventory/app/models/device.py` (`DeviceStatus`, `Device`) \
  Pinned by: `services/inventory/tests/test_storage_constraints.py` (`test_device_defaults_to_available_status`)
- **INV-STATUS-2.** A device import update row writes the status its row carries. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_import_existing_device_is_update`)
- **INV-STATUS-3.** A dynamic-instance device is created `RESERVED` with topology type
  `CLOUD`, whatever the request carries. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`_insert_dynamic_device`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_create_generates_name`)
- **INV-STATUS-4.** An admin `PUT /devices/{id}` that names a status writes it as
  given. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`update_device`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_update_device`)
- **INV-STATUS-5.** `POST /devices/{id}/status` (internal token) sets the status as given
  and answers the full device; an unknown id answers 404. Its only caller is
  reservations, which uses it to write holds (`reservations.md`, RES-HOLD-1 and
  RES-HOLD-6). \
  Enforced in: `services/inventory/app/routers/devices.py` (`update_device_status_internal`); `services/inventory/app/services/inventory_service.py` (`set_device_status`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_internal_status_update`, `test_internal_status_update_to_available`, `test_internal_status_update_to_maintenance`, `test_internal_status_update_nonexistent_device`)
- **INV-STATUS-6.** When `HERD_FAULT_INJECTION` is on (`1`, `true`, `yes`, or `on`) and
  the device's name contains `__herd_fault_status__`, `POST /devices/{id}/status`
  answers 503 and writes nothing. With the variable unset, or for any other device, the
  route behaves normally. Only `docker-compose.override.yml` sets the variable. \
  Enforced in: `services/inventory/app/routers/devices.py` (`_fault_injection_enabled`, `_FAULT_STATUS_SENTINEL`, `update_device_status_internal`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_fault_injection_fails_sentinel_device`, `test_fault_injection_skips_normal_device`, `test_fault_injection_inert_when_env_unset`); `tests/integration/test_provisioning_failed.py` (`test_provisioning_failure_lands_failed_and_reverts_devices`)
- **INV-STATUS-7.** Status writes are plain overwrites with no compare-and-swap, so an
  admin write and a reservations hold write racing for one device leave whichever
  committed last. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`set_device_status`, `update_device`) \
  Pinned by: none
- **INV-STATUS-8.** `POST /devices` and an import create row accept any of the four
  statuses, `RESERVED` included; the import uses `AVAILABLE` for an empty cell. \
  Enforced in: `services/inventory/app/schemas/device.py` (`DeviceCreate`); `services/inventory/app/services/bulk_service.py` (`import_devices`) \
  Pinned by: none
- **INV-STATUS-9.** No admin status write (create, update, or import) checks whether a
  reservation holds the device, so an admin can set a held device `AVAILABLE` or a free
  one `RESERVED`. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_device`, `update_device`) \
  Pinned by: none

## 5. API surface

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| GET | `/devices` | any signed-in user (non-admins: visible DUTs only) | 200 | INV-AUTH-1, INV-LIST-1 to INV-LIST-4, INV-VIS-1 to INV-VIS-5, INV-RED-1 to INV-RED-4 |
| POST | `/devices` | admin | 201 | INV-AUTH-2, INV-DEV-1 to INV-DEV-7, INV-FIELD-1 to INV-FIELD-7, INV-STATUS-1, INV-STATUS-8, INV-POLL-1 |
| POST | `/devices/batch` | any signed-in user (non-admins: visible devices only) | 200 | INV-AUTH-1, INV-BATCH-1 to INV-BATCH-5, INV-RED-1 |
| GET | `/devices/{id}` | any signed-in user (non-admins: visible devices only) | 200 | INV-AUTH-1, INV-VIS-6, INV-VIS-7, INV-RED-1 |
| PUT | `/devices/{id}` | admin | 200 | INV-AUTH-2, INV-DEV-8 to INV-DEV-11, INV-STATUS-4, INV-STATUS-9, INV-POLL-1 |
| DELETE | `/devices/{id}` | admin | 204 | INV-AUTH-2, INV-DEL-1 to INV-DEL-9 |
| GET | `/devices/{id}/ports` | any signed-in user (non-admins: visible devices only) | 200 | INV-AUTH-1, INV-PORT-1, INV-PORT-2, INV-RED-1 |
| POST | `/devices/{id}/ports` | admin | 201 | INV-AUTH-2, INV-PORT-3, INV-PORT-4 |
| POST | `/devices/{id}/ports/bulk` | admin | 201 | INV-AUTH-2, INV-PORT-3, INV-PORT-5, INV-PORT-6, INV-PORT-9 |
| GET | `/ports/{id}` | any signed-in user (non-admins: ports of visible devices only) | 200 | INV-AUTH-1, INV-PORT-2, INV-RED-1 |
| PUT | `/ports/{id}` | admin | 200 | INV-AUTH-2, INV-PORT-7, INV-PORT-10 |
| DELETE | `/ports/{id}` | admin | 204 | INV-AUTH-2, INV-PORT-8 |
| GET | `/templates` | any signed-in user | 200 | INV-AUTH-1, INV-TPL-1 |
| GET | `/templates/{id}` | any signed-in user | 200 | INV-AUTH-1, INV-TPL-1, INV-TPL-20 |
| POST | `/templates` | admin | 201 | INV-AUTH-2, INV-TPL-2 to INV-TPL-14, INV-POLL-1 |
| PUT | `/templates/{id}` | admin | 200 | INV-AUTH-2, INV-TPL-15 to INV-TPL-18, INV-TPL-24, INV-POLL-1 |
| DELETE | `/templates/{id}` | admin | 204 | INV-AUTH-2, INV-TPL-19 |
| GET | `/drivers` | any signed-in user | 200 | INV-AUTH-1, INV-DRV-1 |
| GET | `/drivers/{id}` | any signed-in user | 200 | INV-AUTH-1, INV-DRV-1 |
| GET | `/drivers/{id}/download` | any signed-in user | 200 | INV-AUTH-1, INV-DRV-2, INV-DRV-17 |
| POST | `/drivers` (multipart) | admin | 201 | INV-AUTH-2, INV-DRV-3 to INV-DRV-9 |
| PUT | `/drivers/{id}` | admin | 200 | INV-AUTH-2, INV-DRV-10, INV-DRV-11 |
| PUT | `/drivers/{id}/file` (multipart) | admin | 200 | INV-AUTH-2, INV-DRV-3, INV-DRV-4, INV-DRV-12, INV-DRV-18 |
| DELETE | `/drivers/{id}` | admin | 204 | INV-AUTH-2, INV-DRV-13 |
| GET | `/drivers/{id}/config-schema` | any signed-in user | 200 | none here; belongs to `device-configuration.md` |
| GET | `/hypervisors` | admin | 200 | INV-HYP-1 |
| GET | `/hypervisors/{id}` | admin | 200 | INV-HYP-1 |
| POST | `/hypervisors` | admin | 201 | INV-HYP-1 to INV-HYP-4, INV-HYP-11 |
| PUT | `/hypervisors/{id}` | admin | 200 | INV-HYP-1, INV-HYP-2, INV-HYP-5, INV-HYP-6, INV-HYP-11 |
| DELETE | `/hypervisors/{id}` | admin | 204 | INV-HYP-1, INV-HYP-7 |
| GET | `/device-groups` | admin | 200 | INV-AUTH-2, INV-GRP-1 |
| POST | `/device-groups` | admin | 201 | INV-AUTH-2, INV-GRP-2, INV-GRP-17 |
| GET | `/device-groups/{id}` | admin | 200 | INV-AUTH-2, INV-GRP-1 |
| PUT | `/device-groups/{id}` | admin | 200 | INV-AUTH-2, INV-GRP-2, INV-GRP-17 |
| DELETE | `/device-groups/{id}` | admin | 204 | INV-AUTH-2, INV-GRP-3, INV-GRP-15, INV-GRP-16 |
| POST | `/device-groups/{id}/devices/bulk` | admin | 200 | INV-AUTH-2, INV-GRP-4 to INV-GRP-7 |
| POST | `/device-groups/{id}/devices/bulk-remove` | admin | 200 | INV-AUTH-2, INV-GRP-8, INV-GRP-15 |
| POST | `/device-groups/{id}/permissions/bulk` | admin | 200 | INV-AUTH-2, INV-GRP-9 |
| POST | `/device-groups/{id}/permissions/bulk-remove` | admin | 200 | INV-AUTH-2, INV-GRP-9 |
| GET | `/device-groups/visible-devices?user_id` | the user themself, or any admin | 200 | INV-VIS-8 |
| GET | `/device-groups/device/{id}` | any signed-in user (non-admins: visible devices only) | 200 | INV-GRP-10 to INV-GRP-13, INV-VIS-6 |
| GET | `/devices/export`, `/templates/export` | admin | 200 | INV-BULK-1 to INV-BULK-3, INV-BULK-19, INV-CSV-1, INV-CSV-2 |
| POST | `/devices/import`, `/templates/import` (multipart) | admin | 200 | INV-BULK-4 to INV-BULK-16, INV-BULK-18 to INV-BULK-20, INV-CSV-3 |
| `/devices/{id}/config-versions...`, `/devices/{id}/apply-jobs`, `/apply-jobs/...` | | see `device-configuration.md` | | none here |

`GET /devices` takes `template_id`, `topology_type`, `status`, `dut_only`, `search`,
`skip` (default 0), and `limit` (1 to 500, default 50), orders by `created_at`
descending, and answers `{items, total, skip, limit}`. `GET /templates` takes
`template_type` (not validated; an unknown value matches nothing), `skip`, and `limit`
(same bounds) and orders by `created_at` descending. `GET /drivers` orders by name, `GET /hypervisors` by `created_at`
descending, and `GET /device-groups` by name; each takes `skip` and `limit` with the
same bounds. A device response carries the template's name, icon, vendor, model, part
number, the driver's id, name, SHA256, filename, and connection type, the template's
exclusive flag, the audit fields, `poll_interval_seconds`, and
`resolved_poll_interval_seconds`.

## 6. Events

None. Inventory publishes no events and consumes none; it has no NATS connection.

## 7. Internal API

Every route here requires `X-Internal-Token` (INV-INT-1).

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| POST | `/devices/{id}/status` | `X-Internal-Token` | reservations (device holds) | the device | INV-INT-1, INV-STATUS-5, INV-STATUS-6 |
| GET | `/devices/{id}/internal` | `X-Internal-Token` | execution, ai-orchestrator | the device, password values in clear | INV-INT-1, INV-INT-2, INV-RED-3 |
| POST | `/internal/devices/batch` | `X-Internal-Token` | cabling (L3 intent validation) | list of `{id, name, connection_type, status}` | INV-INT-1, INV-INT-3, INV-INT-4 |
| POST | `/devices/resolve-by-name` | `X-Internal-Token` | cabling (topology import) | `{resolved: {name: id}}` | INV-INT-1, INV-INT-5 |
| GET | `/devices/health-config` | `X-Internal-Token` | execution (health scheduler) | list of `{device_id, resolved_interval_seconds}` | INV-INT-1, INV-POLL-3 |
| POST | `/devices/internal` | `X-Internal-Token` | execution (dynamic provisioning) | the created or existing device | INV-INT-1, INV-DYN-1 to INV-DYN-6, INV-DYN-9, INV-STATUS-3 |
| DELETE | `/devices/{id}/internal` | `X-Internal-Token` | execution (dynamic teardown) | 204 | INV-INT-1, INV-DYN-7, INV-DYN-8 |
| GET | `/templates/{id}/internal` | `X-Internal-Token` | execution, ai-orchestrator | the template | INV-INT-1, INV-TPL-20 |
| GET | `/drivers/{id}/internal-download` | `X-Internal-Token` | execution (driver loader) | the archive bytes | INV-INT-1, INV-DRV-2 |
| GET | `/hypervisors/{id}/internal` | `X-Internal-Token` | execution (dynamic provisioning) | `{id, name, endpoint, hypervisor_type, secret_id, enabled}` | INV-INT-1, INV-HYP-8 |
| GET | `/hypervisors/by-secret/{secret_id}/internal` | `X-Internal-Token` | secrets (delete guard) | list of `{id, name}` | INV-INT-1, INV-HYP-9 |

## 8. Features

### 8.1 Access to inventory routes

**What it does.** Every inventory page needs a signed-in user; browsing is open to every
role, and changing anything is for admins.

**Surfaces.** Every route in section 5. The token check itself is specified in
`identity-and-access.md`.

**Rules.**

- **INV-AUTH-1.** A request with no bearer token, or with a token that does not verify,
  is refused with 401 before the handler runs; any verified token passes the read
  routes. \
  Enforced in: `services/inventory/app/dependencies/auth.py` (`get_current_user_payload`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_list_devices_invalid_token`, `test_batch_invalid_token_returns_401`); `services/inventory/tests/test_device_groups.py` (`test_visible_devices_requires_auth`)
- **INV-AUTH-2.** Every create, update, and delete route for devices, ports, templates,
  drivers, and device groups, and the device group list and detail reads, answer 403
  `Admin or superadmin role required` to a caller whose role claim is not `admin` or
  `superadmin`. \
  Enforced in: `services/inventory/app/dependencies/auth.py` (`require_admin`) \
  Pinned by: `services/inventory/tests/test_rbac_denial.py` (`test_non_admin_denied_json`, `test_non_admin_denied_driver_upload`, `test_non_admin_denied_driver_file_replace`); `services/inventory/tests/test_devices.py` (`test_superadmin_can_create_device`)

**Out of scope.** Group-scoped admin rights do not exist: an admin manages every device.

### 8.2 Device and port templates

**What it does.** An admin defines a template for each kind of device or port: the
custom fields it records (text, number, yes or no, a pick list, or a password), their
defaults, which driver drives it, and its hardware identity. Devices and ports are then
created from templates.

**Surfaces.** User interface `frontend/src/pages/TemplatesPage.tsx` (list, type filter,
Copy, delete) and `frontend/src/pages/TemplateEditorPage.tsx` (view and edit); routes
`GET /templates`, `GET /templates/{id}`, `POST /templates`, `PUT /templates/{id}`,
`DELETE /templates/{id}`.

**Rules.**

- **INV-TPL-1.** Any signed-in user may list and read templates; the list filters by
  `template_type` when given. \
  Enforced in: `services/inventory/app/routers/templates.py` (`get_templates`, `get_template_by_id`); `services/inventory/app/services/template_service.py` (`list_templates`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_user_can_list_templates`, `test_user_can_get_template`, `test_list_templates_filter_by_type`)
- **INV-TPL-2.** `template_type` is `device`, `port`, or `dynamic` and defaults to
  `device`. \
  Enforced in: `services/inventory/app/schemas/template.py` (`TemplateCreate`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_create_template_default_type_device`, `test_create_template_with_type_port`); `services/inventory/tests/test_dynamic_templates.py` (`test_create_dynamic_template`)
- **INV-TPL-3.** A template name is unique; a duplicate answers 409
  `Template with name '<name>' already exists`. \
  Enforced in: `services/inventory/app/services/template_service.py` (`create_template`, `_integrity_http_error`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_create_template_duplicate_name`); `services/inventory/tests/test_template_integrity_errors.py` (`test_create_duplicate_name_still_409`)
- **INV-TPL-4.** A template has at least one section, and every section name is
  non-blank. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_sections`, `validate_name_not_empty`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_create_template_empty_sections`, `test_create_template_empty_section_name`)
- **INV-TPL-5.** A field key is non-empty and contains only letters, digits, and
  underscores; keys are unique within one section. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_key_format`, `validate_unique_field_keys`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_create_template_invalid_field_key`, `test_create_template_duplicate_field_keys`); `services/inventory/tests/test_template_schema_validators.py` (`test_field_key_empty_rejected`)
- **INV-TPL-6.** The same key may appear in two sections; device validation then treats
  it as one field defined by the later section. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: none
- **INV-TPL-7.** A `dropdown` field needs a non-empty options list with no blank option;
  any other type may not carry options. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_options_and_default`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_dropdown_without_options`, `test_non_dropdown_with_options`, `test_create_template_dropdown_empty_option_string`)
- **INV-TPL-8.** A field default must match the field type (a string for `string` and
  `password`, a number for `number`, a boolean for `boolean`), and a dropdown default
  must be one of its options. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_options_and_default`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_template_default_type_mismatch`, `test_template_dropdown_default_not_in_options`); `services/inventory/tests/test_template_schema_validators.py` (`test_string_default_must_be_string`, `test_number_default_must_be_number`, `test_boolean_default_must_be_boolean`)
- **INV-TPL-9.** A `device` template needs a driver and a non-blank vendor and model; a
  `port` template needs neither, and a missing vendor or model is stored as `unknown`. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_sections`); `services/inventory/app/services/template_service.py` (`create_template`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_device_template_without_driver_returns_422`, `test_create_device_template_requires_vendor_and_model`, `test_create_port_template_without_identity_succeeds`)
- **INV-TPL-10.** Vendor, model, and part number, when given, must not be blank or
  whitespace. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_identity_not_whitespace`, `validate_part_number_not_whitespace`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_create_device_template_rejects_whitespace_vendor`); `services/inventory/tests/test_template_schema_validators.py` (`test_template_part_number_whitespace_rejected`)
- **INV-TPL-11.** A `port` template may not carry a driver, and only a `dynamic` template
  may carry a hypervisor. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_sections`) \
  Pinned by: `services/inventory/tests/test_template_schema_validators.py` (`test_template_driver_on_port_template_rejected`); `services/inventory/tests/test_dynamic_templates.py` (`test_hypervisor_id_on_device_template_422`)
- **INV-TPL-12.** A `dynamic` template needs both a driver (its recipe) and a hypervisor;
  it is exempt from the vendor and model requirement. See ADR 0004. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_sections`) \
  Pinned by: `services/inventory/tests/test_dynamic_templates.py` (`test_create_dynamic_template`, `test_dynamic_template_missing_driver_422`, `test_dynamic_template_missing_hypervisor_422`)
- **INV-TPL-13.** On create, a `dynamic` template's driver must be a `Hypervisor`
  package and a `device` template's driver must not be. \
  Enforced in: `services/inventory/app/services/template_service.py` (`_validate_driver_connection_type`, `create_template`) \
  Pinned by: `services/inventory/tests/test_dynamic_templates.py` (`test_dynamic_template_non_hypervisor_driver_422`, `test_device_template_hypervisor_driver_422`)
- **INV-TPL-14.** A driver or hypervisor id that does not exist answers 422
  `Referenced hypervisor or driver does not exist`, never a name conflict. \
  Enforced in: `services/inventory/app/services/template_service.py` (`_integrity_kind`, `_integrity_http_error`) \
  Pinned by: `services/inventory/tests/test_template_integrity_errors.py` (`test_create_fk_violation_is_422_not_name_conflict`, `test_integrity_kind_postgres_sqlstates`)
- **INV-TPL-15.** `PUT /templates/{id}` changes only the fields the body names. An
  unknown id answers 404. \
  Enforced in: `services/inventory/app/schemas/template.py` (`TemplateUpdate`); `services/inventory/app/services/template_service.py` (`update_template`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_update_template`, `test_update_template_icon`, `test_update_template_can_clear_part_number`, `test_update_template_not_found`)
- **INV-TPL-16.** An update whose body names `driver_id` or `hypervisor_id` re-runs the
  create-time requirements of INV-TPL-9, INV-TPL-11, and INV-TPL-12 and the
  connection-type rule of INV-TPL-13 on the merged driver and hypervisor against the
  stored template type, answering 422 with the create path's words (for example
  `Device templates must have a driver`, `Dynamic templates must have a hypervisor`,
  `hypervisor_id is only valid on dynamic templates`), so an update cannot clear a
  device template's driver or a dynamic template's hypervisor. An update that names
  neither is not re-checked. \
  Enforced in: `services/inventory/app/services/template_service.py` (`update_template`, `_validate_driver_and_hypervisor_presence`, `_validate_driver_connection_type`) \
  Pinned by: `services/inventory/tests/test_dynamic_templates.py` (`test_update_device_template_clearing_driver_is_422`, `test_update_device_template_to_hypervisor_driver_is_422`, `test_update_device_template_adding_hypervisor_is_422`, `test_update_dynamic_template_clearing_hypervisor_or_driver_is_422`, `test_update_template_driver_swap_within_contract_still_succeeds`)
- **INV-TPL-17.** An update that replaces `sections` keeps at least one section and
  does not re-validate existing devices' `field_data` against the new sections. \
  Enforced in: `services/inventory/app/schemas/template.py` (`TemplateUpdate`); `services/inventory/app/services/template_service.py` (`update_template`) \
  Pinned by: `services/inventory/tests/test_template_schema_validators.py` (`test_template_update_empty_sections_rejected`); `services/inventory/tests/test_template_device_flow.py` (`test_template_update_does_not_break_existing_devices`)
- **INV-TPL-18.** The exclusive flag defaults to true and may be changed by an update;
  every device read reports its template's current flag. \
  Enforced in: `services/inventory/app/models/template.py` (`DeviceTemplate`); `services/inventory/app/routers/devices.py` (`_device_to_response`) \
  Pinned by: `services/inventory/tests/test_templates.py` (`test_create_template_exclusive_default`, `test_update_template_exclusive`); `services/inventory/tests/test_devices.py` (`test_device_response_includes_exclusive`)
- **INV-TPL-19.** A template that any device or port still references cannot be deleted:
  409 `Cannot delete template: devices still reference it` (checked first) or
  `Cannot delete template: ports still reference it`. \
  Enforced in: `services/inventory/app/services/template_service.py` (`delete_template`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_delete_template_blocked_by_devices`); `services/inventory/tests/test_ports.py` (`test_delete_port_template_blocked_by_ports`)
- **INV-TPL-20.** A template read returns its sections as stored, plus the driver's
  name, SHA256, filename, and connection type (null for a template without a driver). \
  Enforced in: `services/inventory/app/routers/templates.py` (`_template_to_response`) \
  Pinned by: `services/inventory/tests/test_templates_internal.py` (`test_internal_template_exposes_driver_sha256_and_filename`, `test_internal_template_null_driver_fields_for_port_template`)
- **INV-TPL-21.** The templates page's Copy creates `Copy of <name>` carrying the
  type, driver, hypervisor, exclusive flag, icon, description, identity, poll interval,
  and sections, and shows the server's detail when the create is refused. \
  Enforced in: `frontend/src/pages/TemplatesPage.tsx` (`handleCopy`) \
  Pinned by: `frontend/src/test/pages/TemplatesPage.test.tsx` (`copies a dynamic template with template_type, driver_id, and hypervisor_id (issue #473)`, `copies a device template with its driver and identity fields`, `surfaces the backend detail on a copy failure`)
- **INV-TPL-22.** The templates page offers All, Device, Port, and Dynamic type filters,
  and shows Create Template, Copy, and Delete only to admins. \
  Enforced in: `frontend/src/pages/TemplatesPage.tsx` (`TYPE_FILTER_OPTIONS`, `TemplatesPage`) \
  Pinned by: `frontend/src/test/pages/TemplatesPage.test.tsx` (`offers a Dynamic option and filters by it (issue #473)`); `frontend/src/test/pages/TemplatesPage.states.test.tsx` (`a non-admin sees no Create Template button and no Actions column`, `an admin sees the Create Template button and Actions column`)
- **INV-TPL-23.** The template editor disables the type select when editing an existing
  template, offers only non-Hypervisor drivers for a device template and only
  Hypervisor drivers for a dynamic one. \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`TemplateEditorPage`) \
  Pinned by: `frontend/src/test/pages/TemplateEditorPage.editflow.test.tsx` (`Type select is disabled once editing an existing template`); `frontend/src/test/pages/TemplateEditorPage.test.tsx` (`device type: driver dropdown excludes Hypervisor-type drivers`, `dynamic type: shows a Recipe Driver selector filtered to Hypervisor-type drivers`)
- **INV-TPL-24.** The update body has no `template_type` field, so a template's type
  never changes after create; a `template_type` sent in the body is ignored. \
  Enforced in: `services/inventory/app/schemas/template.py` (`TemplateUpdate`) \
  Pinned by: none

**Out of scope.** The AI identity suggestion in the editor (`ai-features.md`). What a
dynamic template does at booking time (`dynamic-resources.md`).

### 8.3 Devices

**What it does.** An admin adds a device by choosing a device template and filling in
its fields; the device then appears in the inventory list, can be edited, copied, and
deleted, and can be booked.

**Surfaces.** User interface `frontend/src/pages/admin/AddDevicePage.tsx` (create),
`frontend/src/pages/DevicePage.tsx` (detail, edit, delete), and the inventory list
(section 8.6); routes `POST /devices`, `GET /devices/{id}`, `PUT /devices/{id}`,
`DELETE /devices/{id}`. Status writes are section 4.

**Rules.**

- **INV-DEV-1.** A device name is 1 to 255 characters and unique; a duplicate answers
  409 `Device with name '<name>' already exists` on create and on rename. \
  Enforced in: `services/inventory/app/schemas/device.py` (`DeviceCreate`, `DEVICE_NAME_MAX_LENGTH`); `services/inventory/app/services/inventory_service.py` (`create_device`, `update_device`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_create_device_duplicate_name`, `test_update_device_duplicate_name`); `services/inventory/tests/test_schema_bounds.py` (`test_device_create_name_empty_rejected`, `test_device_create_name_over_cap_rejected`)
- **INV-DEV-2.** `POST /devices` accepts only a template of type `device`: an unknown
  template answers 422 `Template not found`, a port or dynamic template 422. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_device`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_create_device_invalid_template`, `test_create_device_with_port_template_fails`); `services/inventory/tests/test_devices_internal.py` (`test_admin_create_device_rejects_dynamic_template_422`)
- **INV-DEV-3.** A device cannot be created from a template whose vendor or model is
  `unknown`; the 422 names the template and asks for vendor and model. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_device`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_create_device_blocked_when_template_vendor_unknown`, `test_create_device_succeeds_when_template_identity_known`)
- **INV-DEV-4.** A new device joins the `No Pool` device group, when that group exists. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_device`); `services/inventory/app/services/device_group_service.py` (`add_device_to_no_pool`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_new_device_auto_assigned_to_no_pool`); `services/inventory/tests/test_device_group_service_unit.py` (`test_add_device_to_no_pool_no_group`)
- **INV-DEV-5.** Create records the caller's id and `username` claim as `created_by`
  and `created_by_name`. \
  Enforced in: `services/inventory/app/routers/devices.py` (`create_new_device`) \
  Pinned by: `services/inventory/tests/test_device_audit.py` (`test_create_device_records_created_by_and_name`)
- **INV-DEV-6.** A device's topology type is `PHYSICAL` or `CLOUD`, set on create, and
  the list filters on it. \
  Enforced in: `services/inventory/app/schemas/device.py` (`DeviceCreate`); `services/inventory/app/services/inventory_service.py` (`list_devices`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_filter_by_topology_type`)
- **INV-DEV-7.** A device's driver (id, name, SHA256, file name, connection type) is its
  template's driver, and its exclusive flag is its template's; none is stored on the
  device. \
  Enforced in: `services/inventory/app/routers/devices.py` (`_device_to_response`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_device_response_includes_driver_info`); `services/inventory/tests/test_devices.py` (`test_device_response_carries_driver_sha256_and_filename`, `test_device_response_includes_exclusive`); `services/inventory/tests/test_devices_internal_batch.py` (`test_batch_returns_identity_and_connection_type`)
- **INV-DEV-8.** `PUT /devices/{id}` changes only the fields the body names (name,
  topology type, status, `field_data`, poll interval); an unknown id answers 404. \
  Enforced in: `services/inventory/app/schemas/device.py` (`DeviceUpdate`); `services/inventory/app/services/inventory_service.py` (`update_device`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_update_device`, `test_update_device_not_found`, `test_update_device_name_to_same_name`)
- **INV-DEV-9.** An update that names `field_data` replaces the whole object after
  validating it against the device's template (INV-FIELD-1 to INV-FIELD-7). \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`update_device`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_update_device_field_data`, `test_update_device_invalid_field_data`)
- **INV-DEV-10.** An update records the caller's id and username as `modified_by` and
  `modified_by_name`. \
  Enforced in: `services/inventory/app/routers/devices.py` (`update_device_by_id`) \
  Pinned by: `services/inventory/tests/test_device_audit.py` (`test_update_device_records_modified_by_and_name`)
- **INV-DEV-11.** An update that sends an explicit null for `name`, `topology_type`,
  `status`, or `field_data` (NOT NULL columns) answers 422 `<field> cannot be null; omit
  the field to leave it unchanged`; an omitted field is left unchanged, and a null
  `poll_interval_seconds` still clears the override. Only a unique violation at commit
  answers 409 `Device with name '<name>' already exists`; any other integrity error
  answers 409 `Device violates a database constraint`. \
  Enforced in: `services/inventory/app/schemas/device.py` (`DeviceUpdate`); `services/inventory/app/services/inventory_service.py` (`update_device`) \
  Pinned by: `services/inventory/tests/test_schema_bounds.py` (`test_device_update_explicit_null_on_not_null_column_rejected_by_name`, `test_device_update_explicit_null_poll_interval_still_clears_the_override`); `services/inventory/tests/test_inventory_service_unit.py` (`test_update_device_duplicate_name`, `test_update_device_non_unique_integrity_error_is_not_a_name_clash`); `services/inventory/tests/test_bulk.py` (`test_put_device_explicit_null_status_is_422_naming_the_field`)
- **INV-DEV-12.** Deleting a device removes its ports and its group memberships with it. \
  Enforced in: `services/inventory/app/models/device.py` (`Device`); `services/inventory/app/models/device_group.py` (`DeviceGroupDevice`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_delete_device_cascades_ports`); `services/inventory/tests/test_device_groups.py` (`test_delete_device_cascades_from_group`); `services/inventory/tests/test_storage_constraints.py` (`test_delete_device_cascades_device_group_device`)
- **INV-DEV-13.** The device page shows password-typed field values as `********`
  for every role, and its delete shows the refusal in words; a `device_cabled` refusal
  stays on the page with the connection count and a link to Connections. \
  Enforced in: `frontend/src/pages/DevicePage.tsx` (`handleDelete`); `frontend/src/api/inventory.ts` (`deleteDeviceErrorMessage`, `deviceCabledCount`) \
  Pinned by: `frontend/src/test/pages/DevicePage.test.tsx` (`renders device details with template fields and masks passwords`, `toasts a readable message when delete is blocked by an active reservation (409)`, `keeps the cabled refusal on the page with the count and a link to Connections (issue #940)`)
- **INV-DEV-14.** The delete refusal text names member and transit holders separately
  and reports the true connection total, not the capped id sample. \
  Enforced in: `frontend/src/api/inventory.ts` (`deleteDeviceErrorMessage`) \
  Pinned by: `frontend/src/test/api/deleteDeviceErrorMessage.test.ts` (`names a single transit-only reservation`, `mentions both causes when the device is a member and a transit hop`, `reports the true total, not the capped id sample`)
- **INV-DEV-15.** Edit copies the device as it stands at that moment into the form, and
  the device page's Save sends only the fields the admin changed since then (name,
  topology type, status, or the whole `field_data` object); a status the admin did not
  touch is never sent, and a save with no change sends nothing (issue #1020). \
  Enforced in: `frontend/src/lib/deviceEdit.ts` (`deviceEditPayload`); `frontend/src/pages/DevicePage.tsx` (`DevicePage`) \
  Pinned by: `frontend/src/test/pages/DevicePage.test.tsx` (`a rename does not write back a status that changed server-side after the page loaded`, `Edit starts from the current device, not the copy taken when the page opened`, `saving with nothing changed sends no request and leaves edit mode`); `frontend/src/test/lib/deviceEdit.test.ts` (`sends only the changed name, trimmed, and never the status`)

**Out of scope.** Changing a device's template: the update body has no template
field. The configuration section of the device page and the ports section's
config actions (`device-configuration.md`); the health badge
(`operations-and-observability.md`).

### 8.4 Typed custom fields

**What it does.** A device or port records the values its template asks for; inventory
checks every value against the field's type and fills in defaults.

**Surfaces.** Device create and update, port create and update, and the internal
dynamic-instance create.

**Rules.**

- **INV-FIELD-1.** A key the template does not define is refused with 422
  `Unknown fields: <keys>`, except on the internal dynamic-instance create (INV-DYN-3). \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_unknown_field`); `services/inventory/tests/test_devices.py` (`test_create_device_unknown_field`)
- **INV-FIELD-2.** A field that is missing, null, or the empty string takes its default
  when it has one. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_default_applied`); `services/inventory/tests/test_devices.py` (`test_create_device_with_default_applied`, `test_default_not_overridden`)
- **INV-FIELD-3.** A required field that is still missing, null, or empty after defaults
  answers 422 `Required field missing: <key>`. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_required_missing`); `services/inventory/tests/test_devices.py` (`test_create_device_missing_required_field`)
- **INV-FIELD-4.** A `string` or `password` value must be a string. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_string_type_check`, `test_validate_field_data_password_type`)
- **INV-FIELD-5.** A `number` value must be an integer or a float, and a boolean is not
  a number. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_number_type_check`, `test_validate_field_data_number_rejects_bool`)
- **INV-FIELD-6.** A `boolean` value must be a boolean, and a `dropdown` value must be
  one of the options. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_boolean_type_check`, `test_validate_field_data_dropdown_invalid_value`)
- **INV-FIELD-7.** An optional field left null or empty is stored as given and skips the
  type check. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`) \
  Pinned by: `services/inventory/tests/test_inventory_service_unit.py` (`test_validate_field_data_empty_value_skipped`); `services/inventory/tests/test_devices.py` (`test_create_device_optional_fields_omitted`)

**Out of scope.** Device configuration payloads are validated by
`device-configuration.md`'s rules, not by template fields.

### 8.5 Device visibility and password redaction

**What it does.** A non-admin user sees only the devices in device groups granted to one
of their user groups, and never sees the value of a password field. Admins see
everything.

**Surfaces.** `GET /devices`, `POST /devices/batch`, `GET /devices/{id}`, the port
reads, `GET /device-groups/device/{id}`, `GET /device-groups/visible-devices`. Other
areas call `check_device_read_visibility` for their own device-scoped reads
(`device-configuration.md`).

**Rules.**

- **INV-VIS-1.** A non-admin's visible devices are the members of every device group
  granted to any user group the auth service reports for them. A user in no user
  group, or whose groups have no grants, sees no device. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`get_visible_device_ids`); `services/inventory/app/services/device_visibility.py` (`_resolve_visible_device_ids`) \
  Pinned by: `services/inventory/tests/test_device_group_service_unit.py` (`test_get_visible_device_ids_with_permissions`, `test_get_visible_device_ids_empty`, `test_get_visible_device_ids_no_permissions`); `tests/integration/test_device_group_visibility.py` (`test_non_admin_visibility_requires_device_group_permission`)
- **INV-VIS-2.** The user groups are read from auth's `GET /groups/user/{id}` with the
  caller's own JWT. A transport error or a non-200 answer fails closed with 503; it
  never falls back to showing every device. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`_fetch_user_group_ids`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_fetch_user_group_ids_raises_on_auth_service_5xx`, `test_fetch_user_group_ids_raises_on_connection_error`); `services/inventory/tests/test_devices.py` (`test_device_list_fails_closed_on_auth_outage`)
- **INV-VIS-3.** A visibility lookup without an Authorization header to forward answers
  500. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`_fetch_user_group_ids`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_fetch_user_group_ids_missing_authorization_raises_500`)
- **INV-VIS-4.** `GET /devices` for a non-admin returns only visible devices and forces
  `dut_only` on, so a granted non-Management device is not listed. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_non_admin_forced_dut_only`); `tests/integration/test_device_group_visibility.py` (`test_non_admin_sees_only_dut_devices_in_granted_group`)
- **INV-VIS-5.** `GET /devices` from a non-admin whose token subject is not a UUID
  answers 401 `invalid token subject`. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_list_devices_malformed_token_subject_401`)
- **INV-VIS-6.** A device-scoped read by a non-admin answers a hidden device with the
  same 404 status and detail the route gives an unknown id, and a visibility lookup
  failure with 503. `GET /devices/{id}` and the port reads do this inline;
  `GET /device-groups/device/{id}` and other areas' device reads go through
  `check_device_read_visibility`. \
  Enforced in: `services/inventory/app/services/device_visibility.py` (`check_device_read_visibility`); `services/inventory/app/routers/devices.py` (`get_device_by_id`) \
  Pinned by: `services/inventory/tests/test_device_read_visibility_gate.py` (`test_hidden_device_404_matches_unknown_id_404`, `test_visibility_unavailable_fails_closed_503`, `test_non_admin_inside_groups_sees_same_body_as_admin`); `services/inventory/tests/test_devices.py` (`test_get_device_non_admin_denied_when_not_visible`, `test_single_device_read_fails_closed_on_auth_outage`)
- **INV-VIS-7.** Admins are never filtered and never trigger a visibility lookup; a
  non-admin whose token subject is not a UUID gets the route's not-found 404 on a
  single-device read. \
  Enforced in: `services/inventory/app/services/device_visibility.py` (`check_device_read_visibility`); `services/inventory/app/routers/devices.py` (`get_device_by_id`) \
  Pinned by: `services/inventory/tests/test_device_read_visibility_gate.py` (`test_admin_never_consults_visibility`); `services/inventory/tests/test_router_edge_cases.py` (`test_get_device_malformed_token_subject_404`)
- **INV-VIS-8.** `GET /device-groups/visible-devices` answers only for the caller's own
  `user_id` unless the caller is an admin; another user's id answers 403
  `Cannot query visible devices for another user`. An auth failure answers 503. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`_authorize_subject`, `get_visible_devices_endpoint`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_visible_devices_foreign_user_forbidden_for_user_role`, `test_visible_devices_self_lookup_allowed_for_user_role`, `test_visible_devices_admin_foreign_user_allowed`, `test_visible_devices_superadmin_foreign_user_allowed`, `test_visible_devices_endpoint_returns_503_when_auth_service_down`)
- **INV-RED-1.** Every non-admin device and port read (list, batch, single device, port
  list, single port) replaces the value of each field the template declares as a
  `password` field with `********`, keeping the key. \
  Enforced in: `services/inventory/app/routers/devices.py` (`_password_field_keys`, `_redact_field_data`); `services/inventory/app/routers/ports.py` (`_port_to_response`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_non_admin_get_device_masks_password_fields`, `test_non_admin_list_devices_masks_password_fields`, `test_batch_non_admin_gets_only_visible_with_passwords_redacted`); `services/inventory/tests/test_ports.py` (`test_port_password_field_redacted_for_non_admin`)
- **INV-RED-2.** An empty or null password value is left as it is, so a masked value
  always means a secret is set. \
  Enforced in: `services/inventory/app/routers/devices.py` (`_redact_field_data`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_non_admin_empty_password_value_not_falsely_masked`)
- **INV-RED-3.** Admin reads and the internal device read return password values in
  clear. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_device_by_id`, `get_device_by_id_internal`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_admin_get_device_returns_password_fields_unmasked`, `test_internal_get_device_returns_password_fields_unmasked`, `test_batch_admin_passwords_not_redacted`)
- **INV-RED-4.** Redaction reads the device's current template: a value stored under a
  key the template does not declare as `password` is returned as stored. \
  Enforced in: `services/inventory/app/routers/devices.py` (`_password_field_keys`) \
  Pinned by: none

**Out of scope.** Cabling's and reservations' use of visibility (`topology.md`,
`reservations.md`); granting user groups to device groups is section 8.9.

### 8.6 Inventory list page and search

**What it does.** Everyone can browse the device list: search by name, filter by
status, template, and topology type, page through it, and expand a row to see its
ports and what each port is cabled to. The page remembers the filters and the page
size per user. Admins can also copy and bulk-delete devices.

**Surfaces.** User interface `frontend/src/pages/InventoryPage.tsx`,
`frontend/src/lib/inventoryFilters.ts`; route `GET /devices` (and the port and cabling
connection reads for an expanded row). Import and export controls and Copy render for
admins only. Copy names the new device `Copy of <name>` and sends the source's template,
topology type, and `field_data`, leaving the status to the default; the Template
filter reads the first 500 device templates.

**Rules.**

- **INV-LIST-1.** `GET /devices` filters by `template_id`, `topology_type`, and
  `status` when given, all combined with AND. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`list_devices`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_filter_by_status`, `test_filter_by_template_id`, `test_combined_filters`)
- **INV-LIST-2.** `search` matches a case-insensitive substring of the device name. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`list_devices`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_search_devices_by_name`, `test_search_devices_no_match`)
- **INV-LIST-3.** `dut_only=true` keeps only devices whose template's driver is a
  `Management` package. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`list_devices`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_list_devices_dut_only_filter`); `services/inventory/tests/test_drivers.py` (`test_dut_only_filters_infrastructure_devices`)
- **INV-LIST-4.** `skip` is at least 0 and `limit` is 1 to 500 (default 50); `total`
  counts every matching device, not just the page. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices`); `services/inventory/app/services/inventory_service.py` (`list_devices`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_pagination_limit`, `test_pagination_skip`, `test_pagination_skip_exceeds_total`); `services/inventory/tests/test_inventory_service_unit.py` (`test_list_devices_pagination`)
- **INV-LIST-5.** The page sends no parameter for a filter at All, one parameter per set
  filter, the search with them in the same request, and goes back to the first page on
  any filter change. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`InventoryPage`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`sends no filter parameter at All, and labels templates with vendor and model`, `each filter sends its own parameter, and All removes it again`, `composes the filters with the search in one request`, `resets to the first page on a filter change and shows the filtered total`)
- **INV-LIST-6.** The Template filter lists device templates, each labelled with its
  vendor and model. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`templateOptionLabel`); `frontend/src/api/templates.ts` (`useTemplates`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`sends no filter parameter at All, and labels templates with vendor and model`)
- **INV-LIST-7.** The saved filter is read and written only through
  `parseSavedInventoryFilter` and `serializeInventoryFilter`: a filter at All is
  omitted, and an old object holding only `search` still loads. \
  Enforced in: `frontend/src/lib/inventoryFilters.ts` (`parseSavedInventoryFilter`, `serializeInventoryFilter`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`persists every field, merges the search write with the filters, and round-trips`, `an old saved { search } object keeps working`, `restores saved filters into the controls and the first request`)
- **INV-LIST-8.** A saved status or topology outside the known values, or a saved
  template id that is not among the loaded device templates, falls back to All and is
  never sent; the device query waits while a saved template id is being checked. \
  Enforced in: `frontend/src/lib/inventoryFilters.ts` (`parseSavedInventoryFilter`); `frontend/src/pages/InventoryPage.tsx` (`InventoryPage`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`falls back to All for a stale saved status, topology, or template and never sends them`, `a stale saved template is dropped from the next write while other fields persist`)
- **INV-LIST-9.** A saved search applies at once, never through the debounce; typing is
  debounced 300 ms before it is applied and saved. A filter change made before the
  preferences finish loading is held until they arrive and then saved merged over the
  loaded filter, so it never saves an empty search over the saved one (issue #985). \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`InventoryPage`); `frontend/src/stores/preferencesStore.ts` (`usePreferencesStore`, `mergePreLoadFilter`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`a filter change made before the preferences load keeps the saved search (issue #985)`, `a saved search that loads after mount applies at once and survives a filter change`, `typing after the load still debounces, then applies and persists the typed value`, `debounces user input into the query and persists it as a saved filter`)
- **INV-LIST-10.** Clear filters resets the search and every filter, the request, and
  the saved state; an empty filtered result shows a second Clear filters control. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`InventoryPage`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`Clear filters resets every control, the request, and the saved state`, `shows a filtered-empty state with a working Clear control, not the no-devices state`)
- **INV-LIST-11.** Rows per page offers 25, 50, 100, and 200, defaults to 50, is saved
  per user, and returns to the first page when changed. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`PAGE_SIZE_OPTIONS`, `InventoryPage`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`changing the page-size selector updates the store, resets to the first page, and debounces a preferences patch`)
- **INV-LIST-12.** When the listed rows change, an expanded row that is still listed
  stays expanded, an expanded row that left the list is dropped, and the bulk selection
  is cleared. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`InventoryPage`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`keeps an expanded row open when a list change still contains it`, `drops an expanded row whose id left the list and does not resurrect it`, `still clears the bulk selection on a list change while keeping the expansion`)
- **INV-LIST-13.** An expanded row lists the device's ports and, per port, the device
  and port it is cabled to, matched by port name. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`ExpandedPortsRow`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`resolves a connected port to the other device's name and port, linked to that device`, `shows an unconnected port as Not connected`)
- **INV-LIST-14.** Selection checkboxes and the Actions column (Copy) are shown to admins
  only; the bulk-action bar appears once an admin selects a device. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`InventoryPage`, `DeviceRow`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`hides admin-only controls for a non-admin user`, `shows the bulk-action bar after an admin selects a device`)
- **INV-LIST-15.** Select-all covers the current page only. Bulk delete calls the
  single delete route once per selected device with no bulk endpoint, waits for every
  call, and reports the counts; when any refusal was `device_cabled` it says to remove
  their cables first. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`handleBulkDelete`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`selects and deselects every row`, `deletes every selected device and reports the count on full success`, `reports a partial failure count when one delete fails`, `says remove the cables first when a bulk delete is refused as cabled (issue #940)`)
- **INV-LIST-16.** Copy creates a new device and then copies the source's ports one at
  a time; the toast reports how many ports were copied or failed, and a failed port
  copy does not undo the device. \
  Enforced in: `frontend/src/pages/InventoryPage.tsx` (`handleCopy`) \
  Pinned by: `frontend/src/test/pages/InventoryPage.test.tsx` (`duplicates a device with its ports and reports the count`, `reports the count of ports that failed to copy without failing the whole operation`, `shows an error toast when the device create itself fails`)

**Out of scope.** The filter panel's layout component (`ListFilterPanel`) is shared by
every list page and carries no inventory rule. Cabling connections shown in an
expanded row are `topology.md`'s.

### 8.7 Batch device fetch

**What it does.** A caller that needs many devices at once (the topology canvas,
reservations) fetches them by id in one request instead of one request per device.

**Surfaces.** Route `POST /devices/batch`.

**Rules.**

- **INV-BATCH-1.** The body holds at most 500 ids; more answers 422 before any work. \
  Enforced in: `services/inventory/app/routers/devices.py` (`DeviceBatchRequest`, `DEVICE_BATCH_MAX_IDS`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_batch_over_cap_returns_422`, `test_batch_at_cap_accepted`); `tests/integration/test_device_batch_fetch.py` (`test_batch_over_cap_returns_422`)
- **INV-BATCH-2.** Repeated ids are collapsed, and an empty list answers
  `{"items": []}`. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices_batch`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_batch_dedupes_repeated_ids`, `test_batch_empty_list_returns_empty_items`)
- **INV-BATCH-3.** Unknown ids, and for a non-admin ids outside their visibility, are
  left out of the answer; the batch never answers 403 or 404 for them, and the order
  of `items` is not guaranteed. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices_batch`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_batch_admin_gets_all_requested_and_unknown_ids_omitted`, `test_batch_non_admin_with_no_visible_ids_returns_empty`); `tests/integration/test_device_batch_fetch.py` (`test_non_admin_batch_omits_devices_outside_visibility`)
- **INV-BATCH-4.** Unlike the list, the batch does not force `dut_only` for a
  non-admin: a visible non-Management device is returned. By decision; see the
  `get_devices_batch` docstring. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices_batch`) \
  Pinned by: none
- **INV-BATCH-5.** A non-admin batch fails closed: 401 `invalid token subject` for a
  non-UUID subject and 503 when the visibility lookup fails. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices_batch`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_batch_malformed_token_subject_returns_401`, `test_batch_fails_closed_on_auth_outage`)

**Out of scope.** How reservations and cabling use the batch (`reservations.md`,
`topology.md`).

### 8.8 Ports

**What it does.** An admin adds ports to a device, one at a time or many at once with a
name prefix and a starting number; users see the ports of devices they can see.

**Surfaces.** User interface `frontend/src/components/devices/PortsSection.tsx` on the
device page; routes under `/devices/{id}/ports` and `/ports/{id}`.

**Rules.**

- **INV-PORT-1.** For an admin, the port list of an unknown device id is an empty list,
  not a 404. \
  Enforced in: `services/inventory/app/routers/ports.py` (`get_device_ports`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_list_ports_nonexistent_device_returns_empty`)
- **INV-PORT-2.** A non-admin reading the port list of a hidden or unknown device, or a
  single port on a hidden device, gets 404. \
  Enforced in: `services/inventory/app/routers/ports.py` (`get_device_ports`, `get_port_by_id`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_user_denied_ports_of_invisible_device`, `test_user_lists_ports_of_visible_device`, `test_user_gets_port_of_visible_device`)
- **INV-PORT-3.** A port is created on an existing device (404 `Device not found`
  otherwise) from a template of type `port` (422 `Template not found` or
  `Template is not a port template` otherwise), with `field_data` validated by
  INV-FIELD-1 to INV-FIELD-7. \
  Enforced in: `services/inventory/app/services/port_service.py` (`create_port`, `create_ports_bulk`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_create_port_device_not_found`, `test_create_port_template_not_found`, `test_create_port_wrong_template_type`, `test_create_port_unknown_field`, `test_bulk_create_ports_wrong_template_type`)
- **INV-PORT-4.** A port name is 1 to 255 characters. \
  Enforced in: `services/inventory/app/schemas/port.py` (`PortCreate`) \
  Pinned by: `services/inventory/tests/test_schema_bounds.py` (`test_port_name_empty_rejected`, `test_port_name_over_cap_rejected`)
- **INV-PORT-5.** Bulk create makes `instances` ports (1 to 200) named
  `<name_prefix><starting_index + i>`, with a prefix of 1 to 200 characters and a
  starting index of at least 0, all sharing one template and one `field_data`. \
  Enforced in: `services/inventory/app/schemas/port.py` (`BulkPortCreate`); `services/inventory/app/services/port_service.py` (`create_ports_bulk`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_bulk_create_ports`, `test_bulk_create_ports_instances_exceeds_max`, `test_bulk_create_ports_invalid_instances_zero`, `test_bulk_create_ports_negative_starting_index`, `test_bulk_create_ports_empty_prefix`); `services/inventory/tests/test_port_service_unit.py` (`test_create_ports_bulk_naming`)
- **INV-PORT-6.** The longest generated name (the prefix plus the last index) must fit
  the 255-character column; otherwise the request answers 422 `generated port names
  would exceed 255 characters; use a shorter name_prefix or a smaller starting_index`
  and no port is created. \
  Enforced in: `services/inventory/app/schemas/port.py` (`BulkPortCreate`, `PORT_NAME_MAX_LENGTH`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_bulk_create_ports_name_over_column_width_is_422`, `test_bulk_create_ports_last_name_exactly_at_column_width_is_accepted`)
- **INV-PORT-7.** A port update changes only its name and `field_data`; `field_data` is
  validated against the port's template. \
  Enforced in: `services/inventory/app/services/port_service.py` (`update_port`) \
  Pinned by: `services/inventory/tests/test_ports.py` (`test_update_port`, `test_update_port_field_data`, `test_update_port_invalid_field_data`)
- **INV-PORT-8.** A port delete is refused while any cabling connection names the port
  (cabling stores ports by name, with no reference to the port id): inventory asks
  cabling's by-port lookup (TOPO-CONNINT-2) for (device, current name) and answers 409
  `{"error": "port_cabled", "connection_count", "connection_ids"}` (the true count and
  the sorted, capped id sample), or 503 `Could not verify port is not cabled` when
  cabling is unreachable, answers non-200, or answers a body missing or mistyping either
  key. The port is untouched on both refusals. An unknown port is 404 without asking
  cabling. There is no cascade and no force flag; the admin removes the cables first. \
  Enforced in: `services/inventory/app/services/port_service.py` (`delete_port`); `services/inventory/app/services/port_cabling_guard.py` (`assert_port_uncabled`, `find_connections_naming_port`) \
  Pinned by: `services/inventory/tests/test_port_cabling_guard.py` (`test_delete_cabled_port_is_409_and_port_survives`, `test_delete_when_cabling_down_is_503_and_port_survives`, `test_delete_uncabled_port_is_204`, `test_delete_unknown_port_is_404_without_asking_cabling`, `test_cabled_port_is_409_port_cabled_with_true_count_and_sorted_ids`, `test_transport_failure_is_503`, `test_non_200_is_503`, `test_unparseable_body_is_503_never_uncabled`, `test_non_json_body_is_503`); `services/inventory/tests/test_ports.py` (`test_delete_port`, `test_delete_port_not_found`); `tests/integration/test_port_cabling_guard.py` (`test_cabled_port_refuses_delete_and_rename_until_the_cable_is_removed`)
- **INV-PORT-9.** Port names need not be unique on a device; nothing checks for a
  repeat. \
  Enforced in: `services/inventory/app/models/port.py` (`Port`) \
  Pinned by: none
- **INV-PORT-10.** A port update that changes the name is refused exactly as INV-PORT-8
  refuses a delete (409 `port_cabled` or 503, asked about the CURRENT name, the one
  cables reference), and the name stays as it was. An update that repeats the current
  name or changes only `field_data` never asks cabling, so a cabled port stays editable.
  The guard runs after the `field_data` validation. \
  Enforced in: `services/inventory/app/services/port_service.py` (`update_port`); `services/inventory/app/services/port_cabling_guard.py` (`assert_port_uncabled`) \
  Pinned by: `services/inventory/tests/test_port_cabling_guard.py` (`test_rename_cabled_port_is_409_and_name_unchanged`, `test_rename_when_cabling_down_is_503_and_name_unchanged`, `test_rename_uncabled_port_succeeds`, `test_update_that_is_not_a_rename_never_asks_cabling`); `tests/integration/test_port_cabling_guard.py` (`test_cabled_port_refuses_delete_and_rename_until_the_cable_is_removed`)

**Out of scope.** Cabling connections between ports (`topology.md`).

### 8.9 Device groups

**What it does.** An admin sorts devices into named device groups and grants each group
to one or more user groups; that grant is what lets a non-admin see a device.

**Surfaces.** User interface `frontend/src/pages/admin/DeviceGroupsPage.tsx` and
`frontend/src/pages/admin/DeviceGroupDetailPage.tsx`; routes under `/device-groups`.

**Rules.**

- **INV-GRP-1.** The group list and the group detail are admin reads; the detail
  carries the member devices and the granted user groups. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`list_device_groups_endpoint`, `get_device_group_endpoint`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_list_device_groups`, `test_get_device_group`, `test_user_cannot_list_device_groups`)
- **INV-GRP-2.** A group name is unique (409 `Device group '<name>' already exists`), and
  a description is at most 2000 characters. \
  Enforced in: `services/inventory/app/schemas/device_group.py` (`DeviceGroupCreate`); `services/inventory/app/services/device_group_service.py` (`create_device_group`, `update_device_group`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_create_device_group_duplicate_name`); `services/inventory/tests/test_device_group_service_unit.py` (`test_update_device_group_duplicate_name`); `services/inventory/tests/test_schema_bounds.py` (`test_device_group_description_over_cap_rejected`)
- **INV-GRP-3.** Deleting a group removes its memberships and grants. \
  Enforced in: `services/inventory/app/models/device_group.py` (`DeviceGroup`); `services/inventory/app/services/device_group_service.py` (`delete_device_group`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_delete_device_group_cascades`)
- **INV-GRP-4.** Bulk add takes at most 500 device ids, skips a device already in the
  group, and answers `{added, skipped}`. \
  Enforced in: `services/inventory/app/schemas/device_group.py` (`BulkDeviceIds`); `services/inventory/app/services/device_group_service.py` (`bulk_add_devices`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_bulk_add_devices`, `test_bulk_add_devices_skip_duplicates`); `services/inventory/tests/test_schema_bounds.py` (`test_bulk_device_ids_over_cap_rejected`)
- **INV-GRP-5.** Adding a device to any group other than `No Pool` removes it from
  `No Pool`. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`bulk_add_devices`, `_remove_from_no_pool`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_bulk_add_devices_removes_from_no_pool`)
- **INV-GRP-6.** A concurrent add of the same membership is recounted after the
  unique-constraint conflict instead of answering 500. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`bulk_add_devices`) \
  Pinned by: `services/inventory/tests/test_device_group_service_unit.py` (`test_bulk_add_devices_concurrent_duplicate_skips_gracefully`)
- **INV-GRP-7.** Every requested device id is resolved before anything is inserted. When
  any id names no device the request answers 422 `Devices not found: <ids>` (every
  unknown id, sorted, comma separated) and no device in that request is added: the
  `{added, skipped}` answer has no per-row slot, so the add is all or nothing. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`bulk_add_devices`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_bulk_add_devices_mixed_unknown_id_is_422_naming_it_and_adds_nothing`, `test_bulk_add_devices_all_unknown_ids_are_422_sorted_and_deduplicated`); `services/inventory/tests/test_storage_constraints.py` (`test_group_bulk_add_mixed_unknown_id_is_422_and_adds_nothing_with_fk_on`)
- **INV-GRP-8.** Bulk remove answers `{removed, not_found}`. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`bulk_remove_devices`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_bulk_remove_devices`, `test_bulk_remove_devices_not_found`)
- **INV-GRP-9.** Grants take at most 500 user group ids, which are not checked against
  the auth service; add skips an existing grant. \
  Enforced in: `services/inventory/app/schemas/device_group.py` (`BulkUserGroupIds`); `services/inventory/app/services/device_group_service.py` (`bulk_add_user_groups`, `bulk_remove_user_groups`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_bulk_add_user_groups`, `test_bulk_add_user_groups_skip_duplicates`, `test_bulk_remove_user_groups`); `services/inventory/tests/test_schema_bounds.py` (`test_bulk_user_group_ids_over_cap_rejected`)
- **INV-GRP-10.** `GET /device-groups/device/{id}` answers 404
  `Device <id> not found` for an unknown device and 200 `[]` for a device in no group. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`get_device_groups_for_device_endpoint`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_device_groups_for_device_nonexistent_device_returns_404`, `test_device_groups_for_device_ungrouped_device_returns_empty_list`)
- **INV-GRP-11.** It lists each group the device is in with its granted user groups,
  whose names are read from auth's `GET /groups` with the caller's JWT; an auth failure
  answers 503. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`get_device_groups_for_device_endpoint`, `_fetch_user_group_names`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_device_groups_for_device_resolves_names`, `test_device_groups_for_device_returns_503_when_auth_service_down`, `test_fetch_user_group_names_success_maps_wanted_ids`)
- **INV-GRP-12.** The name lookup pages through auth's group list at auth's maximum
  page size (500) until every wanted user group is named or the list ends, so a user
  group on any page gets its name; only a user group auth no longer lists has a null
  name. A failure on any page answers the 503 of INV-GRP-11. \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`_fetch_user_group_names`, `_AUTH_GROUP_PAGE_SIZE`) \
  Pinned by: `services/inventory/tests/test_device_groups.py` (`test_fetch_user_group_names_pages_until_every_wanted_id_is_named`, `test_fetch_user_group_names_stops_once_every_wanted_id_is_named`, `test_fetch_user_group_names_unknown_id_stops_at_end_of_list_with_no_name`, `test_fetch_user_group_names_second_page_failure_is_503`)
- **INV-GRP-13.** For a non-admin, a hidden device answers the same 404 as an unknown
  one (INV-VIS-6). \
  Enforced in: `services/inventory/app/routers/device_groups.py` (`get_device_groups_for_device_endpoint`) \
  Pinned by: `services/inventory/tests/test_device_read_visibility_gate.py` (`test_hidden_device_404_matches_unknown_id_404`, `test_device_groups_for_device_non_admin_visible_returns_group_data`)
- **INV-GRP-14.** The service creates the `No Pool` group at startup when no group has
  that name. \
  Enforced in: `services/inventory/app/main.py` (`_seed_no_pool`) \
  Pinned by: `tests/e2e/test_device_groups.py` (`test_device_groups_list_shows_no_pool`)
- **INV-GRP-15.** A device left in no group, by a bulk remove or a group delete, is not
  put back into `No Pool`, and is then visible to admins only. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`bulk_remove_devices`, `delete_device_group`) \
  Pinned by: none
- **INV-GRP-16.** Nothing stops an admin from renaming or deleting `No Pool`; the
  default group is found by name, so new devices then join no group. \
  Enforced in: `services/inventory/app/services/device_group_service.py` (`get_no_pool_group`, `add_device_to_no_pool`) \
  Pinned by: none
- **INV-GRP-17.** A group name is 1 to 100 characters. \
  Enforced in: `services/inventory/app/schemas/device_group.py` (`DeviceGroupCreate`, `DeviceGroupUpdate`) \
  Pinned by: none

**Out of scope.** User groups and their membership are the auth service's
(`identity-and-access.md`). Cabling's device-group boundary check on connections calls
the by-device lookup (`topology.md`).

### 8.10 Driver packages

**What it does.** An admin uploads a driver package (a zip or gzipped tar archive) and
says what kind of equipment it drives; templates then point at it, and execution
downloads it to talk to devices.

**Surfaces.** User interface `frontend/src/pages/admin/DriversPage.tsx` (upload, list,
download, delete); routes under `/drivers`. The package format is in
[DRIVERS.md](../DRIVERS.md); loading and running a package is `device-configuration.md`'s.

**Rules.**

- **INV-DRV-1.** Any signed-in user may list and read driver packages. \
  Enforced in: `services/inventory/app/routers/drivers.py` (`get_drivers`, `get_driver_by_id`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_user_can_list_drivers`, `test_user_can_get_driver`)
- **INV-DRV-2.** Download returns the stored archive bytes; execution downloads through
  the internal route. \
  Enforced in: `services/inventory/app/routers/drivers.py` (`download_driver_file`, `download_driver_file_internal`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_download_driver`, `test_internal_download_driver`, `test_internal_download_driver_bad_token`)
- **INV-DRV-17.** Any signed-in user may download a package (recorded in
  [ROLES.md](../ROLES.md), Driver Management); the answer is an attachment named after
  the package's file name, with content type `application/zip` for a `.zip` name and
  `application/gzip` otherwise. \
  Enforced in: `services/inventory/app/routers/drivers.py` (`download_driver_file`) \
  Pinned by: none
- **INV-DRV-18.** A file replacement re-reads both capability flags from the new
  archive and records the replacing admin as `uploaded_by`. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`replace_driver_file`) \
  Pinned by: none
- **INV-DRV-3.** The file name must end in `.zip` or `.tar.gz` (case-insensitive) and
  may not contain `/`, `\`, `..`, or start with `~`; otherwise 422. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`_validate_filename`, `ALLOWED_EXTENSIONS`) \
  Pinned by: `services/inventory/tests/test_driver_service_unit.py` (`test_validate_filename_zip`, `test_validate_filename_tar_gz`, `test_validate_filename_invalid`, `test_validate_filename_rejects_path_traversal`); `services/inventory/tests/test_drivers.py` (`test_create_driver_invalid_extension`)
- **INV-DRV-4.** A file larger than `DRIVER_MAX_SIZE_BYTES` (default 10 MB) answers 422
  `File too large: max <N> bytes`. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`create_driver`, `replace_driver_file`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_create_driver_exceeds_size_limit`); `services/inventory/tests/test_driver_service_unit.py` (`test_replace_driver_file_too_large`)
- **INV-DRV-5.** The connection type must be one of `Management`, `Layer 1 Switch`,
  `Layer 2 Switch`, `Layer 3 Switch`, or `Hypervisor`; otherwise 422. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`_validate_connection_type`); `services/inventory/app/models/driver_package.py` (`ConnectionType`, `VALID_CONNECTION_TYPES`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_create_driver_each_connection_type`, `test_create_driver_invalid_connection_type`)
- **INV-DRV-6.** A package's identity for caching is the SHA256 of the uploaded bytes,
  stored with the size and the uploader's username. The archive content itself is not
  validated at upload. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`create_driver`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_create_driver`); `services/inventory/tests/test_driver_service_unit.py` (`test_create_driver_success`)
- **INV-DRV-7.** `supports_dry_run` and `supports_vrf` are read from a top-level
  `driver_metadata.json` in the archive and are false unless the file declares them;
  an unreadable archive, a missing or malformed file, or a non-object value reads as
  false. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`_parse_driver_metadata`, `create_driver`) \
  Pinned by: `services/inventory/tests/test_driver_service_unit.py` (`test_create_driver_persists_the_declared_capability_flags`, `test_parse_supports_dry_run_malformed_json_returns_false`, `test_parse_supports_dry_run_corrupt_zip_returns_false`, `test_parse_supports_dry_run_non_dict_metadata_returns_false`, `test_the_two_capability_flags_are_read_independently`)
- **INV-DRV-8.** A package name is unique; a duplicate answers 409
  `Driver with name '<name>' already exists`. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`create_driver`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_create_driver_duplicate_name`); `services/inventory/tests/test_driver_service_unit.py` (`test_create_driver_duplicate_name`)
- **INV-DRV-9.** The archive is stored under `<driver id>/<file name>` before the row is
  committed, and a duplicate-name refusal deletes it again (a failed delete is
  ignored). \
  Enforced in: `services/inventory/app/services/driver_service.py` (`create_driver`) \
  Pinned by: none
- **INV-DRV-10.** A metadata update may change the name, description, and connection
  type (validated as INV-DRV-5); a name clash answers 409. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`update_driver`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_update_driver_metadata`, `test_update_driver_connection_type`, `test_update_driver_duplicate_name`); `services/inventory/tests/test_driver_service_unit.py` (`test_update_driver_invalid_connection_type`)
- **INV-DRV-11.** Changing a package's connection type re-checks the templates that use
  it against INV-TPL-13: a change to `Hypervisor` while a device template uses the
  package answers 409 `Cannot change connection_type: device templates use this driver,
  and device templates cannot use a Hypervisor-type driver`, and a change away from
  `Hypervisor` while a dynamic template uses it answers 409 `Cannot change
  connection_type: dynamic templates use this driver, and dynamic templates require a
  Hypervisor-type driver`. Nothing is changed on a refusal. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`update_driver`, `_assert_templates_allow_connection_type`) \
  Pinned by: `services/inventory/tests/test_dynamic_templates.py` (`test_update_driver_used_by_device_template_to_hypervisor_is_409`, `test_update_recipe_used_by_dynamic_template_to_non_hypervisor_is_409`, `test_update_driver_connection_type_within_contract_still_succeeds`)
- **INV-DRV-12.** Replacing the file stores the new archive, deletes the old one when its
  key differs (a failed delete is ignored), and updates the file name, size, and
  SHA256. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`replace_driver_file`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_replace_driver_file`); `services/inventory/tests/test_driver_service_unit.py` (`test_replace_driver_file_success`, `test_replace_driver_file_swallows_delete_error_for_old_key`); `tests/integration/test_driver_flow.py` (`test_replace_driver_file_updates_sha256`)
- **INV-DRV-13.** A package any template references cannot be deleted (409
  `Cannot delete driver: templates still reference it`); otherwise the archive delete
  is attempted, its failure ignored, and the row removed. \
  Enforced in: `services/inventory/app/services/driver_service.py` (`delete_driver`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_delete_driver_referenced_by_template`, `test_delete_driver`); `services/inventory/tests/test_driver_service_unit.py` (`test_delete_driver_swallows_storage_delete_error`)
- **INV-DRV-14.** Storage is MinIO when `MINIO_ENDPOINT` is set (the bucket is created
  at startup when missing) and the local directory `DRIVER_STORAGE_PATH` otherwise. \
  Enforced in: `services/inventory/app/storage.py` (`init_storage`) \
  Pinned by: `services/inventory/tests/test_storage.py` (`test_init_storage_local_backend`, `test_init_storage_minio_backend`, `test_init_storage_minio_bucket_exists`)
- **INV-DRV-15.** On local storage every read, write, and delete refuses a key that
  resolves outside the storage root. \
  Enforced in: `services/inventory/app/storage.py` (`_resolve_local`) \
  Pinned by: `services/inventory/tests/test_storage.py` (`test_upload_local_rejects_traversal_key`, `test_download_local_rejects_traversal_key`, `test_delete_local_rejects_traversal_key`)
- **INV-DRV-16.** The drivers page requires a name, a connection type (Hypervisor
  included), and a file before it uploads, and shows the
  server's detail on a refusal. \
  Enforced in: `frontend/src/pages/admin/DriversPage.tsx` (`DriversPage`, `CONNECTION_TYPES`) \
  Pinned by: `frontend/src/test/pages/DriversPage.test.tsx` (`upload validates name required`, `upload validates connection type required`, `upload validates file required`, `upload select offers the Hypervisor connection type`, `surfaces the server detail message when upload fails`)

**Out of scope.** The Draft with AI panel (`ai-features.md`); extracting a published
config schema and the config-schema route (`device-configuration.md`).

### 8.11 Hypervisor registry

**What it does.** An admin registers each virtualization host that dynamic templates
can create instances on, with a reference to a stored credential rather than the
credential itself.

**Surfaces.** User interface `frontend/src/pages/admin/HypervisorsPage.tsx`; routes
under `/hypervisors`.

**Rules.**

- **INV-HYP-1.** Every user-facing hypervisor route, reads included, is admin-only. \
  Enforced in: `services/inventory/app/routers/hypervisors.py` (`get_hypervisors`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_user_cannot_list_hypervisors`, `test_user_cannot_create_hypervisor`)
- **INV-HYP-2.** A hypervisor name is unique (409
  `Hypervisor with name '<name>' already exists`); the type is free text. \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`create_hypervisor`, `update_hypervisor`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_create_hypervisor_duplicate_name_409`, `test_create_hypervisor`)
- **INV-HYP-3.** Registration asks the secrets service whether `secret_id` exists: a 404
  answers 422 `Secret does not exist`. \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`validate_secret_exists`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_create_hypervisor_missing_secret_422`)
- **INV-HYP-4.** A transport error or any other non-200 answer from the secrets service
  fails closed with 503. \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`validate_secret_exists`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_create_hypervisor_secrets_transport_error_503`, `test_create_hypervisor_secrets_5xx_503`)
- **INV-HYP-5.** An update changes only the fields it names and does not consult the
  secrets service when `secret_id` is absent or unchanged. \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`update_hypervisor`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_update_hypervisor_without_secret_change_skips_validation`)
- **INV-HYP-6.** An update that changes `secret_id` validates the new one as INV-HYP-3
  and INV-HYP-4. \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`update_hypervisor`) \
  Pinned by: none
- **INV-HYP-7.** A hypervisor any template references cannot be deleted (409
  `Cannot delete hypervisor: templates still reference it`). \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`delete_hypervisor`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_delete_hypervisor_blocked_by_template`, `test_delete_hypervisor`)
- **INV-HYP-8.** The internal read answers exactly `id`, `name`, `endpoint`,
  `hypervisor_type`, `secret_id`, and `enabled`. \
  Enforced in: `services/inventory/app/schemas/hypervisor.py` (`HypervisorInternalResponse`); `services/inventory/app/routers/hypervisors.py` (`get_hypervisor_internal`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_internal_get_hypervisor`, `test_internal_get_hypervisor_not_found`)
- **INV-HYP-9.** The by-secret lookup lists `{id, name}` of every hypervisor whose
  `secret_id` is the given id, sorted by name, without paging; an unknown secret id is
  an empty list. \
  Enforced in: `services/inventory/app/services/hypervisor_service.py` (`list_hypervisors_by_secret`); `services/inventory/app/schemas/hypervisor.py` (`HypervisorSecretRefResponse`) \
  Pinned by: `services/inventory/tests/test_hypervisors.py` (`test_by_secret_internal_lists_referencing_hypervisors`, `test_by_secret_internal_unknown_secret_returns_empty`); `tests/integration/test_dynamic_resources.py` (`test_secret_delete_refused_while_hypervisor_references_it`)
- **INV-HYP-10.** The hypervisors page labels a secret reference that no longer exists
  as `Deleted secret <first 8>` once the secrets list has loaded (a truncated id
  before), and the edit form keeps it selected with a warning. \
  Enforced in: `frontend/src/pages/admin/HypervisorsPage.tsx` (`HypervisorsPage`) \
  Pinned by: `frontend/src/test/pages/HypervisorsPage.test.tsx` (`list labels an orphaned secret reference explicitly`, `list keeps the neutral truncated id while secrets are still loading`, `edit form selects the orphaned option and warns`)
- **INV-HYP-11.** Name, endpoint, and type must not be blank or whitespace, on create
  and on update. \
  Enforced in: `services/inventory/app/schemas/hypervisor.py` (`HypervisorCreate`, `HypervisorUpdate`) \
  Pinned by: none

**Out of scope.** Using the hypervisor and its credential to create instances
(`dynamic-resources.md`); the secrets service's own delete guard (`identity-and-access.md`
or the secrets area).

### 8.12 Bulk import and export

**What it does.** An admin downloads every device or every template as a CSV or JSON
file, and uploads such a file to create or update many at once, optionally as a dry run
that only reports what would happen row by row.

**Surfaces.** User interface `frontend/src/components/ui/BulkImportExport.tsx` on the
inventory and templates pages; routes `GET /devices/export`, `POST /devices/import`,
`GET /templates/export`, `POST /templates/import`. Formats are in
[BULK_IMPORT_EXPORT.md](../BULK_IMPORT_EXPORT.md).

**Rules.**

- **INV-BULK-1.** Export and import are admin-only. \
  Enforced in: `services/inventory/app/routers/bulk.py` (`export_devices`, `import_devices_endpoint`, `export_templates`, `import_templates_endpoint`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_export_requires_admin`, `test_import_requires_admin`)
- **INV-BULK-2.** A device record carries `template_name` instead of the template id,
  and a template record carries `driver_name` instead of the driver id; JSON export is
  `{resource, version: 1, items}`. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`device_to_record`, `template_to_record`, `records_to_json`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_export_devices_json_carries_template_name_not_uuid`, `test_export_templates_json_carries_driver_name`)
- **INV-BULK-3.** A device export includes `field_data` with password values in clear;
  a template export carries no `hypervisor_id`. Known gap, see #1024. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`device_to_record`, `template_to_record`) \
  Pinned by: none
- **INV-BULK-4.** Import accepts a JSON list or an object with an `items` list; invalid
  JSON, any other shape, or a non-list `items` answers 422 for the whole file. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`_parse_json`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_parse_json_invalid_document_raises_422`, `test_parse_json_accepts_bare_list`, `test_parse_json_accepts_items_wrapper`, `test_parse_json_scalar_document_rejected`, `test_parse_json_items_not_a_list_rejected`)
- **INV-BULK-5.** The file is decoded as UTF-8, and a leading byte order mark is
  dropped. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`parse_import`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_parse_import_strips_utf8_bom_for_csv`)
- **INV-BULK-6.** A CSV import keeps only the known columns of its resource. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`_parse_csv`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_parse_csv_keeps_only_known_columns`)
- **INV-BULK-7.** Each row is processed and committed on its own: a rejected row is
  rolled back with a `reason` and the rest still run; the report counts created,
  updated, skipped, and rejected rows and lists one result per row. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`, `import_templates`, `_tally`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_one_bad_row_does_not_abort_batch`); `services/inventory/tests/test_bulk_service_unit.py` (`test_import_devices_failing_row_before_valid_row_still_creates`, `test_import_templates_failing_row_before_valid_row_still_creates`)
- **INV-BULK-8.** A row matches an existing resource by its exact (trimmed) name and
  updates it in place; no other row matches. No importer ever reports `skip`. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`, `import_templates`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_import_existing_device_is_update`, `test_template_reexport_reimport_is_noop_update`)
- **INV-BULK-9.** A device row without a name, without a template name, or naming a
  template that does not exist is rejected with that reason, and so is a new device row
  without a topology type (`missing required field: topology_type`). A schema error
  rejects the row with each failing field and its message (`<field>: <message>`), never
  the row's input values. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_missing_name_is_rejected`, `test_one_bad_row_does_not_abort_batch`, `test_device_import_create_without_topology_type_names_the_field`, `test_device_import_schema_error_reason_names_field_and_omits_input`); `services/inventory/tests/test_bulk_service_unit.py` (`test_import_devices_missing_template_name_rejected`)
- **INV-BULK-10.** A device row goes through the same create or update functions as the
  interactive routes, so their rules (INV-DEV-1 to INV-DEV-3, INV-FIELD-1 to
  INV-FIELD-7, INV-POLL-1) reject it on a committed import. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_import_devices_unknown_field_rolls_back_via_http_exception`, `test_import_devices_bad_enum_rolls_back_via_validation_error`)
- **INV-BULK-11.** A dry run writes nothing and returns the same report shape with
  `dry_run: true`. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`, `import_templates`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_dry_run_writes_nothing`); `tests/integration/test_bulk_import_export.py` (`test_device_import_dry_run_writes_nothing`)
- **INV-BULK-12.** A device update row leaves out every column it does not carry or
  leaves empty, so an omitted `field_data`, poll interval, status, or topology type
  keeps its stored value (the template importer's rule, INV-BULK-14). A row that
  carries `field_data` replaces the whole object (INV-DEV-9). An import cannot clear a
  device's poll interval; the device update route can. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_device_json_reimport_omitting_columns_keeps_stored_values`, `test_device_csv_export_drop_columns_reimport_keeps_omitted_values`, `test_device_csv_name_and_template_only_is_a_no_op_update`, `test_import_existing_device_is_update`)
- **INV-BULK-13.** A template row resolves its driver by name (rejected when the name is
  unknown) and goes through the template create or update functions. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_templates`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_template_import_resolves_driver_by_name`, `test_template_import_rejects_unknown_driver`); `services/inventory/tests/test_bulk_service_unit.py` (`test_import_templates_hypervisor_driver_on_device_rejects_via_http_exception`)
- **INV-BULK-14.** A template update row leaves out every column it does not carry, so
  an omitted driver, vendor, model, exclusive flag, or other field keeps its stored
  value. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_templates`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_template_reimport_omitting_vendor_model_preserves_them`); `services/inventory/tests/test_bulk_service_unit.py` (`test_import_templates_omitting_exclusive_preserves_existing`, `test_import_templates_omitting_driver_preserves_existing`)
- **INV-BULK-15.** A template row cannot carry a hypervisor, so a `dynamic` template row
  is rejected on create. Known gap, see #1024. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_templates`) \
  Pinned by: none
- **INV-BULK-16.** The CSV `exclusive` cell reads true for `1`, `true`, `yes`, or `y`
  (any case) and false for any other non-empty value. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`_coerce_bool`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_coerce_bool_string_truthiness`, `test_coerce_bool_empty_returns_default`)
- **INV-CSV-1.** CSV export passes each free-text cell (device `name` and
  `template_name`; template `name`, `driver_name`, `icon`, `description`, `vendor`,
  `model`, `part_number`) through `csv_safe_cell`, which prefixes one quote when the
  first non-space character is `=`, `+`, `-`, `@`, a tab, or a carriage return. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`records_to_csv`, `DEVICE_CSV_TEXT_COLUMNS`, `TEMPLATE_CSV_TEXT_COLUMNS`); `services/common/herd_common/csv_safety.py` (`csv_safe_cell`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_export_devices_csv_neutralizes_formula_trigger_cells`, `test_export_templates_csv_neutralizes_formula_trigger_cells`); `services/common/tests/test_csv_safety.py` (`test_leading_trigger_is_neutralized`, `test_leading_space_then_trigger_is_neutralized`)
- **INV-CSV-2.** Enumerations, numbers, booleans, and the `field_data` and `sections`
  JSON cells are never quoted. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`records_to_csv`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_records_to_csv_neutralizes_only_the_named_text_columns`, `test_records_to_csv_json_blob_column_never_quoted_even_if_named_a_text_column`)
- **INV-CSV-3.** CSV import strips one leading quote from a text cell only when a
  trigger follows it, so an exported value round-trips and an apostrophe-led name is
  kept; JSON import strips nothing. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`_parse_csv`, `parse_import`); `services/common/herd_common/csv_safety.py` (`csv_unsafe_cell`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_parse_csv_strips_neutralizing_quote_from_named_text_columns`, `test_parse_csv_leaves_apostrophe_led_value_alone`); `services/inventory/tests/test_bulk.py` (`test_device_csv_roundtrip_with_formula_trigger_name`, `test_template_csv_roundtrip_with_formula_trigger_name`)
- **INV-BULK-17.** The import dialog can run a dry run and lists each rejected row with
  its reason, and it infers the format from the file extension. \
  Enforced in: `frontend/src/components/ui/BulkImportExport.tsx` (`BulkImportExport`) \
  Pinned by: `frontend/src/test/components/BulkImportExport.test.tsx` (`runs a dry-run and shows the per-row reject report`, `infers csv format from the file extension`)
- **INV-BULK-18.** A dry run validates only the row shape (names, references, and the
  request schemas); it does not run the create and update checks of INV-BULK-10, so it
  can report `create` or `update` for a row the committed import rejects.
  Known gap, see #1017. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`import_devices`, `import_templates`) \
  Pinned by: none
- **INV-BULK-19.** `format` is `csv` or `json` (default `json`); any other value answers
  422. \
  Enforced in: `services/inventory/app/routers/bulk.py` (`export_devices`, `import_devices_endpoint`); `services/inventory/app/services/bulk_service.py` (`parse_import`) \
  Pinned by: `services/inventory/tests/test_bulk_service_unit.py` (`test_parse_import_unknown_format_rejected`)
- **INV-BULK-20.** A file that is not valid UTF-8 answers 422 `Import file must be UTF-8
  encoded; re-save it as UTF-8 and retry` for the whole file, and nothing is written. \
  Enforced in: `services/inventory/app/services/bulk_service.py` (`parse_import`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_non_utf8_import_file_is_422_naming_the_encoding`)

**Out of scope.** Topology import and export (`topology.md`). Ports and device groups
are not exported or imported.

### 8.13 Device delete guard

**What it does.** An admin cannot delete a device while a live reservation still
depends on it or while a cable still names it; the refusal says which, so the admin
can cancel the booking or remove the cables first.

**Surfaces.** Route `DELETE /devices/{id}`; the device page and the inventory page's
bulk delete show the refusal (INV-DEV-13, INV-LIST-15).

**Rules.**

- **INV-DEL-1.** An unknown device id answers 404 before any upstream is asked. \
  Enforced in: `services/inventory/app/routers/devices.py` (`delete_device_by_id`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_delete_device_not_found`)
- **INV-DEL-2.** The guard asks reservations' `GET /internal/by-device/{id}` for the
  reservations booking the device and counts those in `PENDING`, `PENDING_PROVISION`,
  or `ACTIVE`, and asks cabling's `GET /internal/forks/by-device/{id}` for the
  reservations whose non-archived fork wiring names the device. \
  Enforced in: `services/inventory/app/services/reservation_guard.py` (`find_blocking_reservations_for_device`, `_BLOCKING_STATUSES`); `services/inventory/app/services/device_delete_guard.py` (`find_cabling_dependents_for_device`) \
  Pinned by: `services/inventory/tests/test_device_config_restore_reservation_guard.py` (`test_find_blocking_reservations_filters_non_blocking_statuses`); `services/inventory/tests/test_device_delete_guard.py` (`test_cabling_call_shape`, `test_matrix`)
- **INV-DEL-3.** When either answer names a reservation, the delete answers 409
  `{"error": "device_in_use", "reservation_ids", "transit_reservation_ids"}`:
  `reservation_ids` is the sorted union of both sources and `transit_reservation_ids`
  the sorted ids that hold the device only as a fork hop. \
  Enforced in: `services/inventory/app/services/device_delete_guard.py` (`assert_device_deletable`) \
  Pinned by: `services/inventory/tests/test_device_delete_guard.py` (`test_matrix`, `test_member_whose_fork_also_touches_device_is_not_transit_only`); `services/inventory/tests/test_devices.py` (`test_delete_device_blocked_by_active_reservation`, `test_delete_device_blocked_as_transit_hop_only`); `tests/integration/test_device_delete_guard.py` (`test_delete_blocked_by_active_reservation_then_succeeds_after_cancel`, `test_transit_switch_on_live_fork_blocks_delete_until_cancel`)
- **INV-DEL-4.** Otherwise, when cabling reports any connection naming the device, the
  delete answers 409 `{"error": "device_cabled", "connection_count",
  "connection_ids"}` with the true count and cabling's sorted, capped id sample;
  `device_in_use` wins when both apply. \
  Enforced in: `services/inventory/app/services/device_delete_guard.py` (`assert_device_deletable`) \
  Pinned by: `services/inventory/tests/test_device_delete_guard.py` (`test_cabled_detail_ids_are_sorted_and_count_is_the_true_total`, `test_matrix`); `services/inventory/tests/test_devices.py` (`test_delete_device_blocked_while_cabled`); `tests/integration/test_device_delete_guard.py` (`test_cabled_unreserved_device_blocks_delete_until_cables_removed`)
- **INV-DEL-5.** The guard fails closed with 503 `Could not verify device is not in use`
  when reservations or cabling is unreachable or answers non-200, when no internal
  token is configured, or when cabling's body lacks or mistypes `reservation_ids`,
  `connection_count`, or `connection_ids`. \
  Enforced in: `services/inventory/app/services/device_delete_guard.py` (`assert_device_deletable`, `find_cabling_dependents_for_device`, `UNVERIFIABLE_DETAIL`) \
  Pinned by: `services/inventory/tests/test_device_delete_guard.py` (`test_cabling_non_200_is_503`, `test_cabling_unparseable_body_is_503`, `test_missing_internal_token_is_503`, `test_old_cabling_build_without_new_keys_blocks_delete_with_503`); `services/inventory/tests/test_devices.py` (`test_delete_device_blocked_by_reservation_upstream_unreachable_503`)
- **INV-DEL-6.** A 200 from reservations whose body is not a JSON list of objects is
  unverifiable like a non-200 answer: the delete answers the 503 of INV-DEL-5 and the
  device is not deleted. The config-version restore, which asks the same lookup, answers
  503 `reservations service returned an unparseable body while checking active
  reservations`. \
  Enforced in: `services/inventory/app/services/reservation_guard.py` (`find_blocking_reservations_for_device`) \
  Pinned by: `services/inventory/tests/test_device_delete_guard.py` (`test_reservations_unparseable_body_blocks_delete_with_503`); `services/inventory/tests/test_device_config_restore_reservation_guard.py` (`test_find_blocking_reservations_unparseable_body_raises_503`)
- **INV-DEL-7.** With both answers empty the device is deleted (204). There is no force
  flag. \
  Enforced in: `services/inventory/app/routers/devices.py` (`delete_device_by_id`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_delete_device_proceeds_when_no_blocking_reservations`); `tests/integration/test_device_delete_guard.py` (`test_delete_succeeds_for_unreserved_device`)
- **INV-DEL-8.** The guard and the delete are not atomic: a reservation or cable created
  between the check and the commit is not seen. A fork is archived when its reservation
  ends, before execution's asynchronous teardown, so a delete just after a cancel can
  precede the release of the device's wiring (recorded in the
  `device_delete_guard.py` module docstring). \
  Enforced in: `services/inventory/app/routers/devices.py` (`delete_device_by_id`) \
  Pinned by: none
- **INV-DEL-9.** The internal dynamic-instance delete is exempt from this guard by
  decision: its caller is the owning reservation's own teardown (recorded in the
  `delete_dynamic_device_internal` docstring). \
  Enforced in: `services/inventory/app/routers/devices.py` (`delete_dynamic_device_internal`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_delete_204`)

**Out of scope.** Integration tests delete a device's cables before the device through
`delete_device_checked` in `tests/integration/_device_teardown.py`; that is a test
convention, not a service rule.

### 8.14 Dynamic-instance devices

**What it does.** When a booking asks for a virtual instance, execution creates it on a
hypervisor and then asks inventory to record it as a device; when the booking ends,
execution asks inventory to delete that device.

**Surfaces.** Routes `POST /devices/internal` and `DELETE /devices/{id}/internal`. The
instance lifecycle is `dynamic-resources.md`'s.

**Rules.**

- **INV-DYN-1.** The internal create accepts only a template of type `dynamic`; an
  unknown template answers 422 `Template not found` and any other type 422
  `Template is not a dynamic template`. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_create_rejects_non_dynamic_template_422`)
- **INV-DYN-2.** Without a name, the device is named `<template name>-<first 8
  characters of reservation_id>-<n>`, with `n` counting up from 1 past every name
  already taken. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_create_generates_name`, `test_internal_create_name_collision_disambiguates`)
- **INV-DYN-3.** `field_data` is validated against the template with unknown keys
  allowed, so instance attributes the recipe returns are kept; defaults and required
  fields still apply. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`validate_field_data`, `create_dynamic_instance_device`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_create_allows_unknown_field_data`)
- **INV-DYN-4.** A create carrying a `request_id` that already has a device answers 201
  with that same device and creates nothing. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`, `_unique_conflict_target`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_create_same_request_id_returns_same_device`, `test_internal_create_explicit_name_same_request_id_returns_existing`)
- **INV-DYN-5.** Without a `request_id` every create makes a new device; an explicit
  name that is taken answers 409 `Device with name '<name>' already exists`. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_create_omitted_request_id_always_creates`, `test_internal_create_explicit_name_collision_new_request_id_409`)
- **INV-DYN-6.** A new dynamic-instance device joins `No Pool` like any device. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`) \
  Pinned by: none
- **INV-DYN-7.** The internal delete answers 204 for a dynamic-instance device, 404 for
  an unknown id, and 409 `Device is not a dynamic instance` for any other device. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`delete_dynamic_instance_device`); `services/inventory/app/routers/devices.py` (`delete_dynamic_device_internal`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_internal_delete_204`, `test_internal_delete_absent_404`, `test_internal_delete_non_dynamic_409`)
- **INV-DYN-8.** The admin `POST /devices` refuses a dynamic template (INV-DEV-2), so a
  dynamic-instance device exists only through the internal create. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_device`) \
  Pinned by: `services/inventory/tests/test_devices_internal.py` (`test_admin_create_device_rejects_dynamic_template_422`)
- **INV-DYN-9.** The generated-name search stops after 10000 tries and answers 409
  `Could not generate a unique device name for prefix '<prefix>'`. \
  Enforced in: `services/inventory/app/services/inventory_service.py` (`create_dynamic_instance_device`, `_MAX_NAME_ATTEMPTS`) \
  Pinned by: none

**Out of scope.** The execution ledger, retries, and teardown order
(`dynamic-resources.md`).

### 8.15 Answers to other services

**What it does.** Other HERD services read inventory with a shared service token when no
user is acting.

**Surfaces.** Section 7.

**Rules.**

- **INV-INT-1.** Every internal route answers 403 `Invalid internal token` to a wrong
  token (and when the server has none configured) and 422 when the header is missing. \
  Enforced in: `services/inventory/app/routers/devices.py` (`update_device_status_internal`, `get_device_by_id_internal`); `services/common/herd_common/internal_auth.py` (`internal_token_matches`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_internal_status_update_bad_token`, `test_internal_status_update_missing_token`, `test_internal_get_device_bad_token`); `services/common/tests/test_internal_auth.py` (`test_empty_configured_token_refuses_even_with_empty_header`, `test_none_configured_token_refuses_any_provided_value`); `services/inventory/tests/test_hypervisors.py` (`test_by_secret_internal_bad_token_403`); `services/inventory/tests/test_templates.py` (`test_internal_get_template_bad_token`)
- **INV-INT-2.** The internal device read answers the full device for any id, with no
  visibility filter; an unknown id answers 404. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_device_by_id_internal`) \
  Pinned by: `services/inventory/tests/test_devices.py` (`test_internal_get_device`, `test_internal_get_device_not_found`)
- **INV-INT-3.** `POST /internal/devices/batch` answers only `id`, `name`,
  `connection_type` (null without a driver), and `status`, for at most 500 ids,
  collapsing repeats and leaving out unknown ids. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices_batch_internal`, `InternalDeviceBatchEntry`) \
  Pinned by: `services/inventory/tests/test_devices_internal_batch.py` (`test_batch_returns_identity_and_connection_type`, `test_batch_device_with_no_driver_has_null_connection_type`, `test_batch_omits_ids_that_do_not_exist`, `test_batch_deduplicates_repeated_ids`, `test_batch_request_rejects_more_than_cap_ids`)
- **INV-INT-4.** An empty id list answers `[]` without a query. \
  Enforced in: `services/inventory/app/routers/devices.py` (`get_devices_batch_internal`) \
  Pinned by: `services/inventory/tests/test_devices_internal_batch.py` (`test_batch_empty_ids_returns_empty_list_no_query`)
- **INV-INT-5.** `POST /devices/resolve-by-name` maps each exact device name to its id
  and leaves out names with no device; empty names are ignored. \
  Enforced in: `services/inventory/app/routers/devices.py` (`resolve_devices_by_name`) \
  Pinned by: `services/inventory/tests/test_bulk.py` (`test_resolve_by_name_internal`, `test_resolve_by_name_requires_internal_token`); `services/inventory/tests/test_router_edge_cases.py` (`test_resolve_by_name_empty_names_returns_empty`)

**Out of scope.** What each caller does with the answer.

### 8.16 Poll interval

**What it does.** An admin can ask for a device to be health-polled every N seconds,
set on the template for all its devices or on one device; inventory refuses intervals
the poller cannot honor.

**Surfaces.** Template and device create and update; the template editor; route
`GET /devices/health-config`. Polling itself is `operations-and-observability.md`'s.

**Rules.**

- **INV-POLL-1.** `poll_interval_seconds` on a device or a template is null or at least
  `MIN_POLL_INTERVAL_SECONDS` (30); a smaller value answers 422 on create and update. \
  Enforced in: `services/inventory/app/schemas/device.py` (`MIN_POLL_INTERVAL_SECONDS`, `_validate_poll_interval`); `services/inventory/app/schemas/template.py` (`TemplateCreate`, `TemplateUpdate`) \
  Pinned by: `services/inventory/tests/test_poll_interval.py` (`test_poll_interval_below_minimum_rejected_on_device`, `test_poll_interval_below_minimum_rejected_on_template`)
- **INV-POLL-2.** The floor equals execution's default scheduler tick, and a unit test
  fails when the two drift. \
  Enforced in: `services/inventory/app/schemas/device.py` (`MIN_POLL_INTERVAL_SECONDS`) \
  Pinned by: `tests/unit/test_poll_floor_parity.py` (`test_inventory_floor_matches_execution_default_tick`)
- **INV-POLL-3.** A device's resolved interval is its own value, else its template's,
  else none; the health-config route lists only devices with a resolved interval. \
  Enforced in: `services/inventory/app/routers/devices.py` (`list_devices_health_config`, `_device_to_response`) \
  Pinned by: `services/inventory/tests/test_poll_interval.py` (`test_health_config_returns_only_devices_with_resolved_interval`, `test_create_device_without_poll_interval_inherits_from_template`, `test_device_poll_interval_overrides_template`)
- **INV-POLL-4.** The template editor refuses a poll interval below 30 or not a whole
  number before it saves, and treats a blank value as null. \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`handleSave`) \
  Pinned by: `frontend/src/test/pages/TemplateEditorPage.editflow.test.tsx` (`rejects a poll interval below the 30-second floor`, `rejects a non-integer poll interval`, `accepts a blank poll interval as null (no polling)`)

**Out of scope.** Poll tiers and the scheduler (`operations-and-observability.md`).

## 9. Errors

FastAPI validation errors (422) carry `detail` as a list of `{loc, msg, type}`; every
other error carries `detail` as a string or as the object shown.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 401 | `Not authenticated` or `Could not validate credentials` | no bearer token, or one that does not verify | INV-AUTH-1 |
| 401 | `invalid token subject` | a non-admin list or batch with a non-UUID subject | INV-VIS-5, INV-BATCH-5 |
| 403 | `Admin or superadmin role required` | a non-admin on an admin route | INV-AUTH-2, INV-HYP-1 |
| 403 | `Cannot query visible devices for another user` | a non-admin asks for another user's visible devices | INV-VIS-8 |
| 403 | `Invalid internal token` | an internal route with a wrong token, or with none configured | INV-INT-1 |
| 404 | `Device not found` | unknown device on read, update, delete, port create, internal read, status write; a hidden device for a non-admin | INV-DEV-8, INV-DEL-1, INV-PORT-2, INV-PORT-3, INV-INT-2, INV-STATUS-5, INV-VIS-6, INV-DYN-7 |
| 404 | `Device <id> not found` | the by-device group lookup on an unknown or hidden device | INV-GRP-10, INV-GRP-13 |
| 404 | `Port not found` | unknown port, or a port on a hidden device | INV-PORT-2, INV-PORT-7, INV-PORT-8 |
| 404 | `Template not found` | unknown template on read, update, or delete | INV-TPL-1, INV-TPL-15, INV-TPL-19 |
| 404 | `Driver package not found` | unknown driver package | INV-DRV-1, INV-DRV-10, INV-DRV-12, INV-DRV-13 |
| 404 | `Hypervisor not found` | unknown hypervisor | INV-HYP-5, INV-HYP-7, INV-HYP-8 |
| 404 | `Device group not found` | unknown device group on read, update, delete, or a bulk route | INV-GRP-1, INV-GRP-3, INV-GRP-4, INV-GRP-8, INV-GRP-9 |
| 409 | `{"error": "device_in_use", "reservation_ids", "transit_reservation_ids"}` | delete of a device a live reservation depends on | INV-DEL-3 |
| 409 | `{"error": "device_cabled", "connection_count", "connection_ids"}` | delete of a device a connection names | INV-DEL-4 |
| 409 | `{"error": "port_cabled", "connection_count", "connection_ids"}` | delete or rename of a port a connection names | INV-PORT-8, INV-PORT-10 |
| 409 | `Device with name '<name>' already exists` | duplicate device name | INV-DEV-1, INV-DEV-11, INV-DYN-5 |
| 409 | `Device violates a database constraint` | a non-unique integrity error on device update | INV-DEV-11 |
| 409 | `Could not generate a unique device name for prefix '<prefix>'` | the dynamic-instance name search ran out | INV-DYN-9 |
| 409 | `Device is not a dynamic instance` | internal delete of a non-dynamic device | INV-DYN-7 |
| 409 | `Template with name '<name>' already exists` or `Template violates a database constraint` | duplicate template name; another integrity error | INV-TPL-3 |
| 409 | `Cannot delete template: devices still reference it` or `... ports still reference it` | template delete while referenced | INV-TPL-19 |
| 409 | `Driver with name '<name>' already exists` | duplicate driver name on upload or rename | INV-DRV-8, INV-DRV-10 |
| 409 | `Cannot delete driver: templates still reference it` | driver delete while referenced | INV-DRV-13 |
| 409 | `Cannot change connection_type: device templates use this driver, ...` or `Cannot change connection_type: dynamic templates use this driver, ...` | a driver connection-type change that breaks a template using it | INV-DRV-11 |
| 409 | `Hypervisor with name '<name>' already exists` | duplicate hypervisor name | INV-HYP-2 |
| 409 | `Cannot delete hypervisor: templates still reference it` | hypervisor delete while referenced | INV-HYP-7 |
| 409 | `Device group '<name>' already exists` | duplicate group name | INV-GRP-2 |
| 422 | validation list | schema violations: name lengths, unknown enum values, an explicit null on a NOT NULL device field (INV-DEV-11), poll interval below the floor, template field and section rules, batch over 500 ids, group bulk over 500 ids, port bulk bounds, blank hypervisor fields, bad export or import `format`, missing internal token header | INV-DEV-1, INV-DEV-6, INV-POLL-1, INV-TPL-2, INV-TPL-4 to INV-TPL-12, INV-BATCH-1, INV-GRP-2, INV-GRP-4, INV-GRP-9, INV-GRP-17, INV-PORT-4, INV-PORT-5, INV-HYP-11, INV-BULK-19, INV-INT-1, INV-INT-3 |
| 422 | `Devices not found: <ids>` | group bulk add naming a device that does not exist | INV-GRP-7 |
| 422 | `Template not found`, `Template is not a device template`, `Template is not a port template`, `Template is not a dynamic template` | device, port, or dynamic-instance create from the wrong template | INV-DEV-2, INV-PORT-3, INV-DYN-1 |
| 422 | `Template '<name>' has unknown hardware identity. ...` | device create from a template whose vendor or model is `unknown` | INV-DEV-3 |
| 422 | `Unknown fields: <keys>`, `Required field missing: <key>`, `Field '<key>' must be a string`, `... a number`, `... a boolean`, `... one of: <options>` | `field_data` validation | INV-FIELD-1 to INV-FIELD-6 |
| 422 | `Dynamic templates require a Hypervisor-type driver` or `Device templates cannot use a Hypervisor-type driver` | template driver of the wrong connection type, on create or update | INV-TPL-13, INV-TPL-16 |
| 422 | `Device templates must have a driver`, `Dynamic templates must have a driver`, `Dynamic templates must have a hypervisor`, `hypervisor_id is only valid on dynamic templates`, `driver_id is only valid on device or dynamic templates` | a template update whose merged driver or hypervisor breaks the create-time rules | INV-TPL-16 |
| 422 | `Referenced hypervisor or driver does not exist` | template with an unknown driver or hypervisor id | INV-TPL-14 |
| 422 | `Invalid file type: must be one of ...`, `Invalid file name: path separators and traversal segments are not allowed`, `File too large: max <N> bytes`, `Invalid connection_type: must be one of ...` | driver upload, replace, or metadata update | INV-DRV-3, INV-DRV-4, INV-DRV-5, INV-DRV-10 |
| 422 | `Secret does not exist` | hypervisor with an unknown secret | INV-HYP-3, INV-HYP-6 |
| 422 | `Invalid JSON: ...`, `JSON import must be a list of records or an object with an 'items' list`, `'items' must be a list` | unparseable JSON import | INV-BULK-4 |
| 422 | `Import file must be UTF-8 encoded; re-save it as UTF-8 and retry` | an import file that is not valid UTF-8 | INV-BULK-20 |
| 422 | `generated port names would exceed 255 characters; ...` (validation list) | a bulk port create whose last generated name is too long | INV-PORT-6 |
| 500 | `internal: missing Authorization header while resolving user groups` or `... group names` | a visibility or name lookup with no header to forward | INV-VIS-3 |
| 503 | `auth service unreachable while fetching user groups` or `auth service returned <status> when fetching user groups` | visibility lookup failed | INV-VIS-2, INV-VIS-6, INV-VIS-8, INV-BATCH-5 |
| 503 | `auth service unreachable while fetching group names` or `auth service returned <status> when fetching group names` | by-device group name lookup failed | INV-GRP-11 |
| 503 | `Could not verify device is not in use` | the delete guard could not ask reservations or cabling | INV-DEL-5 |
| 503 | `Could not verify port is not cabled` | the port delete or rename guard could not ask cabling | INV-PORT-8, INV-PORT-10 |
| 503 | `secrets service unreachable while validating secret` or `secrets service returned <status> while validating secret` | hypervisor secret check failed | INV-HYP-4, INV-HYP-6 |
| 503 | `fault injection: simulated inventory status-update failure` | the test seam fired | INV-STATUS-6 |

A rejected import row is not an HTTP error: it is a `reject` entry with a `reason` in a
200 report (INV-BULK-7).

## 10. Interactions with other services

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| inventory to auth | auth | `GET /groups/user/{id}` with the caller's JWT | a non-admin's user groups, for visibility | fail closed: 503 (INV-VIS-2) |
| inventory to auth | auth | `GET /groups?skip=&limit=500` with the caller's JWT, paged until every wanted id is named | user group names for the by-device group lookup | fail closed: 503 (INV-GRP-11) |
| inventory to reservations | reservations | `GET /internal/by-device/{id}` (`X-Internal-Token`, 5 s) | delete guard: reservations booking the device | fail closed: 503, a non-JSON 200 included (INV-DEL-5, INV-DEL-6) |
| inventory to cabling | cabling | `GET /internal/forks/by-device/{id}` (`X-Internal-Token`, 5 s) | delete guard: fork wiring and connections naming the device | fail closed: 503 (INV-DEL-5) |
| inventory to cabling | cabling | `GET /connections/internal/by-port?device_id&port_name` (`X-Internal-Token`, 5 s) | port delete and rename guard: connections naming the port | fail closed: 503 (INV-PORT-8) |
| inventory to secrets | secrets | `GET /internal/secrets/{id}/value` (`X-Internal-Token`, 10 s); only the status code is read | hypervisor secret exists | fail closed: 404 is 422, anything else 503 (INV-HYP-3, INV-HYP-4) |
| inventory to storage | local disk or MinIO | put, get, remove object | driver archives | upload or download failure is unhandled (500); a delete failure is ignored (INV-DRV-12, INV-DRV-13) |
| inventory to execution, acl, reservations | | config schema, apply, and fire-time checks | device configuration | specified in `device-configuration.md` |

The calls other services make to inventory are section 7.

## 11. Configuration

| Setting | Default | Effect |
|---|---|---|
| `DRIVER_MAX_SIZE_BYTES` | `10485760` | Largest accepted driver upload (INV-DRV-4) |
| `DRIVER_STORAGE_PATH` | `/data/drivers` | Local storage root, used when `MINIO_ENDPOINT` is empty (INV-DRV-14) |
| `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET` (`herd-drivers`), `MINIO_USE_SSL` (`false`) | empty | MinIO storage when the endpoint is set (INV-DRV-14) |
| `INTERNAL_API_TOKEN` | empty | Shared service token; empty refuses every internal call (INV-INT-1) and makes the delete guard answer 503 (INV-DEL-5) |
| `AUTH_SERVICE_URL`, `RESERVATIONS_SERVICE_URL`, `CABLING_SERVICE_URL`, `SECRETS_SERVICE_URL` | the compose service names | Upstreams of section 10 |
| `HERD_FAULT_INJECTION` | unset | Enables the status-write fault seam (INV-STATUS-6); set only by `docker-compose.override.yml` |
| `APPLY_SCHEDULER_ENABLED`, `APPLY_SCHEDULER_INTERVAL_SECONDS`, `APPLY_JOB_MAX_HORIZON_DAYS`, `EXECUTION_SERVICE_URL`, `ACL_SERVICE_URL` | see [ENV_VARS.md](../ENV_VARS.md) | Device configuration (`device-configuration.md`) |

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/inventory/tests/` (`test_*_unit.py`, `test_template_schema_validators.py`, `test_schema_bounds.py`, `test_storage.py`, `test_storage_constraints.py`, `test_device_delete_guard.py`); `services/common/tests/test_csv_safety.py`; `tests/unit/test_poll_floor_parity.py`; frontend `frontend/src/test/api/deleteDeviceErrorMessage.test.ts` | SQLite in memory; `test_storage_constraints.py` turns on SQLite foreign keys, no other inventory suite does |
| Functional (through the service API) | `services/inventory/tests/test_devices.py`, `test_templates.py`, `test_ports.py`, `test_drivers.py`, `test_hypervisors.py`, `test_device_groups.py`, `test_bulk.py`, `test_devices_internal.py`, `test_device_read_visibility_gate.py`, `test_rbac_denial.py`, and siblings; frontend page tests under `frontend/src/test/pages/` | Upstream services are patched; the delete guard is patched to pass in most suites |
| Integration (running stack) | `tests/integration/test_device_delete_guard.py`, `test_device_group_visibility.py`, `test_device_batch_fetch.py`, `test_bulk_import_export.py`, `test_driver_flow.py`, `test_dynamic_resources.py`, `test_provisioning_failed.py` | Run by the advisory CI `integration` job and the gates |
| Stress and load | `tests/load/locustfile.py` (`InventoryBrowser`, `BulkExporter`) | Read and export load only; no write load on inventory |
| Browser end-to-end | `tests/e2e/test_inventory_filters_playwright.py`, `test_inventory_expanded_playwright.py`, `test_inventory_saved_search_late_load_playwright.py`, `test_inventory_effects_playwright.py`, `test_templates_playwright.py`, `test_dynamic_template_authoring_playwright.py`, `test_device_groups.py`, `test_drivers.py`, `test_bulk_import_export.py`, `test_add_device_ui.py` | Nightly and the gates, not per-PR CI |

Run while writing this document: the inventory backend suite (876 passed), the cited
common suites `test_csv_safety.py` and `test_internal_auth.py`, and `tests/unit/`. Not
run: the frontend suites, and the integration, load, and end-to-end suites (no stack was
started). Behavior marked unpinned was confirmed by reading the code and, for INV-GRP-7,
INV-BULK-20, INV-BULK-18, INV-BULK-12, INV-DEV-11, INV-TPL-16, INV-DRV-11, and
INV-PORT-6, by a throwaway script against the service on SQLite with foreign keys on.

## 13. Known limits and gaps

### Open defects

- #1017 (INV-BULK-18): a dry run skips the create and update service calls, so the
  checks they own (unknown field keys, the template-driver connection-type rule,
  hardware identity) do not run, and the commit can reject a row the dry run accepted.
- #1023 (INV-PORT-8): a port delete has no cabling guard, and neither has a port
  rename (INV-PORT-7 changes the name with no cabling check), so a connection can name
  a port that no longer exists under that name.
- #1024 (INV-BULK-15, INV-BULK-3): a dynamic template cannot be created through import,
  because a template row has no hypervisor column and the export writes none.

### Limits by decision

- Any signed-in user may download a driver package ([ROLES.md](../ROLES.md), Driver
  Management) (INV-DRV-17).
- The batch fetch does not force `dut_only` for a non-admin (the `get_devices_batch`
  docstring) (INV-BATCH-4).
- The internal dynamic-instance delete skips the delete guard (the
  `delete_dynamic_device_internal` docstring) (INV-DEL-9).
- The delete guard has no force flag (the `device_delete_guard.py` module docstring)
  (INV-DEL-7), and neither has the port delete and rename guard (the
  `port_cabling_guard.py` module docstring) (INV-PORT-8).
- The port guard counts plain cabling connections only, not a live fork's wires that
  name the port (INV-PORT-8, TOPO-CONNINT-2), and its check and the write are not
  atomic.
- Unreadable or missing driver metadata reads as no capability (the
  `_parse_driver_metadata` docstring) (INV-DRV-7).
- An admin status write ignores reservation holds (INV-STATUS-9). The manual's
  troubleshooting page ([troubleshooting.html](../manual/troubleshooting.html)) says
  `OFFLINE` or `MAINTENANCE` means an admin took the device down on purpose, and
  reservations' own conflict check reads its own database, not inventory's status.
- The delete guard's check and the delete are not atomic (INV-DEL-8). The window after
  a cancel is recorded in the `device_delete_guard.py` module docstring, and the same
  check-to-commit window is an accepted known limit of the topology delete guard (issue
  #977, [topology.md](topology.md)).

Documentation that disagrees with the code: [ROLES.md](../ROLES.md) and
[ENV_VARS.md](../ENV_VARS.md) each make an inventory statement the code contradicts
(the driver connection types and template types, and whether the MinIO bucket must
exist); #1025 tracks the corrections.

### Rules with no test

- INV-STATUS-7: status writes have no compare-and-swap.
- INV-STATUS-8: create and import create accept any status.
- INV-STATUS-9: an admin status write ignores reservation holds.
- INV-TPL-6: a key repeated across sections.
- INV-TPL-24: a template's type cannot change.
- INV-RED-4: redaction covers only the template's password keys.
- INV-BATCH-4: the batch does not force `dut_only`.
- INV-PORT-9: repeated port names on one device.
- INV-GRP-15: a device left in no group is not returned to `No Pool`.
- INV-GRP-16: renaming or deleting `No Pool`.
- INV-GRP-17: the group name length.
- INV-DRV-9: the storage key, the store-before-commit order, and the cleanup on a
  duplicate name.
- INV-DRV-17: a user-role download and the download content type.
- INV-DRV-18: capability flags and uploader on a file replacement.
- INV-HYP-6: a changed secret is re-validated (the test of that name does not assert
  the call).
- INV-HYP-11: blank hypervisor fields.
- INV-BULK-3: export content (clear passwords, no hypervisor).
- INV-BULK-15: a dynamic template row cannot be imported.
- INV-BULK-18: a dry run skips the create and update checks.
- INV-DEL-8: the guard and the delete are not atomic.
- INV-DYN-6: dynamic-instance devices join `No Pool`.
- INV-DYN-9: the generated-name attempt cap.
