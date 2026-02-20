# HERD: Roles and Permissions

HERD uses three roles. Every authenticated user holds exactly one role, which is encoded
in their JWT access token and enforced independently by each service.

---

## Roles at a Glance

| Capability | User | Admin | Superadmin |
|---|:---:|:---:|:---:|
| Register and log in | yes | yes | yes |
| Browse device inventory | yes | yes | yes |
| Build topology diagrams | yes | yes | yes |
| Create and cancel reservations | yes | yes | yes |
| View backend connections | yes | yes | yes |
| Add devices to inventory | | yes | yes |
| Update devices in inventory | | yes | yes |
| Remove devices from inventory | | yes | yes |
| Create backend connections (cabling) | | yes | yes |
| Delete backend connections | | yes | yes |
| List all user accounts | | | yes |
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

A user cannot:
- Add, modify, or remove devices from the inventory
- Create or remove backend cabling connections
- View or manage other users' accounts

### Admin

The `admin` role is granted by the superadmin. An admin has all user capabilities plus:

- Add new devices to the inventory (`POST /api/inventory/devices`)
- Update device details or status (`PUT /api/inventory/devices/{id}`)
- Remove devices from the inventory (`DELETE /api/inventory/devices/{id}`)
- Create backend connections between device ports (`POST /api/cabling/connections`)
- Remove backend connections (`DELETE /api/cabling/connections/{id}`)

Backend connections represent physical cables or virtual links between devices.
These are created by admins after a topology has been agreed and are not something
end-users configure themselves.

### Superadmin

There is exactly one superadmin account per deployment. It is created automatically
on first startup from environment variables (see Setup below) and cannot be created
or removed through the API.

The superadmin has all admin capabilities plus:

- List all registered user accounts (`GET /api/auth/users`)
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

All admin management endpoints require a valid superadmin JWT in the
`Authorization: Bearer <token>` header.

### List all users

```
GET /api/auth/users
Authorization: Bearer <superadmin-token>
```

Returns an array of all user accounts with their current role.

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
  "device_type": "FIREWALL",
  "topology_type": "PHYSICAL",
  "status": "AVAILABLE",
  "location": "Rack A, Unit 3",
  "specs": { "model": "PA-3220", "throughput_gbps": 5 }
}
```

`device_type` values: `FIREWALL`, `SWITCH`, `ROUTER`, `TRAFFIC_SHAPER`, `OTHER`
`topology_type` values: `PHYSICAL`, `CLOUD`
`status` values: `AVAILABLE`, `RESERVED`, `OFFLINE`, `MAINTENANCE`

### Update a device

```
PUT /api/inventory/devices/{device_id}
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "status": "MAINTENANCE" }
```

Any combination of fields can be updated. Omitted fields are unchanged.

### Remove a device

```
DELETE /api/inventory/devices/{device_id}
Authorization: Bearer <admin-token>
```

Returns HTTP 204 on success.

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
| `/api/auth/logout` | POST | yes | yes | yes |
| `/api/auth/me` | GET | yes | yes | yes |
| `/api/auth/users` | GET | | | yes |
| `/api/auth/users/{id}/role` | PUT | | | yes |
| `/api/inventory/devices` | GET | yes | yes | yes |
| `/api/inventory/devices/{id}` | GET | yes | yes | yes |
| `/api/inventory/devices` | POST | | yes | yes |
| `/api/inventory/devices/{id}` | PUT | | yes | yes |
| `/api/inventory/devices/{id}` | DELETE | | yes | yes |
| `/api/reservations/` | POST | yes | yes | yes |
| `/api/reservations/` | GET | yes | yes | yes |
| `/api/reservations/{id}` | GET | yes | yes | yes |
| `/api/reservations/{id}` | DELETE | yes | yes | yes |
| `/api/reservations/{id}/release` | PUT | yes | yes | yes |
| `/api/cabling/connections` | GET | yes | yes | yes |
| `/api/cabling/connections/{id}` | GET | yes | yes | yes |
| `/api/cabling/connections` | POST | | yes | yes |
| `/api/cabling/connections/{id}` | DELETE | | yes | yes |
| `/api/acl/health` | GET | open | open | open |
| `/api/user-profile/health` | GET | open | open | open |
| `/api/cabling/health` | GET | open | open | open |
