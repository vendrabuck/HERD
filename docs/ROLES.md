# HERD: Roles and Permissions

HERD uses three roles. Every authenticated user holds exactly one role, which is encoded
in their JWT access token and enforced independently by each service.

---

## Roles at a Glance

| Capability | User | Admin | Superadmin |
|---|:---:|:---:|:---:|
| Register and log in | yes | yes | yes |
| Browse device inventory | yes | yes | yes |
| Browse templates and drivers | yes | yes | yes |
| Build topology diagrams | yes | yes | yes |
| Create and cancel reservations | yes | yes | yes |
| List every user's reservations (`GET /reservations/?all=true`) | | yes | yes |
| Cancel any user's reservation | | yes | yes |
| View reservation calendar | yes | yes | yes |
| View backend connections | yes | yes | yes |
| Create, update, delete templates | | yes | yes |
| Upload, update, delete drivers | | yes | yes |
| Draft recipes with AI (gated by AI_RECIPE_AUTHORING_ENABLED) | | yes | yes |
| Register, update, delete hypervisors | | yes | yes |
| Add devices to inventory | | yes | yes |
| Update devices in inventory | | yes | yes |
| Remove devices from inventory | | yes | yes |
| Create, update, delete ports | | yes | yes |
| Create backend connections (cabling) | | yes | yes |
| Delete backend connections | | yes | yes |
| Create topologies | yes | yes | yes |
| Update, delete topologies (creator or admin) | creator | yes | yes |
| Bulk export and import devices and templates | | yes | yes |
| Bulk export and import topologies | yes (import updates: creator only) | yes | yes |
| View user groups and members | yes | yes | yes |
| Create, update, delete groups | | yes | yes |
| Add and remove group members | | yes | yes |
| Bulk add and remove group members | | yes | yes |
| Check ACL permissions | yes | yes | yes |
| Create, view, delete ACL grants | | yes | yes |
| Create, update, delete device groups | | yes | yes |
| Manage device group devices and permissions | | yes | yes |
| View execution runs (unscoped) | | yes | yes |
| View execution runs for an owned reservation | yes | yes | yes |
| Execute drivers and retry failed runs | | yes | yes |
| View device health snapshot (single device) | yes | yes | yes |
| List device health snapshots (all devices) | | yes | yes |
| Set `poll_interval_seconds` on a device or template | | yes | yes |
| List all user accounts | | yes | yes |
| Promote a user to admin | | | yes |
| Demote an admin to user | | | yes |

---

## Role Descriptions

### User (default)

All accounts created through `POST /api/auth/register` receive the `user` role automatically.

A user can:
- Browse the device inventory with optional filters (type, topology, availability status)
- Drag devices onto the topology canvas and build L1/L2/L3 connection diagrams
- Create reservations for one or more devices over a chosen time window
- Cancel or early-release their own reservations
- View existing backend cabling connections (read-only)
- Check ACL permissions on resources (own permissions)
- List resources accessible to them via ACL grants

A user cannot:
- Add, modify, or remove devices from the inventory
- Create or remove backend cabling connections
- Create, update, or delete user groups
- Add or remove group members
- Create, view, or delete ACL grants
- View or manage other users' accounts

### Admin

The `admin` role is granted by the superadmin. An admin has all user capabilities plus:

- Add new devices to the inventory (`POST /api/inventory/devices`)
- Update device details or status (`PUT /api/inventory/devices/{id}`)
- Remove devices from the inventory (`DELETE /api/inventory/devices/{id}`)
- Bulk export and import devices and templates (`GET /api/inventory/devices/export`, `POST /api/inventory/devices/import`, `GET /api/inventory/templates/export`, `POST /api/inventory/templates/import`)
- Create backend connections between device ports (`POST /api/cabling/connections`)
- Remove backend connections (`DELETE /api/cabling/connections/{id}`)
- Create, update, and delete user groups (`POST/PUT/DELETE /api/auth/groups`)
- Add and remove group members (`POST/DELETE /api/auth/groups/{id}/members`)
- Bulk add and remove group members (`POST /api/auth/groups/{id}/members/bulk`, `POST /api/auth/groups/{id}/members/bulk-remove`)
- Create, view, and delete ACL grants (`POST/GET/DELETE /api/acl/grants`)
- Create, update, and delete device groups (`POST/PUT/DELETE /api/inventory/device-groups`)
- Manage device group devices and permissions (bulk add/remove endpoints)
- View execution runs (`GET /api/execution/runs`, `GET /api/execution/runs/{id}`)
- Manually execute driver actions (`POST /api/execution/execute`)
- Retry failed execution runs (`POST /api/execution/runs/{id}/retry`)
- List all registered user accounts (`GET /api/auth/users`)

Backend connections represent physical cables or virtual links between devices.
These are created by admins after a topology has been agreed and are not something
end-users configure themselves.

### Superadmin

There is exactly one superadmin account per deployment. It is created automatically
on first startup from environment variables (see Setup below) and cannot be created
or removed through the API.

The superadmin has all admin capabilities plus:

- Set any user's role to `user` or `admin` (`PUT /api/auth/users/{id}/role`)

The superadmin's own role cannot be changed via the API. Demoting the superadmin
requires direct database access, which is intentional.

---

## Setup: Creating the Superadmin

Set the following three environment variables before starting the stack for the
first time. All three must be non-empty for the account to be created.

```
SUPERADMIN_EMAIL=superadmin@example.com
SUPERADMIN_USERNAME=superadmin
SUPERADMIN_PASSWORD=a-strong-password
```

These are read by the auth service on startup. If a superadmin already exists,
the seed step is skipped; subsequent restarts are safe. The password is stored
as a bcrypt hash; the plaintext value in the environment file is only needed
for the initial creation.

On first run:

```bash
cp .env.example .env
# Edit .env and set SUPERADMIN_EMAIL, SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD
make up
```

After the stack is running, log in as the superadmin through the normal login endpoint:

```
POST /api/auth/login
{ "email": "superadmin@example.com", "password": "a-strong-password" }
```

---

## Admin Management (Superadmin Operations)

Role management endpoints require a superadmin JWT. Listing users requires
admin or superadmin.

### List all users

```
GET /api/auth/users
Authorization: Bearer <admin-token>
```

Returns an array of all user accounts with their current role.
Available to admin and superadmin roles.

### Promote a user to admin

