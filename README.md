# HERD: Hardware Environment Replication and Deployment

Lab inventory topology management system for customer support and engineering QA teams.

NOTE: project is still in progress and not complete yet

## What it does

- Browse and reserve networking lab equipment (firewalls, switches, routers, traffic shapers) from physical labs or cloud environments
- Build network topologies via drag-and-drop, connecting devices at Layer 1/2/3
- Enforce physical/cloud topology separation; physical and cloud devices cannot be mixed in a single topology
- Backend connections between devices are managed by administrators

## Architecture

```
+-------------+     +-----------+     +--------------+
|   Frontend  |---->|  Traefik  |---->|  Auth Svc    |
|  React+Vite |     |  Gateway  |     +--------------+
+-------------+     +-----------+     |  Inventory   |
                                      +--------------+
                                      | Reservations |
                                      +--------------+
                                      |  Cabling     |
                                      +--------------+
                                      |  ACL (stub)  |
                                      |  UserProfile |
                                      +--------------+
                    +---------------------------+
                    |  PostgreSQL  |    NATS     |
                    +---------------------------+
```

## Tech Stack

| Layer | Technology |
|---|---|
| Backend services | Python 3.12 + FastAPI + SQLAlchemy (async) + Alembic |
| Package manager | `uv` with workspace support |
| Database | PostgreSQL 16 (schema-per-service) |
| Async events | NATS JetStream |
| API Gateway (dev) | Traefik v3 |
| Frontend | React 18 + TypeScript + Vite |
| Topology editor | React Flow (xyflow) |
| State management | Zustand + TanStack Query |
| Component library | Tailwind CSS |
| Auth | Custom JWT service (bcrypt + python-jose) |

## Roles

HERD has three roles: **user**, **admin**, and **superadmin**.

| Role | What they can do |
|---|---|
| User | Browse inventory, build topology diagrams, create and manage their own reservations |
| Admin | Everything a user can do, plus add/update/remove devices and manage backend connections |
| Superadmin | Everything an admin can do, plus promote or demote other users (user <-> admin) |

See [docs/ROLES.md](docs/ROLES.md) for the full reference including API endpoints by role
and instructions for creating the superadmin account.

## Quickstart

### Prerequisites

- Docker + Docker Compose
- `uv` (Python package manager)
- Node.js 20+ (for local frontend development)

### Run the full stack

```bash
# Copy environment template and configure it
cp .env.example .env

# Set the superadmin credentials in .env before first startup:
#   SUPERADMIN_EMAIL, SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD

make up

# View logs
docker compose logs -f
```

The app is available at `http://localhost`.

### API Endpoints

| Service | Base Path |
|---|---|
| Auth | `http://localhost/api/auth` |
| Inventory | `http://localhost/api/inventory` |
| Reservations | `http://localhost/api/reservations` |
| Cabling | `http://localhost/api/cabling` |
| ACL | `http://localhost/api/acl` |
| User Profile | `http://localhost/api/user-profile` |

### Development commands

```bash
make up        # Start full stack
make down      # Stop full stack
make build     # Rebuild all images
make test      # Run tests across all services
make migrate   # Run Alembic migrations
make logs      # Tail logs
```

### Verification

1. Set `SUPERADMIN_*` vars in `.env` and run `make up`
2. Log in as superadmin: `POST /api/auth/login`
3. Register a second user: `POST /api/auth/register`
4. Promote them to admin: `PUT /api/auth/users/{id}/role` with `{"role": "admin"}`
5. As admin, add devices: `POST /api/inventory/devices`
6. As user, browse devices: `GET /api/inventory/devices`
7. As user, attempt to add a device (expect HTTP 403)
8. As user, create a reservation: `POST /api/reservations/`
9. Try mixing PHYSICAL + CLOUD devices in one reservation (expect HTTP 422)
10. Try reserving the same device twice in an overlapping window (expect HTTP 409)

## Service Details

### Auth Service
Registration, login, JWT issuance, refresh token rotation, logout, and superadmin
user management endpoints.

### Inventory Service
Device catalogue with CRUD. Reads are available to all authenticated users.
Writes (add, update, delete) require admin or superadmin role.

### Reservations Service
Time-window reservations with topology-type enforcement (PHYSICAL and CLOUD devices
cannot be mixed) and conflict detection for overlapping time windows.

### Cabling Service
Backend connection management between device ports. Reads are available to all
authenticated users. Creating and removing connections requires admin or superadmin role.

### ACL / User Profile
Stub services, ready to be built out.

## Documentation

- [docs/ROLES.md](docs/ROLES.md): Full role and permissions reference

## License

Apache 2.0. See [LICENSE](LICENSE).