```
PUT /api/auth/users/{user_id}/role
Authorization: Bearer <superadmin-token>
Content-Type: application/json

{ "role": "admin" }
```

### Demote an admin back to user

```
PUT /api/auth/users/{user_id}/role
Authorization: Bearer <superadmin-token>
Content-Type: application/json

{ "role": "user" }
```

Rules enforced by the API:
- The `superadmin` role value cannot be set via the API.
- The superadmin cannot change their own role.
- The superadmin's role cannot be changed by anyone through the API.
- The superadmin cannot be deactivated by anyone through the API
  (`POST /users/{id}/deactivate` answers 400); `activate` has no such
  carve-out, so an inactive superadmin row is always recoverable in one call.

---

## Group Management (Admin Operations)

Groups allow organizing users into teams. Any authenticated user can view groups
and their members. Creating, updating, deleting groups and managing membership
requires admin or superadmin role.

### List all groups

```
GET /api/auth/groups
Authorization: Bearer <any-authenticated-token>
```

Returns an array of all groups with id, name, description, created_by, created_at.

### Get group details (with members)

```
GET /api/auth/groups/{group_id}
Authorization: Bearer <any-authenticated-token>
```

Returns group info plus a `members` array with user_id, username, email, added_at.

### Create a group

```
POST /api/auth/groups
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "Lab Team Alpha",
  "description": "Primary lab testing team"
}
```

Returns HTTP 201. Group names must be unique (409 on duplicate).

### Update a group

```
PUT /api/auth/groups/{group_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "name": "Renamed Team", "description": "Updated description" }
```

Both fields are optional; omitted fields are unchanged.

### Delete a group

```
DELETE /api/auth/groups/{group_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204. All memberships are removed via cascade.

### Add a member

```
POST /api/auth/groups/{group_id}/members
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "user_id": "uuid-of-user" }
```

Returns HTTP 201. Returns 404 if user or group not found, 409 if already a member.

### Remove a member

```
DELETE /api/auth/groups/{group_id}/members/{user_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204. Returns 404 if membership does not exist.

### Bulk add members

```
POST /api/auth/groups/{group_id}/members/bulk
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "user_ids": ["uuid-1", "uuid-2", "uuid-3"] }
```

Returns `{"added": 2, "skipped": 1}`. Skipped members are already in the group.

### Bulk remove members

```
POST /api/auth/groups/{group_id}/members/bulk-remove
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "user_ids": ["uuid-1", "uuid-2"] }
```

Returns `{"removed": 1, "not_found": 1}`.

### Default "Not Grouped" group

A group named "Not Grouped" is automatically created on auth service startup.
All new users are auto-assigned to this group on registration.

---

## ACL Grant Management

The ACL service provides resource-level access control via group-based grants. Grants
tie a user group to a specific resource (device, topology, reservation, or secret) with a
permission level ("view" or "manage"). "manage" implies "view": a check for "view"
permission also succeeds when the group has "manage".

### List grants

```
GET /api/acl/grants
Authorization: Bearer <admin-token>
```

Supports query filters: `group_id`, `resource_type`, `resource_id`.

### Create a grant

```
POST /api/acl/grants
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "group_id": "uuid-of-group",
  "resource_type": "device",
  "resource_id": "uuid-of-device",
  "permission": "manage"
}
```

Returns HTTP 201. Returns 409 if the exact grant already exists.
`resource_type` values: `device`, `topology`, `reservation`, `secret`.
`permission` values: `view`, `manage`.

### Secrets service permissions

The secrets service (`/api/secrets`) gates on the `secret` resource type:

| Endpoint | user (no grant) | user + `view` | user + `manage` | admin |
|---|---|---|---|---|
| `GET /api/secrets/secrets` | empty list | listed | listed | all |
| `GET /api/secrets/secrets/{id}` | 404 | metadata | metadata | metadata |
| `GET /api/secrets/secrets/{id}/value` | 404 | 403 | plaintext | plaintext |
| create, update, delete, `POST /keys/rotate` | 403 | 403 | 403 | allowed |

A caller with no grant gets 404 (existence is not confirmed); a `view` caller
requesting plaintext gets 403. `GET /api/secrets/internal/secrets/{id}/value`
is service-to-service only (`X-Internal-Token`; 403 on a missing or wrong
token).

### Delete a grant

```
DELETE /api/acl/grants/{grant_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204.

### Check permission (authenticated user)

```
POST /api/acl/check
Authorization: Bearer <any-authenticated-token>
Content-Type: application/json

{
  "user_id": "uuid-of-user",
  "resource_type": "device",
  "resource_id": "uuid-of-device",
  "permission": "view"
}
```

Resolves the user's group memberships via the auth service, then checks for a
matching grant. Returns `{"allowed": true}` or `{"allowed": false}`.

These endpoints are self-service: a non-admin caller may only query its own
`user_id`. A request whose `user_id` does not match the caller's token returns
403. Admins and superadmins may introspect any user's permissions. The same rule
applies to `/api/acl/check/batch` and `/api/acl/resources`.

### Batch check permissions

```
POST /api/acl/check/batch
Authorization: Bearer <any-authenticated-token>
Content-Type: application/json

{
  "user_id": "uuid-of-user",
  "resource_type": "device",
  "resource_ids": ["uuid-1", "uuid-2"],
  "permission": "view"
}
```

Returns a map of resource_id to boolean.

### List accessible resources

```
GET /api/acl/resources?user_id=<uuid>&resource_type=device&permission=view
Authorization: Bearer <any-authenticated-token>
```

Returns resource IDs the user can access with the given permission level.

### Check a permission (internal)

```
POST /api/acl/internal/check
X-Internal-Token: <internal-api-token>
Content-Type: application/json

{
  "user_id": "uuid-of-user",
  "resource_type": "device",
  "resource_id": "uuid-of-device",
  "permission": "manage"
}
```

Same evaluation and response shape as `POST /check`, for a caller with no user JWT to
forward (issue #704): inventory's apply-job scheduler re-checks a job creator's
authority at fire time with only the creator's user_id. Group membership is resolved
through auth's `GET /internal/users/{user_id}/groups` (below) instead of the
forwarded-JWT `GET /groups/user/{id}`. Closed by default: a transport failure or
non-200 from auth yields `allowed: false`. Internal-token only.

### Reservation-owner widening for device-config writes

Inventory's config-version and apply-job write endpoints (`POST /devices/{id}/config-versions`,
`POST /devices/{id}/config-versions/{vid}/restore`,
`POST /devices/{id}/config-versions/{vid}/apply`,
`POST /devices/{id}/config-versions/{vid}/schedule`, `POST /apply-jobs/{id}/confirm`)
require `manage` on the target device. As of iter 3, this check is widened: a caller
who owns a currently-active reservation that includes the device also passes. The
inventory service makes a second hop to the reservations service at
`GET /api/reservations/internal/active?user_id=…&device_id=…` to answer the ownership
question.

This matches the iter-2 precedent on `GET /api/execution/runs`, where reservation
ownership opened a path previously gated to admins. The trade-off is explicit:
during a user's own active reservation window, that user is treated as effectively
administering their reserved devices. Admins who want to keep AI-driven writes
off-limits for specific devices can revoke device visibility from the topology so
the device cannot be reserved in the first place.

Read paths (list config versions, get version detail, diff) are not gated by this
manage-or-reservation-ownership widening. As of issue #718, they instead carry the
same plain group-visibility gate as `GET /devices/{id}` and `GET /devices/{id}/ports`:
a non-admin caller outside the target device's groups gets 404, identical status and
detail to the device read, so a config-version read cannot be used to distinguish a
hidden device from one that does not exist. Admins are unfiltered. This is a narrower
boundary than the write widening above by design: a user can only book (and therefore
usefully read the configuration of) a device they can already see, so reservation
ownership adds nothing a read needs beyond visibility.

A scheduled (`POST .../schedule`) apply job's authorization is not evaluated once and
forgotten: issue #704 re-checks the creator's authority at fire time, using the same
two grounds (explicit `manage` grant, or reservation-owner of an active reservation
containing the device) via ACL's and reservations' internal-token routes, since the
scheduler has no user JWT to re-forward. If the creator's authority has lapsed by fire
time (grant revoked, reservation ended), the job resolves `skipped` with the error
`creator no longer authorized for this device` rather than firing. This closes the gap
where a job scheduled far in advance could still fire once its creator no longer
qualifies. `scheduled_for` is also bounded (`apply_job_max_horizon_days`, default 30
days) so a job cannot sit queued indefinitely before this re-check ever runs, and a
caller-supplied `reservation_id` on the schedule request is validated up front (must
be an active reservation the caller owns that contains the device; 422 otherwise).

---

## Device Group Management (Admin Operations)

Device groups control which devices non-admin users can see and reserve. Each device
group contains a set of devices and a set of user group permissions. A user can see
a device if any of their user groups has permission on a device group containing that device.
Admins see all devices regardless of group assignments.

### List device groups

```
GET /api/inventory/device-groups
Authorization: Bearer <admin-token>
```

Returns an array of device groups with id, name, description, device_count, user_group_count.

### Get device group detail

```
GET /api/inventory/device-groups/{group_id}
Authorization: Bearer <admin-token>
```

Returns group info plus `devices` array and `user_groups` array.

### Create a device group

```
POST /api/inventory/device-groups
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "name": "Lab Pool A", "description": "Primary lab devices" }
```

Returns HTTP 201. Group names must be unique (409 on duplicate).

### Update a device group

```
PUT /api/inventory/device-groups/{group_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "name": "Renamed Pool", "description": "Updated description" }
```

### Delete a device group

```
DELETE /api/inventory/device-groups/{group_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204. Device and permission associations are removed via cascade.

### Bulk add devices

```
POST /api/inventory/device-groups/{group_id}/devices/bulk
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "device_ids": ["uuid-1", "uuid-2"] }
```

Returns `{"added": 2, "skipped": 0}`. Skipped devices are already in the group.

### Bulk remove devices

```
POST /api/inventory/device-groups/{group_id}/devices/bulk-remove
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "device_ids": ["uuid-1", "uuid-2"] }
```

Returns `{"removed": 1, "not_found": 1}`.

### Bulk add user group permissions

```
POST /api/inventory/device-groups/{group_id}/permissions/bulk
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "user_group_ids": ["uuid-1", "uuid-2"] }
```

Returns `{"added": 2, "skipped": 0}`.

### Bulk remove user group permissions

```
POST /api/inventory/device-groups/{group_id}/permissions/bulk-remove
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "user_group_ids": ["uuid-1"] }
```

Returns `{"removed": 1, "not_found": 0}`.

### Get visible devices for a user

```
GET /api/inventory/device-groups/visible-devices?user_id=<uuid>
Authorization: Bearer <any-authenticated-token>
```

Resolves the user's group memberships via the auth service, then returns all device
IDs accessible through device group permissions.

This endpoint is self-service: a non-admin caller may only query its own `user_id`.
A request whose `user_id` does not match the caller's token returns 403. Admins and
superadmins may introspect any user's visible devices. This is the same rule the
acl permission-query endpoints enforce.

### Default "No Pool" group

A device group named "No Pool" is automatically created on inventory service startup.
All new devices are auto-assigned to this group on creation.

---

## Template Management (Admin Operations)

Templates define the schema for devices and ports. Any authenticated user can list
and view templates. Creating, updating, and deleting templates requires admin or
superadmin role.

### List templates

```
GET /api/inventory/templates
Authorization: Bearer <any-authenticated-token>
```

Supports query param `template_type` to filter by "device" or "port".

### Get a template

```
GET /api/inventory/templates/{template_id}
Authorization: Bearer <any-authenticated-token>
```

### Create a template

```
POST /api/inventory/templates
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "EX2300",
  "template_type": "device",
  "driver_id": "uuid-of-driver",
  "vendor": "Juniper Networks",
  "model": "EX2300",
  "part_number": "EX2300-48T",
  "icon": "firewall",
  "exclusive": true,
  "sections": [
    {
      "name": "Management",
      "fields": [
        { "key": "ip", "label": "IP Address", "type": "string", "required": true },
        { "key": "protocol", "label": "Protocol", "type": "dropdown", "options": ["SSH", "HTTPS"], "default": "HTTPS" }
      ]
    }
  ]
}
```

Returns HTTP 201. Template names must be unique (409 on duplicate).
Device templates require a `driver_id`; port templates must not have one.

### Update a template

```
PUT /api/inventory/templates/{template_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "name": "EX2300 v2", "sections": [...] }
```

### Delete a template

```
DELETE /api/inventory/templates/{template_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204. Blocked if devices or ports reference the template (409).

---

## Driver Management (Admin Operations)

Driver packages are standalone entities that device templates reference via `driver_id`.
Any authenticated user can list, view, and download drivers. Uploading, updating,
and deleting drivers requires admin or superadmin role.

### List drivers

```
GET /api/inventory/drivers
Authorization: Bearer <any-authenticated-token>
```

### Get a driver

```
GET /api/inventory/drivers/{driver_id}
Authorization: Bearer <any-authenticated-token>
```

### Download a driver file

```
GET /api/inventory/drivers/{driver_id}/download
Authorization: Bearer <any-authenticated-token>
```

Returns the driver file as a binary download.

### Upload a new driver

```
POST /api/inventory/drivers
Authorization: Bearer <admin-token>
Content-Type: multipart/form-data

name: "Junos Management"
description: "Management driver for Juniper EX-series switches"
connection_type: "Management"
file: <upload .zip or .tar.gz, max 10 MB>
```

Returns HTTP 201. Driver names must be unique (409 on duplicate).
`connection_type` values: "Management", "Layer 1 Switch", "Layer 2 Switch", "Layer 3 Switch".

### Update driver metadata

```
PUT /api/inventory/drivers/{driver_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "name": "Renamed Driver", "description": "Updated", "connection_type": "Management" }
```

### Replace driver file

```
PUT /api/inventory/drivers/{driver_id}/file
Authorization: Bearer <admin-token>
Content-Type: multipart/form-data

file: <upload .zip or .tar.gz, max 10 MB>
```

### Delete a driver

```
DELETE /api/inventory/drivers/{driver_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204. Blocked if templates reference the driver (409).

---

## Hypervisor Management (Admin Operations, ADR 0004)

Hypervisors back the dynamic-resources feature (issue #32): a `dynamic` template pairs
a registered hypervisor with a recipe driver package to materialize an instance per
reservation. Registering, updating, and deleting hypervisors requires admin or
superadmin role; there is no user-visible read surface (unlike templates and drivers,
which any authenticated user may list).

### List hypervisors

```
GET /api/inventory/hypervisors
Authorization: Bearer <admin-token>
```

### Get a hypervisor

```
GET /api/inventory/hypervisors/{hypervisor_id}
Authorization: Bearer <admin-token>
```

### Register a hypervisor

```
POST /api/inventory/hypervisors
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "proxmox-lab-1",
  "description": "Primary Proxmox cluster",
  "endpoint": "https://proxmox.lab.internal:8006",
  "hypervisor_type": "proxmox",
  "secret_id": "uuid-of-secret",
  "enabled": true
}
```

Returns HTTP 201. Hypervisor names must be unique (409 on duplicate). `secret_id` must
reference an existing secret in the secrets service (422 if it does not; 503 if the
secrets service is unreachable); the credential itself is never accepted inline.
`hypervisor_type` is a free string in v1 (e.g. `proxmox`, `vsphere`, `libvirt`).

### Update a hypervisor

```
PUT /api/inventory/hypervisors/{hypervisor_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "endpoint": "https://proxmox2.lab.internal:8006" }
```

Any combination of fields can be updated; omitted fields are unchanged. `secret_id` is
re-validated against the secrets service only when the update actually changes it.

### Delete a hypervisor

```
DELETE /api/inventory/hypervisors/{hypervisor_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204. Blocked (409) while any template still references the hypervisor.

### Get a hypervisor (internal)

```
GET /api/inventory/hypervisors/{hypervisor_id}/internal
X-Internal-Token: <internal-api-token>
```

Service-to-service read used by the execution service's dynamic-resources create and
teardown flows to resolve a hypervisor's endpoint, type, and secret reference.
Internal-token only.

### List hypervisors referencing a secret (internal)

```
GET /api/inventory/hypervisors/by-secret/{secret_id}/internal
X-Internal-Token: <internal-api-token>
```

Reverse lookup returning the id and name of every hypervisor that references the
given secret. Used by the secrets service's delete guard (issue #456), which refuses
with 409 `{"error": "secret_in_use", ...}` while any hypervisor still points at the
secret, and fails closed with 503 when inventory is unreachable. Internal-token only.

---

## Port Management (Admin Operations)

Ports are children of devices, typed by a port template. Any authenticated user can
view ports. Creating, updating, and deleting ports requires admin or superadmin role.

### List ports for a device

```
GET /api/inventory/devices/{device_id}/ports
Authorization: Bearer <any-authenticated-token>
```

### Create a port

```
POST /api/inventory/devices/{device_id}/ports
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "eth0",
  "template_id": "uuid-of-port-template",
  "field_data": { "speed": "1Gbps" }
}
```

### Bulk create ports

```
POST /api/inventory/devices/{device_id}/ports/bulk
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "ports": [
    { "name": "eth0", "template_id": "uuid", "field_data": {} },
    { "name": "eth1", "template_id": "uuid", "field_data": {} }
  ]
}
```

### Get, update, delete a port

```
GET /api/inventory/ports/{port_id}
PUT /api/inventory/ports/{port_id}
DELETE /api/inventory/ports/{port_id}
Authorization: Bearer <admin-token> (PUT, DELETE) or <any-authenticated-token> (GET)
```

---

## Topology Persistence

Topology canvases (node positions, edges, metadata) are stored in the cabling service.
Any authenticated user can view and create saved topologies. Updating or deleting a
topology requires being its creator, or an admin or superadmin.

### List topologies

```
GET /api/cabling/topologies
Authorization: Bearer <any-authenticated-token>
```

### Get a topology

```
GET /api/cabling/topologies/{topology_id}
Authorization: Bearer <any-authenticated-token>
```

### Create a topology

```
POST /api/cabling/topologies
Authorization: Bearer <token>   # any authenticated user may create
Content-Type: application/json

{ "name": "Lab Topology A", "data": { "nodes": [...], "edges": [...] } }
```

### Update a topology

```
PUT /api/cabling/topologies/{topology_id}
Authorization: Bearer <token>   # creator or admin
Content-Type: application/json

{ "name": "Updated Topology", "data": { "nodes": [...], "edges": [...] } }
```

### Delete a topology

```
DELETE /api/cabling/topologies/{topology_id}
Authorization: Bearer <token>   # creator or admin
```

---

## Reservation Topology Fork (ADR 0006 P3a)

Each reservation gets an editable fork of its parent topology on activation, so
live edits stay off the shared master template. The reservations service exposes
three user-facing fork endpoints (they forward to the cabling service's internal
fork surface). All three are owner-or-admin gated: the reservation owner, an
admin, or a superadmin.

Read the fork (allowed for any reservation status, so the as-built record stays
readable after the reservation ends; on an ACTIVE reservation with no fork yet it
lazy-creates one):

```
GET /api/reservations/{reservation_id}/fork
Authorization: Bearer <token>   # owner or admin
```

Loosely edit the fork canvas (stored as a draft with no reconcile) and commit a
reconcile that appends a fork version. Both mutations require the reservation to
be `ACTIVE`; a fork is editable only while its reservation is live, and either
returns `409` otherwise. A save whose wiring would claim a port already held by
another active reservation returns `409` naming the blocking reservation. Since
the 2026-09-04 fork endpoint-membership fix, a save (or the activation snapshot)
naming a canvas endpoint device outside the reservation's own device set also
returns `409` (`fork_device_not_member`, naming the offending device ids);
admins are not exempt, and PATCH-add is the way to bring a device into the
reservation first. Issue #701 phase 2 pushes the same check earlier: creating
a reservation with a `topology_id` whose canvas names a device outside
`device_ids` is refused at `422` (`topology_device_not_member`, naming the
offending device ids) before any row is written, and a PATCH that edits
`device_ids` on a topology-bound reservation re-runs the same check (`400`,
same detail shape), so the mismatch never has to wait for the first fork
write to surface.

```
PUT  /api/reservations/{reservation_id}/fork/canvas    # owner or admin, ACTIVE only
POST /api/reservations/{reservation_id}/fork/save       # owner or admin, ACTIVE only
Authorization: Bearer <token>
```

When the reservation ends the fork is archived to an immutable as-built record:
the read endpoint still returns it, but the two mutations are refused.

Read the reservation's per-connection wiring status, and reattempt its
hardware-retryable FAILED rows. After a fork save reconciles the intended
wiring, the execution service applies each row connection-by-connection and
records the applied state; these owner-or-admin gated endpoints proxy that
per-connection state (ADR 0007). Since ADR 0009 phase 8 the relayed rows span
all three layers, each tagged `layer`: L1 cross-connects, L2 VLAN memberships,
and L3 route pins, and the retry outcomes are tagged the same way. The status
read is allowed for any reservation status, so the applied and FAILED rows stay
readable after the reservation ends. Retry is permitted for `ACTIVE` and the
terminal statuses `COMPLETED`/`CANCELLED`/`FAILED`, and returns `409` only for
`PENDING`/`PENDING_PROVISION` (there is no provisioned wiring to reattempt yet).
On an ended reservation the freeze is direction-scoped (ADR 0009 phase 3): a
build is still refused (execution reports it `frozen` or relays its own `409`),
while a stuck release-direction disconnect may finish:

```
GET  /api/reservations/{reservation_id}/wiring-status   # owner or admin, any status
POST /api/reservations/{reservation_id}/wiring/retry    # owner or admin; not PENDING/PENDING_PROVISION
Authorization: Bearer <token>
```

---

## Reservation Calendar

The calendar provides a cross-user view of reservations within a time range.
Available to all authenticated users.

```
GET /api/reservations/calendar?range_start=2026-01-01T00:00:00&range_end=2026-02-01T00:00:00
Authorization: Bearer <any-authenticated-token>
```

Supports query params: `range_start` (required), `range_end` (required),
`status` (list, optional), `device_id` (optional). Non-admin users see only
reservations for devices visible through their device group permissions.

---

## Provision-Result Callback (Internal, ADR 0004)

```
POST /api/reservations/internal/{reservation_id}/provision-result
X-Internal-Token: <internal-api-token>
Content-Type: application/json

{ "succeeded": true, "device_ids": ["uuid-of-materialized-device"], "error": null }
```

Reported by the execution service once it has resolved every dynamic instance a
reservation booked. Guarded by `X-Internal-Token`; idempotent per reservation, so a
duplicate or late callback (one arriving after the timeout backstop already failed the
reservation, or a user cancelled it) is a no-op that returns 200 with `applied: false`
rather than resurrecting or re-transitioning the row. On success, the materialized
device ids are attached to the reservation and it activates (staging
`reservation.created` so physical L1/L2/L3 provisioning and the topology fork proceed);
on failure it lands `FAILED` and stages `reservation.failed`, which drives the
execution-side dynamic-instance teardown. See [ARCHITECTURE.md](ARCHITECTURE.md#dynamic-resources-hypervisor-backed-templates-adr-0004-issue-32).

---

## Execution Service (Admin / Internal Operations)

The execution service runs driver code on infrastructure devices. Admins can view
execution runs, manually trigger driver actions, and retry failed runs. The device
check endpoint is for internal service-to-service use only.

### List execution runs

```
GET /api/execution/runs
Authorization: Bearer <token>
```

Supports query params: `device_id`, `reservation_id`, `status`, `skip`, `limit`.

Auth: admin or superadmin can list with any combination of filters (including
the unscoped form with no `reservation_id`). Non-admin callers must supply a
`reservation_id` they own; the execution service verifies ownership via a
cross-service `GET` to the reservations service with the caller's JWT. A
non-admin request without `reservation_id`, or with a `reservation_id` the
caller does not own, returns 403. This is used by the AI reservation
assistant's `list_executions_for_reservation` tool so reservation owners can
inspect their own apply history without being granted admin.

### Get execution run detail

```
GET /api/execution/runs/{run_id}
Authorization: Bearer <admin-token>
```

### Manually execute a driver action

```
POST /api/execution/execute
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "device_id": "uuid-of-device",
  "action": "status",
  "user_id": "uuid-of-user",
  "reservation_id": null,
  "port_a": null,
  "port_b": null
}
```

Returns HTTP 201 with the execution run result.

Admins may execute any action. A non-admin may call this endpoint only when the action is `configure` and the caller holds an ACL `manage` grant on the target device; any other non-admin call is rejected 403 (`Admin access required`, or `Admin access or device manage grant required` when the grant is missing).

### Retry a failed run

```
POST /api/execution/runs/{run_id}/retry
Authorization: Bearer <admin-token>
```

Only runs with status FAILED or TIMEOUT can be retried.

### Trigger device status check (internal)

```
POST /api/execution/device-check
X-Internal-Token: <internal-api-token>
Content-Type: application/json

{
  "device_id": "uuid-of-device",
  "user_id": "uuid-of-requesting-user"
}
```

Executes login, status, logout sequence on the device. Used by other services
for automated health checks.

### Get device health snapshot

```
GET /api/execution/device-health/{device_id}
Authorization: Bearer <token>
```

Returns the periodic-poll snapshot for a device: `last_polled_at`, `last_status`
(UNKNOWN, HEALTHY, DEGRADED, UNREACHABLE), `last_run_id`, `consecutive_failures`,
`next_poll_at`. Available to any authenticated user.

On miss (device has not been polled yet), returns a synthesized 200 with
`last_status=UNKNOWN` and all other fields null. This is deliberate: the frontend
renders a health badge on every device-detail page and per-device 404 handling
would be noisy.

### List device health snapshots

```
GET /api/execution/device-health
Authorization: Bearer <admin-token>
```

Supports query params: `skip`, `limit`, optional `last_status` filter. Admin or
superadmin only.

### Health-poll registry (internal)

```
GET /api/inventory/devices/health-config
X-Internal-Token: <internal-api-token>
```

Returns the resolved `poll_interval_seconds` per device for the execution
service's health-poll scheduler to refresh from. Internal-token only.

### Latest device config version (internal)

```
GET /api/inventory/devices/{device_id}/config-versions/latest/internal
X-Internal-Token: <internal-api-token>
```

Returns the device's newest config version (highest `version_number`) with its
full config payload. Used by the execution service's NATS consumer to read the
`routes` array of a Layer 3 switch at reservation provisioning time; the
consumer has no acting user, so it cannot use the JWT-gated config-version
endpoints. Returns 404 when the device is unknown or has no config versions.
Internal-token only.

### List admin user-ids (internal)

```
GET /api/auth/internal/admins
X-Internal-Token: <internal-api-token>
```

Returns user-ids for users with role admin or superadmin (active accounts only).
Used by the notifications service to fan out `device.health_transition` events
to all admins. Internal-token only.

### Get a user's groups (internal)

```
GET /api/auth/internal/users/{user_id}/groups
X-Internal-Token: <internal-api-token>
```

Same response shape as `GET /groups/user/{id}`, for a caller with no user JWT to
forward. Used by ACL's `POST /internal/check` (issue #704) to resolve group
membership for inventory's apply-job fire-time authority re-check. Scoped to active
users only, unlike `/internal/users/{id}/contact` above: 404 for an unknown OR a
deactivated user, since an authority re-check must never resolve group membership
for an account that has since been disabled. Internal-token only.

### List active reservation users for a device (internal)

```
GET /api/reservations/internal/active-users?device_id=<uuid>
X-Internal-Token: <internal-api-token>
```

Returns deduped user-ids of every user with an ACTIVE reservation that includes
the given device. Used by the notifications service to fan out
`device.health_transition` events to anyone currently using the device.
Internal-token only.

---

## Inventory Management (Admin Operations)

Admin and superadmin endpoints for the inventory service all require a valid
`admin` or `superadmin` JWT.

### Add a device

```
POST /api/inventory/devices
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "FW-LAB-01",
  "template_id": "uuid-of-device-template",
  "topology_type": "PHYSICAL",
  "field_data": { "ip": "10.0.0.1", "login": "admin", "password": "secret" }
}
```

Devices are typed by a device template (template_type must be "device"). Field data
is validated against the template's section definitions (required fields, type checks,
dropdown values). Device names must be unique (409 on duplicate).

`topology_type` values: `PHYSICAL`, `CLOUD`
`status` values: `AVAILABLE`, `RESERVED`, `OFFLINE`, `MAINTENANCE`

### Update a device

```
PUT /api/inventory/devices/{device_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "name": "FW-LAB-01-updated", "field_data": { "ip": "10.0.0.2", "login": "admin", "password": "new-secret" } }
```

Any combination of fields can be updated. Omitted fields are unchanged.
Field data is re-validated against the template on update.

### Remove a device

```
DELETE /api/inventory/devices/{device_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204 on success. Issue #391: refused with HTTP 409
(`{"error": "device_in_use", "reservation_ids": [...]}`) when the device is held by
a reservation in a non-terminal status (`PENDING`, `PENDING_PROVISION`, `ACTIVE`), so
the delete cannot orphan the device UUID in reservations, cabling, and execution (no
cross-schema foreign keys by design). The check calls reservations' internal
`/internal/by-device/{device_id}` lookup and fails CLOSED: an unreachable or erroring
reservations service returns HTTP 503 ("Could not verify device is not in use")
rather than silently letting the delete through. There is no force flag; cancel or
let the blocking reservation end first.

### Materialize a dynamic instance (internal, ADR 0004)

```
POST /api/inventory/devices/internal
X-Internal-Token: <internal-api-token>
Content-Type: application/json

{
  "template_id": "uuid-of-dynamic-template",
  "reservation_id": "uuid-of-reservation",
  "name": null,
  "field_data": { "image": "ubuntu-24.04", "cpu": 2, "memory_mb": 4096 }
}
```

Used by the execution service after a dynamic-resources recipe's `create_instance`
succeeds; not reachable with an admin JWT. Accepts only a `dynamic` template (422
otherwise). The created device is `RESERVED` and joined to the "No Pool" device group
like every device; when `name` is omitted, one is generated as
`<template-name>-<first-8-chars-of-reservation-id>-<n>`. `field_data` is validated
against the template's fields but tolerates unknown keys, since the recipe's
`create_instance` result carries instance attributes (e.g. a management address) that
are not template fields. Returns HTTP 201.

### Delete a dynamic instance (internal, ADR 0004)

```
DELETE /api/inventory/devices/{device_id}/internal
X-Internal-Token: <internal-api-token>
```

Used by the execution service after a dynamic-resources recipe's `destroy_instance`
succeeds. Returns HTTP 204; 404 if the device is absent; 409 if the device exists but
is not a dynamic instance, since this surface only manages dynamic instances (physical
devices stay admin-managed through the endpoints above).

---

## Backend Connection Management (Admin Operations)

Backend connections are physical cables or virtual links between device ports.
They are managed by administrators and are not exposed to end-users as raw data.
The topology canvas reflects them, but users cannot create or delete them.

### List all connections

```
GET /api/cabling/connections
Authorization: Bearer <any-authenticated-token>
```

### Create a connection

```
POST /api/cabling/connections
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "device_a_id": "uuid-of-device-a",
  "port_a": "eth0",
  "device_b_id": "uuid-of-device-b",
  "port_b": "eth1",
  "connection_type": "ethernet",
  "notes": "Core uplink between lab switches"
}
```

### Remove a connection

```
DELETE /api/cabling/connections/{connection_id}
Authorization: Bearer <admin-token>
```

---

## API Reference by Role

| Endpoint | Method | User | Admin | Superadmin |
|---|---|:---:|:---:|:---:|
| `/api/auth/register` | POST | open | open | open |
| `/api/auth/login` | POST | open | open | open |
| `/api/auth/refresh` | POST | open | open | open |
| `/api/auth/logout` | POST | open | open | open |
| `/api/auth/me` | GET | yes | yes | yes |
| `/api/auth/groups` | GET | yes | yes | yes |
| `/api/auth/groups/{id}` | GET | yes | yes | yes |
| `/api/auth/groups` | POST | | yes | yes |
| `/api/auth/groups/{id}` | PUT | | yes | yes |
| `/api/auth/groups/{id}` | DELETE | | yes | yes |
| `/api/auth/groups/{id}/members` | POST | | yes | yes |
| `/api/auth/groups/{id}/members/{uid}` | DELETE | | yes | yes |
| `/api/auth/groups/{id}/members/bulk` | POST | | yes | yes |
| `/api/auth/groups/{id}/members/bulk-remove` | POST | | yes | yes |
| `/api/auth/groups/user/{user_id}` | GET | yes | yes | yes |
| `/api/auth/groups/users/groups` | POST | yes | yes | yes |
| `/api/auth/users` | GET | | yes | yes |
| `/api/auth/users/{id}/role` | PUT | | | yes |
| `/api/auth/users/{id}/activate` | POST | | yes | yes |
| `/api/auth/users/{id}/deactivate` | POST | | yes | yes |
| `/api/auth/admin/ldap-sync/status` | GET | | yes | yes |
| `/api/auth/admin/ldap-sync/mappings` | POST | | yes | yes |
| `/api/auth/admin/ldap-sync/mappings` | GET | | yes | yes |
| `/api/auth/admin/ldap-sync/mappings/{id}` | DELETE | | yes | yes |
| `/api/auth/admin/ldap-sync/run` | POST | | yes | yes |
| `/api/auth/admin/ldap-sync/runs` | GET | | yes | yes |
| `/api/auth/admin/ldap-sync/runs/{id}` | GET | | yes | yes |
| `/api/inventory/templates` | GET | yes | yes | yes |
| `/api/inventory/templates/{id}` | GET | yes | yes | yes |
| `/api/inventory/templates` | POST | | yes | yes |
| `/api/inventory/templates/{id}` | PUT | | yes | yes |
| `/api/inventory/templates/{id}` | DELETE | | yes | yes |
| `/api/inventory/drivers` | GET | yes | yes | yes |
| `/api/inventory/drivers/{id}` | GET | yes | yes | yes |
| `/api/inventory/drivers/{id}/download` | GET | yes | yes | yes |
| `/api/inventory/drivers` | POST | | yes | yes |
| `/api/inventory/drivers/{id}` | PUT | | yes | yes |
| `/api/inventory/drivers/{id}/file` | PUT | | yes | yes |
| `/api/inventory/drivers/{id}` | DELETE | | yes | yes |
| `/api/inventory/devices` | GET | yes | yes | yes |
| `/api/inventory/devices/{id}` | GET | yes | yes | yes |
| `/api/inventory/devices/batch` | POST | yes | yes | yes |
| `/api/inventory/devices` | POST | | yes | yes |
| `/api/inventory/devices/{id}` | PUT | | yes | yes |
| `/api/inventory/devices/{id}` | DELETE | | yes | yes |
| `/api/inventory/devices/{id}/status` | POST | internal | internal | internal |
| `/api/inventory/devices/internal` | POST | internal | internal | internal |
| `/api/inventory/devices/{id}/internal` | DELETE | internal | internal | internal |
| `/api/inventory/drivers/{id}/internal-download` | GET | internal | internal | internal |
| `/api/inventory/hypervisors` | GET | | yes | yes |
| `/api/inventory/hypervisors/{id}` | GET | | yes | yes |
| `/api/inventory/hypervisors` | POST | | yes | yes |
| `/api/inventory/hypervisors/{id}` | PUT | | yes | yes |
| `/api/inventory/hypervisors/{id}` | DELETE | | yes | yes |
| `/api/inventory/hypervisors/{id}/internal` | GET | internal | internal | internal |
| `/api/inventory/hypervisors/by-secret/{id}/internal` | GET | internal | internal | internal |
| `/api/inventory/devices/{id}/ports` | GET | yes | yes | yes |
| `/api/inventory/devices/{id}/ports` | POST | | yes | yes |
| `/api/inventory/devices/{id}/ports/bulk` | POST | | yes | yes |
| `/api/inventory/ports/{id}` | GET | yes | yes | yes |
| `/api/inventory/ports/{id}` | PUT | | yes | yes |
| `/api/inventory/ports/{id}` | DELETE | | yes | yes |
| `/api/inventory/device-groups` | GET | | yes | yes |
| `/api/inventory/device-groups/{id}` | GET | | yes | yes |
| `/api/inventory/device-groups` | POST | | yes | yes |
| `/api/inventory/device-groups/{id}` | PUT | | yes | yes |
| `/api/inventory/device-groups/{id}` | DELETE | | yes | yes |
| `/api/inventory/device-groups/{id}/devices/bulk` | POST | | yes | yes |
| `/api/inventory/device-groups/{id}/devices/bulk-remove` | POST | | yes | yes |
| `/api/inventory/device-groups/{id}/permissions/bulk` | POST | | yes | yes |
| `/api/inventory/device-groups/{id}/permissions/bulk-remove` | POST | | yes | yes |
| `/api/inventory/device-groups/visible-devices` | GET | yes (own `user_id` only) | yes | yes |
| `/api/reservations/` | POST | yes | yes | yes |
| `/api/reservations/` | GET | yes (own only) | own, or all with `all=true` | own, or all with `all=true` |
| `/api/reservations/calendar` | GET | yes | yes | yes |
| `/api/reservations/{id}` | GET | yes (owner only) | yes (owner only) | yes (owner only) |
| `/api/reservations/{id}` | PATCH | yes (owner only) | yes (owner only) | yes (owner only) |
| `/api/reservations/{id}` | DELETE | yes (owner only) | any reservation | any reservation |
| `/api/reservations/{id}/release` | PUT | yes | yes | yes |
| `/api/reservations/{id}/fork` | GET | owner only | owner or admin | owner or admin |
| `/api/reservations/{id}/fork/canvas` | PUT | owner only, ACTIVE only | owner or admin, ACTIVE only | owner or admin, ACTIVE only |
| `/api/reservations/{id}/fork/save` | POST | owner only, ACTIVE only | owner or admin, ACTIVE only | owner or admin, ACTIVE only |
| `/api/reservations/{id}/wiring-status` | GET | owner only, any status | owner or admin, any status | owner or admin, any status |
| `/api/reservations/{id}/wiring/retry` | POST | owner only, not PENDING/PENDING_PROVISION | owner or admin, not PENDING/PENDING_PROVISION | owner or admin, not PENDING/PENDING_PROVISION |
| `/api/reservations/internal/{id}/provision-result` | POST | internal | internal | internal |
| `/api/reservations/reports/utilization` | GET | | yes | yes |
| `/api/reservations/reports/utilization.csv` | GET | | yes | yes |
| `/api/cabling/connections` | GET | yes | yes | yes |
| `/api/cabling/connections/{id}` | GET | yes | yes | yes |
| `/api/cabling/connections` | POST | | yes | yes |
| `/api/cabling/connections/{id}` | DELETE | | yes | yes |
| `/api/cabling/topologies` | GET | yes | yes | yes |
| `/api/cabling/topologies/{id}` | GET | yes | yes | yes |
| `/api/cabling/topologies` | POST | yes | yes | yes |
| `/api/cabling/topologies/{id}` | PUT | creator | yes | yes |
| `/api/cabling/topologies/{id}` | DELETE | creator | yes | yes |
| `/api/cabling/topologies/export` | GET | yes | yes | yes |
| `/api/cabling/topologies/import` | POST | create yes; update creator, per row | yes | yes |
| `/api/cabling/internal/forks` | POST | internal | internal | internal |
| `/api/acl/grants` | GET | | yes | yes |
| `/api/acl/grants/{id}` | GET | | yes | yes |
| `/api/acl/grants` | POST | | yes | yes |
| `/api/acl/grants/{id}` | DELETE | | yes | yes |
| `/api/acl/check` | POST | yes | yes | yes |
| `/api/acl/check/batch` | POST | yes | yes | yes |
| `/api/acl/resources` | GET | yes | yes | yes |
| `/api/execution/runs` | GET | yes (owner, with `reservation_id`) | yes | yes |
| `/api/execution/runs/{id}` | GET | | yes | yes |
| `/api/execution/execute` | POST | yes (`configure` only, with device `manage` grant) | yes | yes |
| `/api/execution/runs/{id}/retry` | POST | | yes | yes |
| `/api/execution/device-check` | POST | internal | internal | internal |
| `/api/auth/health` | GET | open | open | open |
| `/api/inventory/health` | GET | open | open | open |
| `/api/reservations/health` | GET | open | open | open |
| `/api/cabling/health` | GET | open | open | open |
| `/api/acl/health` | GET | open | open | open |
| `/api/execution/health` | GET | open | open | open |
| `/api/user-profile/health` | GET | open | open | open |

`POST /api/auth/logout` carries no auth dependency; it revokes the refresh token passed
in the request body, mirroring `/login` and `/refresh`. `PATCH /api/reservations/{id}`
is owner-scoped: any authenticated user may call it, but the update only ever applies to
a reservation the caller owns (404 otherwise), with no admin bypass.

`GET /api/reservations/` defaults to the caller's own reservations. Admins and
superadmins may pass `all=true` to list every user's reservations; a non-admin who
passes `all=true` is rejected with 403 `Only admins can list all reservations` (issue
#340). `DELETE /api/reservations/{id}` lets an admin or superadmin cancel any
reservation, not just their own; a non-admin cancelling a reservation they do not own
still gets 404. An admin cancelling a reservation they do not own is recorded in the
reservation's `cancelled_by` field (an owner self-cancel leaves it null), and emits the
same `reservation.cancelled` event as an owner self-cancel.

---

## LDAP / Active Directory authentication

When `AUTH_METHOD=ldap` (see [ENV_VARS.md](ENV_VARS.md#ldap--active-directory)):

- Login verifies the submitted password via an LDAP bind instead of bcrypt.
- On first successful bind, a HERD `User` row is created with `auth_source=ldap`
  and no local password hash. Subsequent logins reuse that row.
- `/register` returns 409 because account creation is driven by the directory.
- Role assignment (`user`, `admin`, `superadmin`) and `UserGroup` membership
  are still managed entirely inside HERD. Promote LDAP-provisioned users to
  `admin` or add them to groups using the existing admin UI / endpoints.
- Directory group sync (ADR 0011, `docs/design/0011-ldap-group-sync.md`,
  issue #38) is fully delivered, all 6 phases: admins can map
  directory groups to HERD groups (`/api/auth/admin/ldap-sync/mappings`),
  trigger a reconcile on demand with `POST /api/auth/admin/ldap-sync/run`
  (202; poll `/runs`), opt in to the deactivation/reactivation sweep
  (`LDAP_SYNC_DEACTIVATION_ENABLED`, dark by default; a two-term circuit
  breaker guards mass deactivation, and only sync-deactivated users are
  ever auto-reactivated; admin intent via
  `/api/auth/users/{id}/activate|deactivate` always outranks the
  directory), and opt in to a background loop that runs the same reconcile
  on an interval (`LDAP_GROUP_SYNC_ENABLED`, also dark by default;
  `LDAP_SYNC_INTERVAL_SECONDS` sets the cadence, and the loop also prunes
  `ldap_sync_runs` rows older than `LDAP_SYNC_RUNS_RETENTION_DAYS`). The
  admin UI (`/admin/ldap-sync`, phase 6) covers mapping CRUD and sync-now
  plus run history, reading mode context from
  `GET /api/auth/admin/ldap-sync/status`. Role assignment never syncs;
  HERD stays the authority for role.
