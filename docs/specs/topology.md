# Topology specification

| | |
|---|---|
| Area prefix | `TOPO` (used in rule identifiers, for example `TOPO-CONN-1`) |
| Verified at | commit `fd589e50` (`v0.6.0-68-gfd589e50`), 2026-10-05 |
| Owning services | cabling (`services/cabling/`); the topology editor and its pages in `frontend/` |
| Other services involved | inventory (device-group membership, device visibility, device names, device type and Layer 3 config), reservations (the reservation lookup behind the edit lock and the delete guard; the only caller of the fork write routes), execution (reads fork wiring, fabric membership), ai-orchestrator (pathfind, the user-facing validate route, fork reads), auth (JWT only) |
| Design records | [ADR 0001](../design/0001-editable-reservation-topologies.md), [ADR 0006](../design/0006-fork-reconcile-and-as-built.md), [ADR 0012](../design/0012-network-element-objects.md), [ADR 0014](../design/0014-first-class-layer-3-routing.md) |
| Related guides | [TOPOLOGY_EDITOR.md](../TOPOLOGY_EDITOR.md), [BULK_IMPORT_EXPORT.md](../BULK_IMPORT_EXPORT.md), [ROLES.md](../ROLES.md), [ARCHITECTURE.md](../ARCHITECTURE.md), [USER_GUIDE.md](../USER_GUIDE.md), [ENV_VARS.md](../ENV_VARS.md) |

All API paths below are the cabling service's own paths. Through the gateway they are
prefixed with `/api/cabling` (for example `GET /api/cabling/topologies`).

## 1. Purpose

Cabling records which device port is physically cabled to which, finds the paths
those cables make between two devices, and stores topologies: diagrams a person draws
of the devices they need and how those devices should be wired. It checks a diagram
against the real cabling, keeps a version history of every diagram, and, while a
reservation is live, keeps that reservation's private editable copy (the fork) and the
wiring that copy asks for. It does not drive hardware, book devices, or own devices
and ports: execution, reservations, and inventory do.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). Ownership and visibility rules
beyond role are numbered rules in section 8; section 5 names the caller condition for
each route.

| Actor | May | May not |
|---|---|---|
| User | Read every topology, template, and version; create topologies, clones, templates, and imports; edit, restore, validate, and delete topologies they created; edit and delete templates they created; list and read cabling (the list filtered to devices they can see); pathfind between devices they can see | Create or delete cabling; edit, restore, validate, or delete another user's topology; update another user's topology through import; learn anything about a device outside their device-group visibility through validate, pathfind, the connection list, or import |
| Admin | Everything a user may, unfiltered by device visibility; create and delete cabling; edit, validate, restore, and delete any topology and template | Bypass the topology delete guard (TOPO-DEL-2) or the fork membership check (TOPO-FORK-13) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Create, read, edit, save, restore, prune, and archive forks; list active forks; ask what still names a device; read fork device sets; read the unfiltered connection list; validate a topology without a user; read a device's fabric (section 7) | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Connection | One physical cable or link between `device_a_id`/`port_a` and `device_b_id`/`port_b`. Device ids are bare inventory ids, no foreign key; port names are free text, not checked against inventory | cabling | `connections` (`Connection` in `services/cabling/app/models/connection.py`) |
| Path | A sequence of devices joined by connections between two devices, with the port each cable leaves and enters on | cabling (computed) | not stored |
| Fabric | The connected component of the cabling graph a device belongs to, named by a deterministic id | cabling (computed) | not stored |
| Topology | A named diagram: `canvas_data` holds React Flow `nodes` and `edges`. `created_by` is a bare auth user id | cabling | `topologies` (`Topology` in `services/cabling/app/models/topology.py`) |
| Topology version | An immutable snapshot of a topology's canvas, numbered per topology | cabling | `topology_versions` (`TopologyVersion`) |
| Device node | A canvas node whose `data.device` is an object; `data.device.id` is a bare inventory id | cabling (canvas content) | inside `canvas_data` |
| Network element | A canvas node of type `networkElementNode` standing for a VLAN segment, subnet, external cloud, or patch trunk; it has no device and no ports | cabling (canvas content) | inside `canvas_data` |
| Dynamic placeholder | A canvas node standing for N hypervisor instances; frontend only, never stored by cabling | frontend | not stored |
| Routing intent | `data.l3.routes` on a device node: Layer 3 routes the user wants on a Layer 3 switch | cabling (canvas content) | inside `canvas_data` |
| Topology template | A reusable canvas whose device nodes carry role labels instead of devices | cabling | `topology_templates` (`TopologyTemplate` in `services/cabling/app/models/template.py`) |
| Fork | One reservation's editable copy of its topology, keyed by a bare reservation id, with a draft canvas, a status, and a pin to the parent version | cabling | `reservation_fork` (`ReservationFork` in `services/cabling/app/models/fork.py`) |
| Fork connection | One physical hop of the wiring the fork's last save asks for (its intended set), with the canvas edge it came from (`edge_key`) and the backing connection id | cabling | `fork_connections` (`ForkConnection`) |
| Fork version | An immutable snapshot of a fork canvas at create, save, or prune | cabling | `fork_versions` (`ForkVersion`) |
| Fork route | One resolved route of the fork's routing intent, keyed by device and route identity | cabling | `fork_l3_routes` (`ForkL3Route`) |
| Restore marker | `draft_restored_from_id`: the fork version the current draft was restored from, until the next save | cabling | `reservation_fork.draft_restored_from_id` |
| Port claim | A physical `(device, port)` endpoint held by a fork connection of an `ACTIVE` fork | cabling (derived) | `fork_connections` joined to `reservation_fork` |

## 4. State model

The fork has a lifecycle; topologies and templates do not. Fork and topology version
numbering and the restore marker are state written here too, so their rules live in
this section.

**Statuses (fork).**

- `ACTIVE`: the reservation is live or has not been archived yet; the draft can be
  edited, saved, restored, and pruned, and its connections are port claims.
- `ARCHIVED`: the immutable as-built record. Readable, never writable, never a claim.

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | `ACTIVE` | `POST /internal/forks` | no fork exists for the reservation; canvas devices are members; no port claim collides | nothing | TOPO-FSTATE-1, TOPO-FORK-3, TOPO-CLAIM-1 |
| `ACTIVE` | `ARCHIVED` | `POST /internal/forks/{reservation_id}/archive` | none | nothing | TOPO-FSTATE-2 |
| `ARCHIVED` | `ARCHIVED` | `POST /internal/forks/{reservation_id}/archive` | none (idempotent) | nothing | TOPO-FSTATE-2 |

Cabling publishes no event. The `reservation.wiring_changed` event a save leads to is
staged by reservations (`reservations.md`, RES-FORK-8).

**Concurrency.** A second fork create for one reservation loses on the unique
`reservation_id` and returns the winner (TOPO-FSTATE-1). Every fork writer locks the
fork row `FOR UPDATE` from its load to its commit (TOPO-MARKER-3). Version numbers are
allocated as `max + 1` under a unique constraint with a bounded retry (TOPO-FVER-5,
TOPO-TVER-4). Archive takes no explicit lock: on Postgres its update waits on a
writer's row lock.

**Rules.**

- **TOPO-FSTATE-1.** Fork create makes one `ACTIVE` fork per reservation; a repeat call,
  or the loser of a concurrent create (its `IntegrityError` at flush or commit), returns
  the existing fork unchanged and is not re-validated. \
  Enforced in: `services/cabling/app/services/fork_service.py` (`create_fork`); `services/cabling/app/models/fork.py` (`ReservationFork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_is_idempotent`, `test_create_fork_returns_winner_on_flush_integrity_error`, `test_create_fork_returns_winner_on_commit_integrity_error`, `test_reservation_fork_unique_reservation`)
- **TOPO-FSTATE-2.** Archive sets an `ACTIVE` fork to `ARCHIVED` and appends no version;
  archiving an `ARCHIVED` fork answers 200 with its state, and archiving a reservation
  with no fork answers 204. \
  Enforced in: `services/cabling/app/routes/forks.py` (`archive_fork_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_archive_fork_freezes_and_is_idempotent`, `test_archive_fork_appends_no_version`, `test_archive_fork_absent_returns_204`)
- **TOPO-FSTATE-3.** `ARCHIVED` is final: canvas PUT, save, restore, and prune on an
  archived fork answer 409 `Fork is archived and cannot be edited` and change nothing;
  no route moves a fork back to `ACTIVE`. \
  Enforced in: `services/cabling/app/routes/forks.py` (`update_fork_canvas_internal`, `save_fork_internal`, `restore_fork_version_internal`, `prune_fork_devices_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_update_fork_canvas_refuses_archived`, `test_save_fork_refuses_archived`); `services/cabling/tests/test_fork_versions.py` (`test_restore_409_when_fork_archived`); `services/cabling/tests/test_fork_prune.py` (`test_prune_refuses_archived`)
- **TOPO-FSTATE-4.** Save checks `ARCHIVED` twice: on a first unlocked load, and again
  after re-loading the row `FOR UPDATE` with a forced refresh, so an archive committed
  while the save was making inventory calls refuses the save with no version appended. \
  Enforced in: `services/cabling/app/routes/forks.py` (`save_fork_internal`, `_load_fork`) \
  Pinned by: `services/cabling/tests/test_fork_versions.py` (`test_save_archived_during_gate_window_is_refused_with_no_version`); `services/cabling/tests/test_fork_restore_save_race_live_pg.py` (`test_save_route_refuses_archived_in_the_gap_live_pg`)
- **TOPO-FVER-1.** Fork create writes fork version 1 holding the forked canvas. \
  Enforced in: `services/cabling/app/services/fork_service.py` (`create_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_deep_copies_canvas_and_pins_version`)
- **TOPO-FVER-2.** Every accepted save appends exactly one fork version, whether or not
  any wiring changed. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`save_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_builds_new_wire`, `test_save_fork_unchanged_wire_is_not_rewritten`); `services/cabling/tests/test_route_handlers_direct.py` (`test_save_fork_handler_builds_wire_and_bumps_version`)
- **TOPO-FVER-3.** Canvas PUT and version restore append no fork version. \
  Enforced in: `services/cabling/app/routes/forks.py` (`update_fork_canvas_internal`, `restore_fork_version_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_update_fork_canvas_stores_draft_without_reconcile_or_version`); `services/cabling/tests/test_fork_versions.py` (`test_restore_appends_no_version_and_sets_marker`, `test_restore_twice_still_appends_no_version`)
- **TOPO-FVER-4.** Prune appends a version only when it released wiring or routes; that
  version holds the last saved canvas with the removed devices pruned, never the draft. A
  prune that releases nothing appends nothing, even when it scrubbed the draft. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`prune_fork_devices`) \
  Pinned by: `services/cabling/tests/test_fork_prune.py` (`test_prune_releases_removed_device_rows_and_bumps_version`, `test_prune_is_idempotent_on_replay`, `test_prune_scrubs_draft_only_content_without_a_version`)
- **TOPO-FVER-5.** A fork version number is `max + 1` for that fork under the unique
  `(fork_id, version_number)`; a collision rolls back, reapplies the canvas, the restore
  marker, and the whole reconcile, and retries, at most 5 attempts, then the
  `IntegrityError` propagates. \
  Enforced in: `services/cabling/app/services/version_service.py` (`commit_fork_with_new_version`, `_commit_with_new_version`, `_MAX_ALLOCATE_RETRIES`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_commit_fork_with_new_version_allocates_next_number`, `test_commit_fork_with_new_version_retries_on_conflict`, `test_commit_fork_with_new_version_exhausts_retries_and_raises`, `test_fork_versions_unique_number`)
- **TOPO-MARKER-1.** Restore sets the fork's `draft_restored_from_id` to the restored
  version; a canvas PUT leaves it as it is. \
  Enforced in: `services/cabling/app/routes/forks.py` (`restore_fork_version_internal`, `update_fork_canvas_internal`) \
  Pinned by: `services/cabling/tests/test_fork_versions.py` (`test_restore_appends_no_version_and_sets_marker`, `test_canvas_put_between_restore_and_save_keeps_marker`)
- **TOPO-MARKER-2.** The next save copies the marker onto its new version's
  `restored_from_id` and clears it on the fork in the same transaction; a version-race
  retry reapplies both, so the marker is never resurrected. A save with no marker writes
  `restored_from_id` null. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`save_fork`); `services/cabling/app/services/version_service.py` (`commit_fork_with_new_version`) \
  Pinned by: `services/cabling/tests/test_fork_versions.py` (`test_save_after_restore_carries_marker_and_clears_it`, `test_second_save_after_restore_carries_no_marker`); `services/cabling/tests/test_forks.py` (`test_commit_fork_with_new_version_reapplies_restore_marker_on_retry`)
- **TOPO-MARKER-3.** Restore, canvas PUT, save, and prune load the fork row
  `FOR UPDATE` and hold it to their own commit, so a restore marker is consumed by exactly
  one saved version or is still on the fork. The read routes never lock. On SQLite the
  lock is a no-op. \
  Enforced in: `services/cabling/app/routes/forks.py` (`_load_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_fork_mutating_routes_load_the_fork_row_for_update`); `services/cabling/tests/test_fork_restore_save_race_live_pg.py` (`test_save_holds_lock_so_a_racing_restore_waits_and_wins_last`, `test_restore_holds_lock_so_a_racing_save_consumes_the_fresh_marker`, `test_save_route_consumes_restore_committed_in_the_gap_live_pg`)
- **TOPO-TVER-1.** A topology PUT whose stripped canvas differs from the stored one
  appends a topology version (`name`, optional `description`, author id and name); a PUT
  with the same canvas, with no canvas, or with only a name appends none. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_first_save_creates_version_one`, `test_second_distinct_save_creates_version_two`, `test_identical_save_is_deduped`, `test_name_only_save_does_not_create_version`)
- **TOPO-TVER-2.** `POST /topologies` writes no version; clone, template instantiate, and
  an import that creates a topology each write version 1 with a fixed description
  (`Cloned from <name>`, `Instantiated from template <name>`, `Imported via bulk import`). \
  Enforced in: `services/cabling/app/routes/topologies.py` (`create_topology`, `clone_topology`); `services/cabling/app/routes/templates.py` (`instantiate_template`); `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_clone_topology_writes_v1_snapshot`); `services/cabling/tests/test_templates.py` (`test_instantiate_substitutes_devices_and_creates_v1_snapshot`); `services/cabling/tests/test_topology_versions.py` (`test_first_save_creates_version_one`)
- **TOPO-TVER-3.** Restoring a topology version always appends a new version carrying
  `restored_from_id`, described `Restored from v<N>` unless the caller supplies a
  description, even when the restored canvas equals the current one. \
  Enforced in: `services/cabling/app/routes/versions.py` (`restore_version`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_restore_applies_snapshot_and_creates_new_version`); `services/cabling/tests/test_route_handlers_direct.py` (`test_versions_restore_default_description`)
- **TOPO-TVER-4.** A topology version number is `max + 1` for that topology under the
  unique `(topology_id, version_number)`, with the same 5-attempt retry as forks. \
  Enforced in: `services/cabling/app/services/version_service.py` (`commit_with_new_version`, `_commit_with_new_version`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_update_topology_retries_on_version_number_conflict`, `test_restore_retries_on_version_number_conflict`, `test_commit_with_new_version_exhausts_retries_and_raises`)
- **TOPO-TVER-5.** Deleting a topology deletes its versions. \
  Enforced in: `services/cabling/app/models/topology.py` (`TopologyVersion`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_delete_topology_cascades_versions`)

## 5. API surface

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| GET | `/connections` | any signed-in user; a non-admin sees only rows touching a visible device | 200 | TOPO-CONN-6, TOPO-CONN-7, TOPO-CONN-8, TOPO-VIS-1 |
| GET | `/connections/{id}` | any signed-in user | 200 | TOPO-CONN-9 |
| POST | `/connections` | admin | 201 | TOPO-CONN-1 to TOPO-CONN-5, TOPO-BOUND-1 to TOPO-BOUND-3, TOPO-BOUND-6 |
| POST | `/connections/bulk` | admin | 200 | TOPO-CONN-12, TOPO-CONN-13, TOPO-CONN-14, TOPO-BOUND-4, TOPO-BOUND-5 |
| DELETE | `/connections/{id}` | admin | 204 | TOPO-CONN-10, TOPO-CONN-11 |
| POST | `/pathfind` | any signed-in user; a non-admin only between visible devices | 200 | TOPO-PATH-1 to TOPO-PATH-5, TOPO-PATH-8, TOPO-PATH-9, TOPO-PATH-11 |
| POST | `/pathfind/batch` | any signed-in user; a non-admin's hidden pairs are refused per pair | 200 | TOPO-PATH-7, TOPO-PATH-10, TOPO-PATH-11 |
| GET | `/topologies` | any signed-in user | 200 | TOPO-LIST-1 to TOPO-LIST-6 |
| POST | `/topologies` | any signed-in user | 201 | TOPO-CRUD-1, TOPO-TVER-2 |
| GET | `/topologies/{id}` | any signed-in user | 200 | TOPO-CRUD-2, TOPO-STRIP-3 |
| PUT | `/topologies/{id}` | creator, or any admin | 200 | TOPO-EDIT-1 to TOPO-EDIT-6, TOPO-TVER-1, TOPO-STRIP-2, TOPO-STRIP-4 |
| DELETE | `/topologies/{id}` | creator, or any admin | 204 | TOPO-DEL-1, TOPO-DEL-2, TOPO-DEL-3, TOPO-TVER-5 |
| POST | `/topologies/{id}/clone` | any signed-in user | 201 | TOPO-CLONE-1, TOPO-TVER-2 |
| POST | `/topologies/{id}/validate` | creator, or any admin | 200 | TOPO-VAL-1 to TOPO-VAL-11, TOPO-VAL-13 |
| GET | `/topologies/{id}/versions` | any signed-in user | 200 | TOPO-VER-1 |
| GET | `/topologies/{id}/versions/diff?a&b` | any signed-in user | 200 | TOPO-VER-3, TOPO-STRIP-3 |
| GET | `/topologies/{id}/versions/{vid}` | any signed-in user | 200 | TOPO-VER-2, TOPO-STRIP-3 |
| POST | `/topologies/{id}/versions/{vid}/restore` | creator, or any admin | 200 | TOPO-VER-4, TOPO-VER-5, TOPO-TVER-3 |
| GET | `/topologies/export?format` | any signed-in user | 200 | TOPO-BULK-1 to TOPO-BULK-5 |
| POST | `/topologies/import?format&dry_run` | any signed-in user; updates creator or admin per row | 200 | TOPO-BULK-5 to TOPO-BULK-18 |
| GET | `/templates` | any signed-in user | 200 | TOPO-TMPL-1 |
| POST | `/templates` | any signed-in user | 201 | TOPO-TMPL-2, TOPO-TMPL-3 |
| POST | `/templates/from-topology/{topology_id}` | any signed-in user | 201 | TOPO-TMPL-5, TOPO-TMPL-6, TOPO-TMPL-7 |
| GET | `/templates/{id}` | any signed-in user | 200 | TOPO-TMPL-1, TOPO-STRIP-3 |
| PUT | `/templates/{id}` | creator, or any admin | 200 | TOPO-TMPL-4 |
| DELETE | `/templates/{id}` | creator, or any admin | 204 | TOPO-TMPL-4 |
| POST | `/templates/{id}/instantiate` | any signed-in user | 201 | TOPO-TMPL-8, TOPO-TMPL-9, TOPO-TVER-2 |

`/topologies/export` and `/topologies/import` are declared before `/topologies/{id}`, so
the literal paths are never read as an id. `GET /connections` and the version list take
`skip` (default 0) and `limit` (1 to 500, default 50); `GET /connections` also takes
`device_id`. A missing or invalid bearer token is 401 on every route; a non-admin on an
admin route is 403 (section 9).

## 6. Events

None. Cabling publishes no event. Reservations stages `reservation.wiring_changed` from
a save's answer (`reservations.md`, RES-FORK-8).

## 7. Internal API

Every row is guarded by `X-Internal-Token` (TOPO-FORK-1).

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| GET | `/connections/internal` | `X-Internal-Token` | no caller in `services/` today | the unfiltered connection page | TOPO-CONNINT-1 |
| GET | `/fabric/internal?device_id` | `X-Internal-Token` | execution (VLAN allocation scope) | `{device_id, fabric_id, component_size}` | TOPO-FABRIC-1 |
| POST | `/topologies/{id}/validate/internal?l3` | `X-Internal-Token` | reservations (create and device-set PATCH) | `TopologyValidationResponse` | TOPO-VAL-12, TOPO-VAL-14 |
| POST | `/internal/forks` | `X-Internal-Token` | reservations (activation, sweep backstop, lazy read) | `{fork_id, version_number}` (201) | TOPO-FSTATE-1, TOPO-FVER-1, TOPO-FORK-2 to TOPO-FORK-8 |
| GET | `/internal/forks?skip&limit` | `X-Internal-Token` | reservations (archive reconcile, wiring heal) | `{reservation_ids, forks, total, skip, limit}` | TOPO-LISTF-1 |
| GET | `/internal/forks/by-device/{device_id}` | `X-Internal-Token` | inventory (device delete guard) | `{reservation_ids, connection_count, connection_ids}` | TOPO-BYDEV-1, TOPO-BYDEV-2 |
| POST | `/internal/forks/devices/batch` | `X-Internal-Token` | reservations (utilization report) | `{devices: {reservation_id: [device_id]}}` | TOPO-DEVB-1 |
| GET | `/internal/forks/{reservation_id}` | `X-Internal-Token` | reservations (fork read), execution (intended wiring), ai-orchestrator (purpose signals) | fork detail | TOPO-FORK-9, TOPO-FORK-25 |
| GET | `/internal/forks/{reservation_id}/versions/{version_id}` | `X-Internal-Token` | reservations | one fork version with its canvas | TOPO-FORK-10 |
| POST | `/internal/forks/{reservation_id}/versions/{version_id}/restore` | `X-Internal-Token` | reservations | `{id, valid, invalid_edges, draft_restored_from_id}` | TOPO-FORK-12, TOPO-MARKER-1, TOPO-FVER-3, TOPO-FSTATE-3 |
| PUT | `/internal/forks/{reservation_id}/canvas` | `X-Internal-Token` | reservations | `{id, valid, invalid_edges}` | TOPO-FORK-11, TOPO-FVER-3, TOPO-FSTATE-3 |
| POST | `/internal/forks/{reservation_id}/save` | `X-Internal-Token` | reservations | `ForkSaveResponse` | TOPO-FORK-13 to TOPO-FORK-24, TOPO-CLAIM-1 to TOPO-CLAIM-4, TOPO-FVER-2, TOPO-MARKER-2, TOPO-FSTATE-4 |
| POST | `/internal/forks/{reservation_id}/prune-devices` | `X-Internal-Token` | reservations (device removal) | `{fork_id, version_number, changed, released}` | TOPO-PRUNE-1 to TOPO-PRUNE-4, TOPO-FVER-4 |
| POST | `/internal/forks/{reservation_id}/archive` | `X-Internal-Token` | reservations (terminal transitions, sweep) | `{fork_id, reservation_id, status}` or 204 | TOPO-FSTATE-2 |

## 8. Features

### 8.1 Physical connections

**What it does.** An admin records a cable between two device ports, singly or up to 200
at once; anyone signed in can list and read cables, a non-admin only those touching
devices they can see. These records are what pathfinding, validation, and fork wiring
run on.

**Surfaces.** User interface `frontend/src/pages/admin/ConnectionsPage.tsx` (section
8.17); routes `GET /connections`, `GET /connections/{id}`, `POST /connections`,
`POST /connections/bulk`, `DELETE /connections/{id}`.

**Rules.**

- **TOPO-CONN-1.** Creating a connection is admin or superadmin only and answers 201
  with the row. \
  Enforced in: `services/cabling/app/routes/connections.py` (`create_connection_endpoint`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_create_connection`, `test_user_cannot_create`)
- **TOPO-CONN-2.** Port names are 1 to 255 characters, `notes` at most 2000, and
  `connection_type` at most 50 with default `ethernet`; the type is not checked against a
  vocabulary (issue #130). \
  Enforced in: `services/cabling/app/schemas/connection.py` (`ConnectionCreate`) \
  Pinned by: `services/cabling/tests/test_schema_bounds.py` (`test_connection_port_empty_rejected`, `test_connection_port_at_cap_accepted`, `test_connection_port_over_cap_rejected`, `test_connection_notes_over_cap_rejected`)
- **TOPO-CONN-3.** A connection from a port to the same port of the same device is 422
  `Cannot connect a port to itself`; two different ports of one device (a loopback) are
  allowed. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`_validate_connection_row`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_create_connection_same_port_self_loop`, `test_create_connection_self_loop`)
- **TOPO-CONN-4.** Cabling does not check that a port exists in inventory, and does not
  refuse an exact duplicate, a reverse duplicate, or a second cable on a port that
  already has one. By decision (the docstring of `create_connections_bulk` and the "warn,
  never block" comment in `MultiConnectDialog.tsx`). \
  Enforced in: `services/cabling/app/services/connection_service.py` (`create_connection`, `create_connections_bulk`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_create_duplicate_connection`, `test_create_reverse_duplicate_connection`); `services/cabling/tests/test_connections_bulk.py` (`test_bulk_duplicates_all_created_none_rejected`)
- **TOPO-CONN-5.** `created_by` is the JWT `username` claim, or `unknown` when absent. \
  Enforced in: `services/cabling/app/routes/connections.py` (`create_connection_endpoint`, `create_connections_bulk_endpoint`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_create_connection`)
- **TOPO-CONN-6.** The list is unfiltered for an admin, with no inventory call. For a
  non-admin it holds only connections with at least one endpoint in the caller's visible
  device set, applied in SQL so `total` is the filtered count; an empty visible set
  answers an empty page without a query. \
  Enforced in: `services/cabling/app/routes/connections.py` (`list_connections_endpoint`); `services/cabling/app/services/connection_service.py` (`list_connections`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_non_admin_sees_only_connections_touching_visible_device`, `test_non_admin_visible_set_total_matches_filtered_count`, `test_admin_list_connections_never_calls_inventory`, `test_non_admin_empty_visible_set_returns_empty_page`)
- **TOPO-CONN-7.** When the visibility lookup fails for a non-admin, the list answers 503
  and returns nothing (fail closed). \
  Enforced in: `services/cabling/app/routes/connections.py` (`list_connections_endpoint`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_non_admin_list_connections_inventory_unreachable_503`); `services/cabling/tests/test_visibility_oracle.py` (`test_list_connections_non_admin_fails_closed_when_visibility_unavailable`)
- **TOPO-CONN-8.** `device_id` filters to connections naming the device on either side;
  the list is ordered newest first. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`list_connections`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_filter_connections_by_device_id`, `test_list_connections_pagination`)
- **TOPO-CONN-9.** `GET /connections/{id}` follows the list's visibility rule
  (TOPO-CONN-7): an admin reads any row; a non-admin reads the full row when at least one
  end is visible, and a row with no visible end answers the same 404 `Connection not
  found` an unknown id gets (issue #1008). Visibility is resolved first, so an
  unanswerable lookup is 503 `Could not verify device visibility; the connection was
  not returned. Retry the request.` for every id. \
  Enforced in: `services/cabling/app/routes/connections.py` (`get_connection_endpoint`); `services/cabling/app/services/visible_devices.py` (`resolve_caller_visibility`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_user_can_get_connection`, `test_get_connection_not_found`, `test_user_get_connection_with_no_visible_end_is_404`, `test_user_get_connection_visibility_unavailable_is_503`, `test_admin_get_connection_never_calls_inventory`)
- **TOPO-CONN-10.** Deleting a connection is admin only; an unknown id is 404. \
  Enforced in: `services/cabling/app/routes/connections.py` (`delete_connection_endpoint`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_delete_connection`, `test_user_cannot_delete`, `test_delete_connection_not_found`)
- **TOPO-CONN-11.** Deleting a connection consults no fork and no reservation: a cable a
  live fork's wiring uses can be deleted, and the fork's rows keep its id as a bare
  `physical_connection_id`. By decision: ADR 0007 (Decision 5) accepts a graph change
  under a live reservation, with a re-save as the recovery. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`delete_connection`) \
  Pinned by: none
- **TOPO-CONN-12.** A bulk request carries 1 to 200 items; anything else, or a malformed
  item, is 422 for the whole request. \
  Enforced in: `services/cabling/app/schemas/connection.py` (`ConnectionBulkCreate`) \
  Pinned by: `services/cabling/tests/test_connections_bulk.py` (`test_bulk_cap_enforcement_over_200_rejected`, `test_bulk_empty_items_rejected`, `test_bulk_at_cap_200_accepted`, `test_bulk_malformed_row_rejected_at_schema_level`)
- **TOPO-CONN-13.** Bulk applies the single-create row rules to every row; a rejected
  row never blocks the others, and the answer is always 200 with `created`, `rejected`,
  and one `rows` entry per item in request order (`index`, `status`, `connection_id`,
  `error`). \
  Enforced in: `services/cabling/app/services/connection_service.py` (`create_connections_bulk`, `_validate_connection_row`) \
  Pinned by: `services/cabling/tests/test_connections_bulk.py` (`test_bulk_happy_path_all_created`, `test_bulk_mixed_batch_indexes_line_up`, `test_bulk_self_loop_rejected_siblings_created`)
- **TOPO-CONN-14.** All accepted bulk rows are inserted in one commit; a batch with no
  accepted row commits nothing. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`create_connections_bulk`) \
  Pinned by: `services/cabling/tests/test_connections_bulk.py` (`test_bulk_single_commit_not_per_row`, `test_bulk_all_rejected_no_commit`)
- **TOPO-CONNINT-1.** `GET /connections/internal` takes the same parameters as the
  user-facing list and answers unfiltered. \
  Enforced in: `services/cabling/app/routes/connections.py` (`list_connections_internal`) \
  Pinned by: `services/cabling/tests/test_connections.py` (`test_internal_list_connections_valid_token`, `test_internal_list_connections_invalid_token_403`, `test_internal_list_connections_missing_token`, `test_internal_list_connections_device_filter`)

**Out of scope.** Ports, devices, and device groups themselves are inventory's
(`inventory.md`). Inventory's device delete guard reads this area's by-device route
(TOPO-BYDEV-1) and is specified there.

### 8.2 The device-group boundary on cabling

**What it does.** With the boundary on (the default), an admin cannot cable two devices
that belong to different labs, meaning device-group sets that share no group. A device
in no group can be cabled to anything.

**Surfaces.** Routes `POST /connections` and `POST /connections/bulk`; inventory
`GET /device-groups/device/{id}` with the caller's JWT (section 10); setting
`ENFORCE_DEVICE_GROUP_BOUNDARIES`.

**Rules.**

- **TOPO-BOUND-1.** With the boundary on, a connection whose two devices both have
  groups and share none is 422 `Devices belong to different device groups and share none;
  cross-group cabling is disabled.`; an ungrouped device on either side, or one shared
  group, allows it. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`_enforce_device_group_boundary`) \
  Pinned by: `services/cabling/tests/test_service_unit.py` (`test_rejects_cross_group_connection`, `test_allows_shared_group_connection`, `test_allows_when_either_device_ungrouped`)
- **TOPO-BOUND-2.** Membership is read from inventory with the caller's own JWT (5 s); a
  404 for either device refuses the row with 422 `Device <id> does not exist`. The
  existence check runs only while the boundary is on. \
  Enforced in: `services/cabling/app/services/device_group_guard.py` (`fetch_device_group_ids`, `DeviceNotFoundError`); `services/cabling/app/services/connection_service.py` (`_enforce_device_group_boundary`) \
  Pinned by: `services/cabling/tests/test_service_unit.py` (`test_rejects_connection_when_device_does_not_exist`); `services/cabling/tests/test_route_handlers_direct.py` (`test_device_group_guard_404_raises_device_not_found`, `test_device_group_guard_returns_group_ids`)
- **TOPO-BOUND-3.** On single create, membership that cannot be read (transport error, or
  a status of 400 or above other than 404) lets the connection through and logs
  `device_group_boundary_unverified` (fail open). By decision (the docstring of
  `_enforce_device_group_boundary`, issue #392). \
  Enforced in: `services/cabling/app/services/device_group_guard.py` (`fetch_device_group_ids`); `services/cabling/app/services/connection_service.py` (`_enforce_device_group_boundary`) \
  Pinned by: `services/cabling/tests/test_service_unit.py` (`test_allows_when_inventory_unavailable_fail_open`); `services/cabling/tests/test_route_handlers_direct.py` (`test_device_group_guard_unreachable_returns_none`, `test_device_group_guard_bad_response_returns_none`)
- **TOPO-BOUND-4.** On bulk create, membership that cannot be read for any device in the
  batch refuses the whole request with 503 and creates nothing (fail closed). By decision
  (the docstring of `_resolve_group_cache_for_batch`). \
  Enforced in: `services/cabling/app/services/connection_service.py` (`create_connections_bulk`, `_resolve_group_cache_for_batch`) \
  Pinned by: `services/cabling/tests/test_connections_bulk.py` (`test_bulk_unverifiable_device_fails_closed_creates_nothing`, `test_bulk_unverifiable_device_503_creates_nothing_http`)
- **TOPO-BOUND-5.** Bulk reads each distinct device once, concurrently, before any row is
  judged; a device inventory answers 404 for rejects only the rows that name it. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`_resolve_group_cache_for_batch`, `_cached_fetch_group_ids`) \
  Pinned by: `services/cabling/tests/test_connections_bulk.py` (`test_bulk_resolves_group_ids_once_per_distinct_device_id`, `test_bulk_memoizes_group_lookups_across_batch`, `test_bulk_confirmed_not_found_rejects_only_its_rows_siblings_created`, `test_bulk_cached_device_not_found_rejects_every_row`)
- **TOPO-BOUND-6.** With the boundary off, no inventory call is made and neither the
  boundary nor device existence is checked. \
  Enforced in: `services/cabling/app/services/connection_service.py` (`_enforce_device_group_boundary`, `create_connections_bulk`) \
  Pinned by: `services/cabling/tests/test_service_unit.py` (`test_disabled_enforcement_skips_membership_fetch`)

**Out of scope.** Device-group membership itself is inventory's (`inventory.md`).

### 8.3 Pathfinding

**What it does.** Given two devices, HERD lists every shortest chain of cables between
them, including the ports each cable uses. A non-admin cannot ask about a device they
cannot see, and a hop through such a device comes back blanked.

**Surfaces.** Routes `POST /pathfind` and `POST /pathfind/batch`; the editor's edge
status (section 8.15) and ai-orchestrator (`ai-features.md`) call them.

**Rules.**

- **TOPO-PATH-1.** Pathfind answers every shortest-hop path between the two devices, each
  hop carrying `device_id`, `port_in`, and `port_out`; `hop_count` is the number of
  devices on a path, endpoints included. With no path it answers `reachable` false,
  `hop_count` 0, `paths` empty. \
  Enforced in: `services/cabling/app/routes/pathfind.py` (`pathfind_endpoint`); `services/cabling/app/services/pathfind_service.py` (`find_all_shortest_paths`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_direct_connection`, `test_linear_chain`, `test_no_path`, `test_shortest_path_wins`, `test_path_includes_ports`)
- **TOPO-PATH-2.** The cabling graph is undirected: a cable recorded A to B is walkable
  both ways. \
  Enforced in: `services/cabling/app/services/pathfind_service.py` (`build_adjacency_graph`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_bidirectional`, `test_cycle_does_not_loop`)
- **TOPO-PATH-3.** A device paired with itself answers one path of one hop. \
  Enforced in: `services/cabling/app/services/pathfind_service.py` (`find_all_shortest_paths`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_same_device`)
- **TOPO-PATH-4.** Paths through the same sequence of intermediate devices collapse to
  one (parallel cables are one route); distinct sequences are separate paths, at most 256. \
  Enforced in: `services/cabling/app/services/pathfind_service.py` (`find_all_shortest_paths`, `_enumerate_paths`, `MAX_ENUMERATED_PATHS`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_parallel_cables_through_same_intermediate_collapse`, `test_parallel_paths_via_distinct_intermediates`, `test_path_count_cap_truncates_on_dense_mesh`, `test_path_count_cap_does_not_truncate_collapsed_parallel_cables`)
- **TOPO-PATH-5.** The graph is loaded only for the connected components of the
  requested devices, and that includes intermediate devices that are not themselves
  requested, so scoping never loses a path. \
  Enforced in: `services/cabling/app/services/pathfind_service.py` (`build_adjacency_graph`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_scoped_graph_loads_only_in_scope_component`, `test_scoped_graph_includes_off_scope_intermediates`, `test_scoped_results_match_unscoped`)
- **TOPO-PATH-6.** In-process callers (validation, fork wiring) can require a path to
  leave the source on one port and reach the target on one port; the filter applies
  during the search, never falls back to an unconstrained answer, can pick a longer path,
  and is ignored when both devices are the same. The HTTP routes take no port. \
  Enforced in: `services/cabling/app/services/pathfind_service.py` (`find_all_shortest_paths`, `find_all_shortest_paths_async`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_port_constraint_selects_between_two_direct_cables`, `test_port_constraint_no_match_on_source_returns_empty`, `test_port_constraint_no_match_on_target_returns_empty`, `test_port_constraint_can_select_longer_than_unconstrained_shortest`, `test_port_constraint_same_device_ignores_ports`)
- **TOPO-PATH-7.** Batch takes up to 2000 pairs (more is 422), builds the graph once,
  and answers one result per pair in request order, each the single route's shape plus
  the echoed pair; an empty list answers an empty list. \
  Enforced in: `services/cabling/app/routes/pathfind.py` (`pathfind_batch_endpoint`); `services/cabling/app/schemas/pathfind.py` (`MAX_BATCH_PAIRS`, `PathfindBatchRequest`) \
  Pinned by: `services/cabling/tests/test_pathfind.py` (`test_batch_multiple_pairs_single_graph_build`, `test_batch_preserves_request_order_and_echoes_pairs`, `test_batch_result_matches_single_endpoint_shape`, `test_batch_over_cap_returns_422`, `test_batch_at_cap_accepted`, `test_batch_empty_pairs_returns_empty_results`)
- **TOPO-PATH-8.** For a non-admin, a request whose source or target is outside the
  caller's visible devices is 404 `Device not found`, the same answer an unknown device
  gets, and nothing is resolved. \
  Enforced in: `services/cabling/app/routes/pathfind.py` (`pathfind_endpoint`, `DEVICE_NOT_FOUND`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_pathfind_non_admin_refuses_hidden_endpoint`, `test_pathfind_non_admin_refuses_unknown_device_identically`)
- **TOPO-PATH-9.** For a non-admin, every hop through a device outside the visible set
  comes back with `device_id` null, `hidden` true, and no port names, in its position, so
  `hop_count` and reachability equal an admin's answer. In-process callers always get
  whole hops. \
  Enforced in: `services/cabling/app/routes/pathfind.py` (`_redact_paths`); `services/cabling/app/schemas/pathfind.py` (`PathHop`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_pathfind_non_admin_redacts_hidden_transit_hop`, `test_pathfind_admin_sees_the_transit_hop`)
- **TOPO-PATH-10.** In a batch, a non-admin's pair naming a hidden device is answered in
  place as unreachable with `error` `Device not found`, its devices never seed the graph,
  and the other pairs resolve; a genuinely unreachable pair has `error` null. \
  Enforced in: `services/cabling/app/routes/pathfind.py` (`pathfind_batch_endpoint`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_batch_non_admin_refuses_hidden_pair_and_resolves_the_rest`, `test_batch_unreachable_pair_stays_distinguishable_from_a_refused_one`, `test_batch_admin_resolves_the_hidden_pair`)
- **TOPO-PATH-11.** When the visibility lookup fails for a non-admin, both routes answer
  503 and resolve nothing (fail closed). \
  Enforced in: `services/cabling/app/routes/pathfind.py` (`_VISIBILITY_UNAVAILABLE`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_pathfind_non_admin_fails_closed_when_visibility_unavailable`, `test_batch_non_admin_fails_closed_when_visibility_unavailable`)
- **TOPO-VIS-1.** One helper decides visibility for the connection list, validate,
  pathfind, and import: an admin or superadmin gets no filter and causes no inventory
  call; anyone else gets the set inventory's `GET /device-groups/visible-devices` returns
  for their own id with their own JWT (5 s); a transport error or a non-200 is a 503 with
  the route's own wording, and a missing `Authorization` header is a 500. \
  Enforced in: `services/cabling/app/services/visible_devices.py` (`resolve_caller_visibility`, `fetch_visible_device_ids`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_validate_admin_never_calls_the_visibility_lookup`, `test_list_connections_admin_never_calls_the_visibility_lookup`); `services/cabling/tests/test_connections.py` (`test_non_admin_list_connections_missing_authorization_header`)

**Out of scope.** Which device groups a user sees is inventory's (`inventory.md`).

### 8.4 Listing and creating topologies

**What it does.** Everyone signed in sees every saved topology, can search by name,
filter to their own, and sort by name, owner, created, or updated. Anyone can create an
empty topology and draw on it.

**Surfaces.** User interface `frontend/src/pages/TopologyPage.tsx` (section 8.14);
routes `GET /topologies`, `POST /topologies`, `GET /topologies/{id}`.

**Rules.**

- **TOPO-LIST-1.** Every role reads every topology; the list filters only narrow it. By
  decision ([ROLES.md](../ROLES.md), Get a topology, issue #763). \
  Enforced in: `services/cabling/app/routes/topologies.py` (`list_topologies`, `get_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_other_user_can_read_topology`); `services/cabling/tests/test_topology_list_controls.py` (`test_owner_all_equals_the_default`)
- **TOPO-LIST-2.** `search` (at most 255 characters) is a case-insensitive substring of
  the name with `%` and `_` literal and surrounding whitespace ignored; blank means no
  filter. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`list_topologies`) \
  Pinned by: `services/cabling/tests/test_topology_list_controls.py` (`test_search_is_case_insensitive_substring_and_total_is_filtered`, `test_search_treats_like_wildcards_literally`, `test_search_ignores_surrounding_whitespace`, `test_blank_search_is_no_filter`, `test_search_at_the_length_cap_is_accepted`)
- **TOPO-LIST-3.** `owner=mine` keeps topologies whose `created_by` is the caller's JWT
  `sub`, for admins too; `all` is the default. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`list_topologies`, `TopologyOwnerFilter`) \
  Pinned by: `services/cabling/tests/test_topology_list_controls.py` (`test_owner_mine_returns_only_the_callers_topologies`, `test_owner_mine_for_an_admin_is_the_admins_own`, `test_owner_mine_with_nothing_owned_is_empty`)
- **TOPO-LIST-4.** `sort_by` is one of `name`, `owner_name`, `created_at`, `updated_at`
  (default `updated_at`) and `sort_dir` one of `asc`, `desc` (default `desc`); an unknown
  `owner`, `sort_by`, or `sort_dir` is 422. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`TopologySortField`, `TopologySortDir`) \
  Pinned by: `services/cabling/tests/test_topology_list_controls.py` (`test_default_order_is_updated_at_desc`, `test_each_sort_field_ascending`, `test_each_sort_field_descending`, `test_bad_values_are_422`)
- **TOPO-LIST-5.** Text fields sort by `lower(coalesce(column, ''))` with the byte-order
  `C` collation on Postgres only, so a missing owner sorts as empty and both databases
  agree; every ordering ends with id ascending. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`_topology_order_by`) \
  Pinned by: `services/cabling/tests/test_topology_list_controls.py` (`test_null_owner_name_sorts_as_empty`, `test_full_tie_falls_back_to_id_ascending`, `test_tiebreak_keeps_pages_disjoint_and_complete`, `test_order_by_uses_byte_order_collation_on_postgres_only`); `services/cabling/tests/test_topology_list_order_live_pg.py` (`test_name_order_is_case_folded_byte_order_on_postgres`, `test_owner_name_order_places_null_as_empty_on_postgres`)
- **TOPO-LIST-6.** `total` counts the filtered set; `limit` is 1 to 500 (default 50). \
  Enforced in: `services/cabling/app/routes/topologies.py` (`list_topologies`) \
  Pinned by: `services/cabling/tests/test_topology_list_controls.py` (`test_total_is_filtered_not_the_page_size`, `test_search_owner_sort_and_page_combined`)
- **TOPO-CRUD-1.** Any signed-in user creates a topology from a name of 1 to 100
  characters; `created_by` is the JWT `sub`, `owner_name` the `username` claim (empty
  when absent), and the canvas starts null. The body carries no canvas. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`create_topology`); `services/cabling/app/schemas/topology.py` (`TopologyCreate`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_create_topology`, `test_create_topology_empty_name`, `test_owner_name_in_list`); `services/cabling/tests/test_schema_bounds.py` (`test_topology_name_at_cap_accepted`, `test_topology_name_over_cap_rejected`)
- **TOPO-CRUD-2.** `GET /topologies/{id}` answers any signed-in user with the full
  canvas, reduced by the device node allowlist (TOPO-STRIP-3); an unknown id is 404. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`get_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_get_topology`, `test_get_topology_not_found`, `test_other_user_can_read_topology`)

**Out of scope.** Topology names are not unique. AI-generated topologies are created by
ai-orchestrator (`ai-features.md`).

### 8.5 Editing a topology and the reservation edit lock

**What it does.** The creator or an admin saves a new name or canvas. While a reservation
someone else holds still references the topology, its drawing cannot be changed by
anyone but that reservation's owner or an admin.

**Surfaces.** User interface `frontend/src/pages/TopologyEditorPage.tsx` (Save); route
`PUT /topologies/{id}`; reservations `GET /internal/by-topology/{topology_id}`
(`reservations.md`, RES-INTERNAL-5).

**Rules.**

- **TOPO-EDIT-1.** Only the creator, an admin, or a superadmin may update; anyone else
  gets 403 `Not authorized to update this topology`, after the 404 for an unknown id. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_update_topology_by_creator`, `test_update_topology_by_admin`, `test_update_topology_by_superadmin`, `test_update_topology_forbidden_for_other_user`, `test_update_topology_not_found`)
- **TOPO-EDIT-2.** `name` (1 to 100), `canvas_data`, and `description` (at most 2000)
  are optional; a missing canvas keeps the stored one, and `description` is written only
  to the version a canvas change appends. \
  Enforced in: `services/cabling/app/schemas/topology.py` (`TopologyUpdate`); `services/cabling/app/routes/topologies.py` (`update_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_update_topology_null_canvas_preserves_existing`, `test_update_topology_name_only`, `test_update_topology_canvas_only`); `services/cabling/tests/test_schema_bounds.py` (`test_topology_update_name_empty_rejected`, `test_topology_description_over_cap_rejected`)
- **TOPO-EDIT-3.** Every update sets `modified_by` to the caller and keeps `owner_name`. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_topology_modified_by_set_on_update`, `test_owner_name_preserved_on_update`)
- **TOPO-EDIT-4.** When the canvas changes and the caller is not an admin, cabling asks
  reservations for every reservation on the topology; any `PENDING`,
  `PENDING_PROVISION`, or `ACTIVE` one owned by another user refuses the update with 409
  `{message, reservations: [{id, status, end_time}]}`. The caller's own reservations
  never block. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`); `services/cabling/app/services/reservation_guard.py` (`find_blocking_reservations`, `_BLOCKING_STATUSES`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_update_topology_canvas_blocked_by_other_users_reservation`, `test_update_topology_canvas_allowed_for_reservation_owner`); `services/cabling/tests/test_route_handlers_direct.py` (`test_reservation_guard_filters_blocking_status`)
- **TOPO-EDIT-5.** Admins skip the lock, and a name-only or description-only update never
  consults it. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_update_topology_canvas_admin_bypasses_reservation_lock`, `test_update_topology_name_only_skips_reservation_lock`)
- **TOPO-EDIT-6.** The lock fails open: a transport error or a status of 400 or above
  from reservations means no blocking reservation, and rows naming another topology are
  ignored. By decision (the docstring of `find_blocking_reservations`; [ROLES.md](../ROLES.md),
  Delete a topology). \
  Enforced in: `services/cabling/app/services/reservation_guard.py` (`find_blocking_reservations`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_reservation_guard_unreachable_fails_open`, `test_reservation_guard_bad_response_returns_empty`); `services/cabling/tests/test_topology_delete_guard.py` (`test_edit_lock_lookup_still_fails_open_on_the_same_answers`, `test_edit_lock_lookup_still_skips_other_topology_rows`)

**Out of scope.** A live reservation's own wiring is edited through its fork (section
8.12), not the parent topology; the fork pins the parent version at activation.

### 8.6 Cloning and deleting a topology

**What it does.** Anyone can copy any topology under a new name and own the copy. The
creator or an admin can delete a topology, but not while a reservation that has not
finished still references it.

**Surfaces.** User interface `frontend/src/pages/TopologyPage.tsx` (Clone, Delete, bulk
Delete); routes `POST /topologies/{id}/clone` and `DELETE /topologies/{id}`.

**Rules.**

- **TOPO-CLONE-1.** Any signed-in user may clone any topology; the clone takes the new
  name (1 to 100), belongs to the caller, carries an independent deep copy of the
  stripped canvas, and gets version 1 (TOPO-TVER-2). \
  Enforced in: `services/cabling/app/routes/topologies.py` (`clone_topology`); `services/cabling/app/schemas/topology.py` (`TopologyClone`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_clone_topology_happy_path`, `test_clone_topology_independent_canvas`, `test_clone_topology_null_canvas`, `test_clone_topology_not_found`, `test_clone_topology_missing_name`)
- **TOPO-DEL-1.** Only the creator or an admin may delete; an unknown id is 404 and a
  non-owner is 403, both answered before reservations is asked. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`delete_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_delete_topology`, `test_delete_topology_by_admin`, `test_delete_topology_forbidden_for_other_user`); `services/cabling/tests/test_topology_delete_guard.py` (`test_unknown_topology_is_404_without_asking_reservations`, `test_non_owner_is_403_without_asking_reservations`)
- **TOPO-DEL-2.** While any `PENDING`, `PENDING_PROVISION`, or `ACTIVE` reservation
  references the topology, delete is refused with 409
  `{"error": "topology_in_use", "reservation_ids": [...]}`, ids sorted and distinct;
  `COMPLETED`, `CANCELLED`, and `FAILED` reservations do not block. There is no force
  flag. \
  Enforced in: `services/cabling/app/services/reservation_guard.py` (`assert_topology_deletable`, `find_blocking_reservations_strict`) \
  Pinned by: `services/cabling/tests/test_topology_delete_guard.py` (`test_live_reservation_blocks_delete_with_its_id`, `test_terminal_reservation_does_not_block`, `test_no_reservation_deletes`, `test_mixed_rows_report_only_live_ids_sorted_and_deduped`, `test_http_409_body_shape`)
- **TOPO-DEL-3.** The delete guard fails closed: a transport error, a non-200, a body that
  is not a list of objects with string `id` and `status`, an unknown status, or a row
  naming another topology answers 503 `Could not verify topology is not in use` and
  nothing is deleted. \
  Enforced in: `services/cabling/app/services/reservation_guard.py` (`find_blocking_reservations_strict`, `_KNOWN_STATUSES`, `TOPOLOGY_DELETE_UNVERIFIABLE_DETAIL`) \
  Pinned by: `services/cabling/tests/test_topology_delete_guard.py` (`test_unverifiable_answer_is_503_and_nothing_deleted`)

**Out of scope.** Deleting a topology does not touch any fork: a fork keeps its own copy
of the canvas and its pin to a version id.

### 8.7 Topology versions, diff, and restore

**What it does.** Every saved drawing change becomes a numbered version. Anyone can list
versions, open one, and compare two; the creator or an admin can roll the topology back
to an older version, which itself becomes a new version.

**Surfaces.** User interface `frontend/src/components/topology-editor/HistoryPanel.tsx`
and `frontend/src/components/topology-editor/VersionDiffDialog.tsx`; the four
`/topologies/{id}/versions` routes in section 5. Version numbering is TOPO-TVER-1 to
TOPO-TVER-5 (section 4).

**Rules.**

- **TOPO-VER-1.** Any signed-in user lists a topology's versions, newest number first,
  without canvases; an unknown topology is 404. \
  Enforced in: `services/cabling/app/routes/versions.py` (`list_versions`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_list_omits_canvas_data_detail_includes_it`, `test_versions_list_pagination`, `test_version_on_missing_topology`)
- **TOPO-VER-2.** A version is read only through its own topology; a version of another
  topology is 404 `Version not found`. \
  Enforced in: `services/cabling/app/routes/versions.py` (`_load_version`, `get_version`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_version_detail_wrong_topology`); `services/cabling/tests/test_route_handlers_direct.py` (`test_versions_get_handler_and_wrong_topology`)
- **TOPO-VER-3.** Diff compares the nodes and the edges of versions `a` and `b` by `id`:
  `added`, `removed`, and `modified` (with `before` and `after`) for each; an entry that
  is not an object or has no `id` is ignored. \
  Enforced in: `services/cabling/app/services/version_diff.py` (`diff_canvas`, `diff_collection`, `_index_by_id`); `services/cabling/app/routes/versions.py` (`diff_versions`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_diff_detects_add_remove_modify`, `test_diff_identical_versions_empty`); `services/cabling/tests/test_route_handlers_direct.py` (`test_version_diff_index_skips_non_dict_and_missing_id`, `test_version_diff_reports_modified_edge`)
- **TOPO-VER-4.** Restore is creator or admin (403 `Not authorized to modify this
  topology` otherwise); it copies the version's stripped canvas onto the topology, also
  its name when `restore_name` is true, and sets `modified_by`. \
  Enforced in: `services/cabling/app/routes/versions.py` (`restore_version`, `_require_mutator`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_restore_applies_snapshot_and_creates_new_version`, `test_non_creator_can_read_but_not_restore`, `test_admin_can_restore`, `test_restore_with_description_and_restore_name`)
- **TOPO-VER-5.** Restore is refused with 409 `{message, reservations}` while any
  `PENDING`, `PENDING_PROVISION`, or `ACTIVE` reservation references the topology,
  including the caller's own and for admins; the lookup fails open. By decision
  ([USER_GUIDE.md](../USER_GUIDE.md) and [TOPOLOGY_EDITOR.md](../TOPOLOGY_EDITOR.md):
  restore is blocked while a reservation still references the topology). \
  Enforced in: `services/cabling/app/routes/versions.py` (`restore_version`); `services/cabling/app/services/reservation_guard.py` (`find_blocking_reservations`) \
  Pinned by: `services/cabling/tests/test_topology_versions.py` (`test_restore_blocked_by_active_reservation`); `services/cabling/tests/test_route_handlers_direct.py` (`test_versions_restore_blocked_by_active_reservation`)

**Out of scope.** Fork versions are section 8.12.

### 8.8 The device node allowlist

**What it does.** A device's record can hold credentials. Whatever a caller sends, a
device node in a stored or returned canvas keeps only the few device fields the editor
needs.

**Surfaces.** Every canvas write and read in cabling; the frontend's
`frontend/src/lib/canvasNodes.ts`; cabling migration
`services/cabling/migrations/versions/0013_scrub_device_node_field_data.py`.

**Rules.**

- **TOPO-STRIP-1.** A node is a device node when `data.device` is an object, whatever its
  `type`; its `data.device` keeps only `id`, `name`, `topology_type`, `connection_type`,
  `status`, `template_name`, `template_icon`, and `role`. Other nodes, edges, and other
  canvas keys pass through; the input is never mutated and a second pass changes nothing. \
  Enforced in: `services/cabling/app/services/canvas_nodes.py` (`strip_device_nodes`, `DEVICE_NODE_ALLOWED_KEYS`) \
  Pinned by: `services/cabling/tests/test_canvas_nodes_strip_device_nodes.py` (`test_strips_field_data_and_non_allowlisted_keys`, `test_keeps_allowlisted_keys_byte_for_byte`, `test_other_node_types_and_edges_untouched`, `test_does_not_mutate_input`, `test_idempotent`, `test_legacy_node_with_no_type_tag_still_stripped`, `test_role_only_template_device_dict_survives`)
- **TOPO-STRIP-2.** The allowlist is applied before storing at every canvas write:
  topology PUT, clone, version restore, import, template create, update, from-topology,
  and instantiate, and fork create, canvas PUT, save, and restore. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`, `clone_topology`); `services/cabling/app/routes/versions.py` (`restore_version`); `services/cabling/app/services/bulk_service.py` (`import_topologies`); `services/cabling/app/routes/templates.py` (`create_template`, `update_template`, `create_template_from_topology`, `instantiate_template`); `services/cabling/app/services/fork_service.py` (`create_fork`); `services/cabling/app/routes/forks.py` (`update_fork_canvas_internal`, `save_fork_internal`, `restore_fork_version_internal`) \
  Pinned by: `services/cabling/tests/test_device_node_field_data_scrub_http.py` (`test_topology_put_scrubs_field_data`, `test_topology_clone_scrubs_field_data`, `test_topology_import_json_scrubs_field_data`, `test_topology_restore_version_scrubs_field_data`, `test_fork_canvas_put_scrubs_field_data`, `test_fork_save_scrubs_field_data`, `test_fork_create_scrubs_a_dirty_parent_topology_canvas`, `test_template_create_and_instantiate_scrub_field_data`)
- **TOPO-STRIP-3.** The allowlist is applied again on every canvas read (topology,
  topology version, both sides of a diff, fork, fork version, template, and JSON export)
  to the response only; a stored row that predates the fix is returned clean and is not
  rewritten. CSV export applies no strip but writes only names, ports, and layer. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`get_topology`); `services/cabling/app/routes/versions.py` (`get_version`, `diff_versions`); `services/cabling/app/routes/forks.py` (`get_fork_internal`, `get_fork_version_internal`); `services/cabling/app/routes/templates.py` (`get_template`); `services/cabling/app/services/bulk_service.py` (`topology_to_record`) \
  Pinned by: `services/cabling/tests/test_device_node_field_data_scrub_http.py` (`test_topology_get_strips_a_dirty_row_without_rewriting_it`, `test_topology_version_get_strips_a_dirty_row_without_rewriting_it`, `test_topology_version_diff_strips_dirty_rows_on_both_sides`, `test_fork_get_strips_a_dirty_row_without_rewriting_it`, `test_fork_version_get_strips_a_dirty_row_without_rewriting_it`, `test_template_get_strips_a_dirty_row_without_rewriting_it`, `test_topology_export_json_and_csv_scrub_field_data`)
- **TOPO-STRIP-4.** A topology PUT judges "the canvas changed" on the stripped canvas, so
  a PUT that differs only in a device key outside the allowlist appends no version and
  never consults the edit lock. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`update_topology`) \
  Pinned by: none
- **TOPO-STRIP-5.** Migration 0013 scrubs every stored canvas in the five canvas tables
  with a frozen copy of the allowlist, which a test holds equal to the live set. \
  Enforced in: `services/cabling/migrations/versions/0013_scrub_device_node_field_data.py` (`upgrade`) \
  Pinned by: `services/cabling/tests/test_scrub_device_node_field_data_migration.py` (`test_frozen_allowlist_matches_live_module`, `test_frozen_strip_function_matches_live_behavior_on_fixtures`, `test_scrub_fixture_rows_idempotent`)
- **TOPO-STRIP-6.** The editor sends only `id`, `name`, `topology_type`,
  `connection_type`, `status`, `template_name`, and `template_icon` for a device node,
  while its in-memory store keeps the full device for display. \
  Enforced in: `frontend/src/lib/canvasNodes.ts` (`persistableDevice`, `persistableCanvasNodes`) \
  Pinned by: `frontend/src/test/lib/canvasNodes.test.ts` (`drops field_data and every other non-allowlisted key`, `keeps the allowlisted keys byte-for-byte`, `a PUT payload built from a hydrated store node contains no field_data`)

**Out of scope.** The device record itself is inventory's (`inventory.md`).

### 8.9 Validating a topology against the cabling

**What it does.** HERD checks that every line drawn between two devices has a real cable
path, that every element attachment names a port, and that any routing intent is
valid. The editor shows the answer; a reservation made from the topology is refused
when the answer is not valid (`reservations.md`, RES-TOPO-1 to RES-TOPO-6).

**Surfaces.** Routes `POST /topologies/{id}/validate` and
`POST /topologies/{id}/validate/internal`; the edge pass alone also runs on fork canvas
PUT and restore (TOPO-FORK-11, TOPO-FORK-12).

**Rules.**

- **TOPO-VAL-1.** An edge's endpoints are canvas node ids; a node names a device through
  `data.device.id`, and an id that does not parse as a UUID names no device. \
  Enforced in: `services/cabling/app/services/canvas_nodes.py` (`node_to_device_map`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_validate_skips_malformed_device_uuid`, `test_fork_node_to_device_map_skips_malformed_nodes`)
- **TOPO-VAL-2.** An edge with `data.isProposal` set is not judged. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_skips_proposal_edges`)
- **TOPO-VAL-3.** An edge whose endpoint is neither a device node nor a network element is
  `missing_device`. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_missing_device_reference`)
- **TOPO-VAL-4.** An edge between two device nodes with no path in the cabling graph is
  `no_path`; one with a path is valid. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_reachable_edge`, `test_validate_topology_unreachable_edge`)
- **TOPO-VAL-5.** The edge pass judges a port-constrained edge by the fork save's rule
  (TOPO-FORK-17, TOPO-FORK-18): when the edge names `source_port_name` or
  `target_port_name`, the path must leave and arrive on those ports, with no fallback to
  the device pair, and an edge no such path satisfies is `no_port_path`. Both judges read
  the ports through one helper, so validation never calls valid an edge the save builds
  nothing for (issue #1007). This applies wherever the edge pass runs: both validate
  routes, reservation create, import, and the fork canvas PUT and restore. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`); `services/cabling/app/services/canvas_nodes.py` (`edge_port_constraints`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_validator_and_save_judge_port_constrained_edges_alike`); `services/cabling/tests/test_topologies.py` (`test_validate_topology_reports_uncabled_chosen_ports`)
- **TOPO-VAL-6.** `invalid_edges` lists problems in canvas edge order, each with
  `edge_id`, both device ids when known, the edge's `layer`, and `reason`. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`); `services/cabling/app/schemas/topology.py` (`InvalidEdge`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_multi_edge_mix_preserves_order`)
- **TOPO-VAL-7.** `device_ids` lists the canvas's device node ids, distinct and sorted;
  element and placeholder nodes never appear. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_device_ids_field_lists_canvas_devices`, `test_validate_topology_device_ids_empty_canvas`, `test_validate_topology_device_ids_excludes_element_nodes`)
- **TOPO-VAL-8.** `valid` is true when there is no invalid edge and no routing problem
  other than the informational `l3_duplicate_route`. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`run_full_topology_validation`); `services/cabling/app/services/l3_validation.py` (`route_causes_invalid`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_empty_canvas`); `services/cabling/tests/test_l3_validation.py` (`test_duplicate_route_reported_as_informational_and_does_not_invalidate`)
- **TOPO-VAL-9.** The user-facing validate is creator or admin (403 `Not authorized to
  validate this topology` otherwise), after the 404 for an unknown topology. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`validate_topology`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_forbidden_for_non_owner`, `test_validate_topology_admin_can_validate_any`, `test_validate_topology_not_found`)
- **TOPO-VAL-10.** For a non-admin, device nodes outside the visible set are removed from
  the canvas before validation, so their edges report `missing_device` and their routing
  intent is never judged; admins are judged on the whole canvas. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`validate_topology`); `services/cabling/app/services/canvas_nodes.py` (`redact_invisible_device_nodes`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_validate_non_admin_reports_hidden_node_as_missing_device`, `test_validate_admin_sees_the_real_l3_reasons`)
- **TOPO-VAL-11.** When the visibility lookup fails for a non-admin, validate answers 503
  (fail closed). \
  Enforced in: `services/cabling/app/routes/topologies.py` (`validate_topology`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_validate_non_admin_fails_closed_when_visibility_unavailable`)
- **TOPO-VAL-12.** The internal validate takes no user, applies no visibility filter, and
  runs both passes. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`validate_topology_internal`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_internal_endpoint_with_token`, `test_validate_topology_internal_endpoint_rejects_wrong_token`); `services/cabling/tests/test_route_handlers_direct.py` (`test_topology_validate_internal_not_found`)
- **TOPO-VAL-13.** The routing pass runs only when some node carries a `data.l3` key; a
  canvas with none makes no inventory call. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`run_full_topology_validation`); `services/cabling/app/services/l3_intent.py` (`canvas_has_l3`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_no_l3_data_makes_no_inventory_call`, `test_empty_routes_list_is_no_intent_makes_no_inventory_call`)
- **TOPO-VAL-14.** On the internal validate, `l3=0` skips the routing pass entirely: no
  wiring resolution, no inventory call, and `invalid_routes` empty. \
  Enforced in: `services/cabling/app/routes/topologies.py` (`validate_topology_internal`); `services/cabling/app/services/topology_validation.py` (`run_full_topology_validation`) \
  Pinned by: none

**Out of scope.** What reservations does with the answer is `reservations.md`
(RES-TOPO-1 to RES-TOPO-6, RES-PATCH-8). The AI commit flow's use of the user-facing
validate is `ai-features.md`.

### 8.10 Network elements

**What it does.** A user can drop a VLAN segment, subnet, external cloud, or patch trunk
onto a diagram and attach device ports to it. An attachment is a note on the drawing:
it is never wired.

**Surfaces.** User interface `frontend/src/components/topology-editor/nodes/NetworkElementNode.tsx`
and `frontend/src/components/topology-editor/ElementAttachDialog.tsx` (section 8.15);
the shared classifier in `services/cabling/app/services/canvas_nodes.py`.

**Rules.**

- **TOPO-ELEM-1.** A node of type `networkElementNode` is an element; its element id is
  `data.element.id`, or the node id when that is absent. \
  Enforced in: `services/cabling/app/services/canvas_nodes.py` (`node_to_element_map`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_node_to_element_map_reads_element_id`, `test_node_to_element_map_falls_back_to_node_id_when_element_id_absent`, `test_node_to_element_map_ignores_non_element_nodes`); `services/cabling/tests/test_topologies.py` (`test_validate_topology_element_node_missing_element_id_falls_back_to_node_id`)
- **TOPO-ELEM-2.** An edge between two elements is `element_to_element`. \
  Enforced in: `services/cabling/app/services/canvas_nodes.py` (`classify_element_edge`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_element_to_element`)
- **TOPO-ELEM-3.** An edge between an element and a device node whose device-side port
  name is missing or empty is `element_edge_no_port`; the device-side port is
  `source_port_name` when the device is the source and `target_port_name` when it is the
  target. \
  Enforced in: `services/cabling/app/services/canvas_nodes.py` (`classify_element_edge`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_element_edge_no_port`, `test_validate_topology_element_edge_empty_port`)
- **TOPO-ELEM-4.** An element edge with a device-side port is a valid attachment in
  either direction and is never pathfound; an element edge whose other end is not a
  device node is `missing_device`. \
  Enforced in: `services/cabling/app/services/canvas_nodes.py` (`classify_element_edge`); `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`) \
  Pinned by: `services/cabling/tests/test_topologies.py` (`test_validate_topology_element_attachment_valid_no_bfs`, `test_validate_topology_element_attachment_valid_element_first`, `test_validate_topology_element_edge_unknown_device_side`)
- **TOPO-ELEM-5.** An element edge never becomes a fork connection; fork save and create
  count the valid attachments they skipped in `element_attachments_skipped` and do not
  count invalid element edges. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`resolve_canvas_wiring`, `CanvasWiringResolution`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_resolve_canvas_wiring_element_edge_yields_no_specs_and_reports_count`, `test_resolve_canvas_wiring_element_edge_either_direction_skipped`, `test_resolve_canvas_wiring_element_to_element_not_counted`, `test_resolve_canvas_wiring_element_edge_no_port_not_counted`, `test_save_fork_element_attachment_reports_skip_count_and_builds_nothing`, `test_create_fork_skips_element_attachment_in_parent_canvas`)
- **TOPO-ELEM-6.** Element nodes are not devices for the fork membership check and never
  carry routing intent that is read. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`assert_endpoints_are_members`); `services/cabling/app/services/l3_intent.py` (`walk_l3_nodes`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_element_node_ignored_by_membership_check`); `services/cabling/tests/test_l3_intent.py` (`test_element_node_l3_is_never_read`)

**Out of scope.** Provisioning anchored VLANs from an element is not built (ADR 0012
phase 2). AI-proposed elements are `ai-features.md`.

### 8.11 Layer 3 routing intent in cabling

**What it does.** A user can list routes on a Layer 3 switch in the diagram. Cabling
reads that list, checks its shape, and checks each route against the switch's latest
configuration and against the wiring the diagram actually asks for.

**Surfaces.** `services/cabling/app/services/l3_intent.py` (parsing),
`services/cabling/app/services/l3_validation.py` and
`services/common/herd_common/l3_validation.py` (validation); used by both validate
routes, by fork save (TOPO-FORK-21), and tolerantly by fork create (TOPO-FORK-6); the
editor's Routing panel (section 8.16).

**Rules.**

- **TOPO-L3-1.** `data.l3` must be an object whose only key is `routes`, a list of
  objects with only `destination`, `next_hop`, `interface`, `virtual_router`;
  `destination` and `interface` are required non-empty strings, the other two optional
  strings or null, all at most 64 characters. Anything else is malformed. \
  Enforced in: `services/cabling/app/services/l3_intent.py` (`_parse_node_l3_full`, `_require_string`, `_ALLOWED_ROUTE_KEYS`, `_MAX_FIELD_LENGTH`) \
  Pinned by: `services/cabling/tests/test_l3_intent.py` (`test_malformed_l3_not_an_object`, `test_malformed_l3_extra_top_level_key`, `test_malformed_routes_not_a_list`, `test_malformed_route_unexpected_key`, `test_malformed_route_missing_destination`, `test_malformed_route_empty_destination`, `test_malformed_route_destination_too_long`, `test_malformed_route_interface_wrong_type`, `test_malformed_route_next_hop_null_is_allowed`)
- **TOPO-L3-2.** An empty optional field is stored as null, and a parseable destination
  is rewritten to its canonical network; an unparseable one is kept as written. \
  Enforced in: `services/cabling/app/services/l3_intent.py` (`_require_string`, `_parse_node_l3_full`) \
  Pinned by: `services/cabling/tests/test_l3_intent.py` (`test_next_hop_empty_string_normalizes_to_null`, `test_virtual_router_empty_string_normalizes_to_null`, `test_destination_canonicalized_when_parseable`, `test_destination_kept_verbatim_when_not_parseable`)
- **TOPO-L3-3.** A route's identity is a JSON packing of all four fields, shared with
  execution, so routes differing only in `virtual_router` are distinct. \
  Enforced in: `services/cabling/app/services/l3_intent.py` (`RouteSpec`); `services/common/herd_common/l3_route_identity.py` (`route_identity_key`) \
  Pinned by: `services/cabling/tests/test_l3_intent.py` (`test_route_key_packs_all_four_fields_as_json`, `test_routes_differing_only_by_virtual_router_are_distinct_not_deduped`); `services/cabling/tests/test_l3_route_key_width_live_pg.py` (`test_worst_case_virtual_router_round_trips_through_save_and_internal_get`)
- **TOPO-L3-4.** A duplicate route within one node collapses to its first occurrence and
  is reported as the informational `l3_duplicate_route` at its original index; every
  reported index is the route's original position. \
  Enforced in: `services/cabling/app/services/l3_intent.py` (`_parse_node_l3_full`); `services/cabling/app/services/l3_validation.py` (`validate_canvas_l3`) \
  Pinned by: `services/cabling/tests/test_l3_intent.py` (`test_duplicate_route_key_collapses_to_first_occurrence`, `test_source_index_is_original_position_not_post_collapse`); `services/cabling/tests/test_l3_validation.py` (`test_original_index_survives_a_collapse_for_a_later_bad_route`)
- **TOPO-L3-5.** Only device nodes are read; an empty `routes` list is no intent at all,
  and two nodes naming one device merge their routes, first occurrence winning. \
  Enforced in: `services/cabling/app/services/l3_intent.py` (`walk_l3_nodes`, `merge_candidates_by_device`) \
  Pinned by: `services/cabling/tests/test_l3_intent.py` (`test_parse_l3_intent_empty_routes_list_is_absent_not_empty_list`, `test_parse_l3_intent_merges_routes_across_two_nodes_same_device`, `test_parse_l3_intent_merge_dedupes_on_full_identity_first_wins`, `test_node_with_no_device_id_is_skipped`)
- **TOPO-L3-6.** Validation reports a malformed node as `l3_malformed` with the parser's
  message and keeps judging the other switches; the strict parser used by save raises on
  the first malformed node; the tolerant parser used by fork create drops it with a
  warning naming the node. \
  Enforced in: `services/cabling/app/services/l3_intent.py` (`parse_l3_intent`, `parse_l3_intent_tolerant`); `services/cabling/app/services/l3_validation.py` (`validate_canvas_l3`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_malformed_shape_reports_l3_malformed_and_suppresses_per_route`); `services/cabling/tests/test_l3_intent.py` (`test_parse_l3_intent_raises_on_first_malformed_node_in_canvas_order`, `test_parse_l3_intent_tolerant_drops_malformed_node_keeps_others`, `test_parse_l3_intent_tolerant_logs_warning_naming_the_node`)
- **TOPO-L3-7.** Per switch, the first that applies stops further checks for it:
  `l3_not_a_router` (inventory type is not `Layer 3 Switch`, or inventory did not return
  the device), `l3_switch_unconfigured` (no latest config version, or no usable
  interface name), `l3_switch_unattached` (no resolved hop touches the switch). \
  Enforced in: `services/cabling/app/services/l3_validation.py` (`validate_switch_l3`, `LAYER_3_SWITCH_CONNECTION_TYPE`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_not_a_router_suppresses_per_route_reasons`, `test_not_a_router_when_device_missing_from_batch_response`, `test_unconfigured_when_no_config_version_404`, `test_unconfigured_when_config_has_no_interfaces`, `test_unconfigured_when_interfaces_entries_are_not_dicts`, `test_unattached_when_no_valid_edge_touches_switch`)
- **TOPO-L3-8.** Attachment is judged on the resolved fork wiring of the canvas: port
  constraints are honored, a transit device on a multi-hop path counts as attached, and
  an element attachment does not. \
  Enforced in: `services/cabling/app/services/topology_validation.py` (`run_full_topology_validation`); `services/cabling/app/services/fork_save_service.py` (`touched_devices_from_specs`, `wired_ports_from_specs`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_transit_device_on_multi_hop_path_counts_as_attached`, `test_port_constrained_edge_with_no_cable_leaves_switch_unattached`, `test_unattached_when_only_element_attachment_touches_switch`, `test_touched_devices_is_exactly_the_wired_ports_key_set`)
- **TOPO-L3-9.** Each route of an attached switch gets the first reason that applies, in
  order: `l3_bad_destination`, `l3_bad_next_hop`, `l3_unknown_interface`,
  `l3_interface_unwired` (a physical interface whose port, its declared `port` or else
  its name, carries no resolved hop; a `logical` interface is exempt), then the three
  virtual router reasons, then `l3_next_hop_unverifiable` and
  `l3_next_hop_outside_interface` (both skipped for a route with no next hop). \
  Enforced in: `services/common/herd_common/l3_validation.py` (`validate_one_route`, `usable_interface_attachments`); `services/cabling/app/services/l3_validation.py` (`_validate_one_route`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_validate_one_route_bad_destination`, `test_validate_one_route_bad_next_hop`, `test_validate_one_route_unknown_interface`, `test_route_on_an_unwired_physical_interface_is_refused`, `test_route_on_a_logical_interface_is_exempt_from_the_wiring_check`, `test_declared_port_resolves_an_interface_named_unlike_its_port`, `test_validate_one_route_next_hop_outside_interface`, `test_validate_one_route_interface_route_skips_next_hop_checks`); `services/common/tests/test_l3_validation.py` (`test_unknown_virtual_router`, `test_interface_outside_virtual_router`, `test_interface_bound_to_virtual_router`, `test_the_unwired_check_precedes_every_vrf_reason`)
- **TOPO-L3-10.** Device types come from inventory's `POST /internal/devices/batch` in
  chunks of 500 fetched concurrently, and configs from
  `GET /devices/{id}/config-versions/latest/internal` at most 8 at once, only for
  confirmed Layer 3 switches, each with the internal token and a 4 s timeout, the whole
  pass under 12 s. \
  Enforced in: `services/cabling/app/services/l3_validation.py` (`L3InventoryContext`, `_INVENTORY_BATCH_CHUNK_SIZE`, `_CONFIG_FETCH_CONCURRENCY`, `_INVENTORY_CALL_TIMEOUT_SECONDS`, `_L3_PASS_DEADLINE_SECONDS`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_inventory_call_timeout_constant_is_pinned`, `test_l3_pass_deadline_constant_is_pinned`, `test_valid_route_reports_no_invalid_routes`)
- **TOPO-L3-11.** A transport error, a non-2xx other than the config route's 404, or the
  12 s deadline fails the whole pass closed with 503 `{"error": "l3_config_unavailable"}`. \
  Enforced in: `services/cabling/app/services/l3_validation.py` (`validate_canvas_l3`, `L3ConfigUnavailable`) \
  Pinned by: `services/cabling/tests/test_l3_validation.py` (`test_503_on_device_batch_transport_error`, `test_503_on_device_batch_5xx`, `test_503_on_config_fetch_5xx`, `test_503_on_missing_internal_token_short_circuits_to_l3_config_unavailable`, `test_l3_pass_deadline_trips_into_503`)

**Out of scope.** Driving routes onto switches and drive-time re-validation are
execution's (`provisioning-and-wiring.md`). Switch configuration versions are
`device-configuration.md`.

### 8.12 The reservation fork in cabling

**What it does.** When a reservation goes live, cabling copies its topology into a
private fork, works out which cable hops that drawing needs, and claims those ports so
no other live reservation can use them. The owner then edits the fork's draft; a save
recomputes the wiring, releases what left, builds what arrived, and records a version.
Removing a device from the reservation releases its wiring. When the reservation ends,
the fork is archived as the as-built record.

**Surfaces.** The internal fork routes in section 7, called by reservations, which
decides when to create, save, prune, and archive (`reservations.md`, RES-FORK-1 to
RES-FORK-17). Fork status, versions, and the restore marker are section 4.

**Rules.**

- **TOPO-FORK-1.** Every internal route compares `X-Internal-Token` in constant time and
  refuses a wrong or unconfigured token with 403 `Invalid internal token`; a missing
  header is 422. \
  Enforced in: `services/cabling/app/routes/forks.py` (`_check_internal_token`); `services/common/herd_common/internal_auth.py` (`internal_token_matches`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_requires_internal_token`, `test_save_fork_requires_internal_token`); `services/cabling/tests/test_fork_by_device.py` (`test_wrong_token_is_403`, `test_missing_token_is_422`)
- **TOPO-FORK-2.** Create forks from the explicit `parent_version_id` when it exists and
  pins it; otherwise from the parent topology's latest version, pinned; otherwise from
  the topology's live canvas, unpinned; with no topology at all, from a null canvas. \
  Enforced in: `services/cabling/app/services/fork_service.py` (`_resolve_parent_canvas`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_fork_resolve_explicit_version_pins_it`, `test_fork_resolve_missing_explicit_version_falls_through`, `test_fork_resolve_topology_without_versions_uses_live_canvas`, `test_fork_resolve_unknown_topology_returns_none`); `services/cabling/tests/test_forks.py` (`test_create_fork_no_topology_creates_empty_fork`)
- **TOPO-FORK-3.** Create requires `member_device_ids` (422 without it); a forked canvas
  naming a device node outside it is 409 `{"error": "fork_device_not_member",
  "device_ids": [...]}` and writes nothing. Transit devices are not checked and admins
  are not exempt. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`assert_endpoints_are_members`); `services/cabling/app/schemas/fork.py` (`ForkCreate`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_non_member_endpoint_409_writes_nothing`, `test_create_fork_missing_member_device_ids_422`)
- **TOPO-FORK-4.** Create writes the forked canvas's resolved wiring with the same
  resolver as save (TOPO-FORK-15 to TOPO-FORK-18), in the same transaction as the fork
  row and version 1. \
  Enforced in: `services/cabling/app/services/fork_service.py` (`create_fork`, `_snapshot_connections`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_snapshots_physical_path`, `test_create_fork_skips_unreachable_edge`, `test_create_fork_snapshot_persists_edge_key`); `services/cabling/tests/test_route_handlers_direct.py` (`test_fork_create_snapshots_multi_hop_path_and_dedupes`, `test_fork_snapshot_skips_proposal_and_unresolvable_edges`)
- **TOPO-FORK-5.** Create runs the port-claim lock and check (TOPO-CLAIM-1, TOPO-CLAIM-2)
  on that wiring before writing any row. \
  Enforced in: `services/cabling/app/services/fork_service.py` (`create_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_409_on_cross_reservation_port_claim`, `test_create_fork_archived_other_fork_does_not_block`)
- **TOPO-FORK-6.** Create never judges routing intent: it parses tolerantly, writes every
  well-formed route with `validated_config_version_id` null, and makes no inventory
  call. By decision (ADR 0014 phase 1 review fix R4, recorded in the module docstring of
  `fork_service.py`). \
  Enforced in: `services/cabling/app/services/fork_service.py` (`create_fork`); `services/cabling/app/services/l3_intent.py` (`parse_l3_intent_tolerant`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_create_fork_inserts_l3_routes`, `test_create_fork_drops_malformed_node_with_warning_writes_other_routes`, `test_create_fork_makes_no_inventory_call`, `test_create_fork_leaves_validated_config_version_id_null`)
- **TOPO-FORK-7.** Create answers 201 with the fork id and its highest version number,
  also when it returned an existing fork. \
  Enforced in: `services/cabling/app/routes/forks.py` (`create_fork_internal`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_forks_route_handler_returns_version_number`)
- **TOPO-FORK-8.** Fork create records the request's `created_by` on the rows it writes,
  `system` when absent. \
  Enforced in: `services/cabling/app/routes/forks.py` (`create_fork_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_threads_created_by`)
- **TOPO-FORK-9.** The fork read answers its metadata, stripped draft canvas, restore
  marker, connections in creation order, versions newest first, and routes sorted by
  device then route identity; no fork is 404 `Fork not found`. Any fork status is
  readable. \
  Enforced in: `services/cabling/app/routes/forks.py` (`get_fork_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_get_fork_returns_metadata_canvas_connections_versions`, `test_get_fork_404_when_absent`); `services/cabling/tests/test_fork_l3_routes.py` (`test_internal_get_carries_l3_routes`); `services/cabling/tests/test_fork_versions.py` (`test_get_fork_detail_exposes_draft_restored_from_id`)
- **TOPO-FORK-10.** A fork version read answers that version's own canvas; an unknown
  fork, an unknown version, and another fork's version are each 404. \
  Enforced in: `services/cabling/app/routes/forks.py` (`get_fork_version_internal`, `_load_fork_version`) \
  Pinned by: `services/cabling/tests/test_fork_versions.py` (`test_get_fork_version_returns_canvas_data`, `test_get_fork_version_404_when_fork_absent`, `test_get_fork_version_404_when_version_id_unknown`, `test_get_fork_version_404_for_a_foreign_forks_version`)
- **TOPO-FORK-11.** Canvas PUT stores the stripped canvas as the draft and nothing else:
  no wiring, no routes, no version. It answers the edge pass's verdict without gating on
  it and never runs the routing pass or calls inventory. \
  Enforced in: `services/cabling/app/routes/forks.py` (`update_fork_canvas_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_update_fork_canvas_stores_draft_without_reconcile_or_version`, `test_update_fork_canvas_reports_invalid_without_gating`, `test_update_fork_canvas_404_when_absent`)
- **TOPO-FORK-12.** Restore copies the version's stripped canvas onto the draft
  unchanged, touches no fork connection or route, and answers the edge pass's verdict;
  the next save wires it. \
  Enforced in: `services/cabling/app/routes/forks.py` (`restore_fork_version_internal`) \
  Pinned by: `services/cabling/tests/test_fork_versions.py` (`test_restore_replaces_draft_canvas_byte_for_byte`, `test_restore_does_not_touch_fork_connections`, `test_restore_404_for_a_foreign_version`); `services/cabling/tests/test_route_handlers_direct.py` (`test_restore_fork_version_handler_sets_marker_and_reports_validation`)
- **TOPO-FORK-13.** Save requires `member_device_ids` (422 without it) and refuses a
  canvas naming a device node outside it with 409 `fork_device_not_member` before any
  inventory call, and again inside every version-race retry. \
  Enforced in: `services/cabling/app/routes/forks.py` (`save_fork_internal`); `services/cabling/app/services/fork_save_service.py` (`assert_endpoints_are_members`, `save_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_non_member_endpoint_409`, `test_save_fork_transit_device_need_not_be_member`, `test_save_fork_missing_member_device_ids_422`); `services/cabling/tests/test_fork_l3_routes.py` (`test_save_route_membership_checked_before_gate_no_inventory_call`)
- **TOPO-FORK-14.** Save refuses malformed routing intent with 422
  `{"error": "l3_intent_malformed", "node_id", "message"}` before any inventory call. \
  Enforced in: `services/cabling/app/routes/forks.py` (`save_fork_internal`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_save_route_422_on_malformed_intent`)
- **TOPO-FORK-15.** The resolver turns each committed edge between two device nodes into
  the first shortest path and records each cable on it as one hop with its backing
  connection id and the canvas edge id; a hop two edges share is recorded once, under the
  first edge. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`resolve_canvas_wiring`, `WireSpec`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_resolves_multi_hop_path`, `test_save_fork_dedups_shared_hop`, `test_save_fork_delta_carries_physical_connection_id`, `test_save_persists_edge_key_on_built_wire`, `test_save_groups_hops_per_edge`, `test_save_tolerates_edge_without_id`)
- **TOPO-FORK-16.** Every hop is recorded at layer `L1`; the edge's drawn layer is not
  read. By decision (ADR 0009 option C; the module docstring of `fork_save_service.py`). \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`resolve_canvas_wiring`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_create_fork_snapshots_physical_path`)
- **TOPO-FORK-17.** An edge's `source_port_name` and `target_port_name`, when non-empty,
  constrain its path to those ports, so N edges between one pair with distinct ports
  resolve to N wires; without ports, N edges between one pair resolve to one wire. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`resolve_canvas_wiring`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_two_same_pair_edges_with_ports_resolve_to_two_wires`, `test_save_fork_two_same_pair_edges_without_ports_resolve_to_one_wire`, `test_save_fork_empty_string_port_names_treated_as_absent`, `test_save_fork_distinct_source_ports_share_common_final_hop`); `tests/integration/test_fork_save_port_resolution.py` (`test_activation_fork_resolves_two_port_distinct_edges_to_two_connections`)
- **TOPO-FORK-18.** An edge with no path, or with port constraints no path satisfies,
  contributes no hop and never falls back to an unconstrained path; the save or create
  still succeeds, and the answer does not name the skipped edge. Validation reports
  such an edge before the save (`no_path` or `no_port_path`, TOPO-VAL-5); the save's own
  answer naming it is still open, see #1007. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`resolve_canvas_wiring`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_unresolvable_port_pair_does_not_fall_back`, `test_create_fork_skips_unreachable_edge`)
- **TOPO-FORK-19.** A wire's identity is its two `(device, port)` endpoints in canonical
  order plus its layer; the edge id and connection id are not part of it. Save deletes
  the old wires missing from the new set, then inserts the new ones missing from the old
  set, and leaves the rest untouched. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`connection_identity`, `reconcile_connection_sets`, `reconcile_by_identity`, `save_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_connection_identity_normalizes_orientation`, `test_reconcile_sets_release_build_unchanged`, `test_save_fork_moves_wire`, `test_save_fork_unchanged_wire_is_not_rewritten`, `test_save_fork_removes_all_wiring`, `test_save_edge_id_only_change_reconciles_as_unchanged`)
- **TOPO-FORK-20.** A save is all or nothing: any refusal or error rolls back every
  deletion, insertion, and route change, and appends no version. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`save_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_rolls_back_between_release_and_build`); `services/cabling/tests/test_fork_l3_routes.py` (`test_save_route_409_on_invalid_intent_appends_no_version`)
- **TOPO-FORK-21.** Save judges routing intent only when the canvas has some and its
  route set per device differs from the stored routes; it then runs the routing pass
  before the fork row lock and refuses any blocking reason with 409
  `{"error": "l3_intent_invalid", "invalid_routes": [...]}` (the list includes
  informational entries). \
  Enforced in: `services/cabling/app/routes/forks.py` (`save_fork_internal`); `services/cabling/app/services/fork_save_service.py` (`gate_l3_intent`, `l3_intent_changed`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_save_route_unchanged_l3_intent_never_calls_validation`, `test_save_route_adding_a_route_calls_validation`, `test_save_route_removing_all_routes_never_calls_validation`, `test_save_route_gate_runs_before_for_update_load`, `test_gate_l3_intent_409_exact_shape_on_invalid_routes`, `test_gate_l3_intent_ignores_duplicate_route_entries_for_refusal`)
- **TOPO-FORK-22.** Routes built by a judged save carry the config version they were
  judged against in `validated_config_version_id`. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`gate_l3_intent`, `l3_row_from_spec`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_gate_l3_intent_returns_validated_config_version_ids`, `test_save_route_stamps_validated_config_version_id`)
- **TOPO-FORK-23.** Save reconciles routes by `(device_id, route identity)` the way it
  reconciles wires, and reports `l3_routes_built` and `l3_routes_released`. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`reconcile_l3_route_sets`, `save_fork`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_save_fork_builds_l3_routes_and_counts`, `test_save_fork_second_save_releases_and_builds`, `test_save_fork_resaving_the_same_intent_is_unchanged`, `test_save_fork_move_across_devices_end_to_end`, `test_save_fork_duplicate_route_in_canvas_collapses_to_one_row`)
- **TOPO-FORK-24.** Save resolves the canvas once; a version-race retry recomputes only
  the set arithmetic and claims against committed rows, with no second resolve or
  inventory call. \
  Enforced in: `services/cabling/app/routes/forks.py` (`save_fork_internal`); `services/cabling/app/services/fork_save_service.py` (`save_fork`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_save_route_resolves_canvas_wiring_exactly_once`, `test_save_fork_l3_reconcile_reapplies_on_version_race_retry`); `services/cabling/tests/test_forks.py` (`test_save_fork_retries_on_version_conflict`)
- **TOPO-FORK-25.** The user-facing fork read is redacted to the caller's device
  visibility (issue #1008). Reservations forwards the caller's bearer beside the internal
  token (`on_behalf_of`); for a non-admin, cabling resolves visibility through
  `resolve_caller_visibility` and every connection end on a hidden device comes back with
  its device id and port null, the row's `physical_connection_id` null, and `hidden`
  true, the redaction pathfind applies to a hidden transit hop (TOPO-PATH-9). An admin is
  unfiltered; an unanswerable lookup is 503 `Could not verify device visibility; the fork
  was not returned. Retry the request.`, relayed by reservations; a forwarded bearer that
  does not verify is 401. A service caller that forwards no bearer (execution,
  ai-orchestrator) gets every row as stored. \
  Enforced in: `services/cabling/app/routes/forks.py` (`get_fork_internal`, `_redact_fork_connection`, `_decode_on_behalf_of`); `services/reservations/app/routers/reservations.py` (`get_reservation_fork`); `services/reservations/app/services/reservation_service.py` (`_cabling_fork_call`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_get_fork_on_behalf_of_non_admin_redacts_hidden_transit`, `test_get_fork_on_behalf_of_admin_and_service_callers_unredacted`, `test_get_fork_on_behalf_of_visibility_unavailable_is_503`, `test_get_fork_on_behalf_of_bad_bearer_is_401`); `services/reservations/tests/test_fork_endpoints.py` (`test_get_fork_owner_forwards_200`, `test_get_fork_lazy_create_rereads_on_behalf_of_caller`, `test_get_fork_relays_cabling_visibility_503`, `test_cabling_fork_call_sends_on_behalf_of_beside_internal_token`)
- **TOPO-CLAIM-1.** A wire to build whose `(device, port)` endpoint is already in a wire
  of another `ACTIVE` fork refuses the save or create with 409
  `{message, conflicts: [{reservation_id, device_id, port}]}`, conflicts sorted;
  `ARCHIVED` forks and the fork's own rows never block. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`assert_no_port_claims`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_409_on_cross_reservation_port_claim`, `test_save_fork_archived_other_fork_does_not_block`)
- **TOPO-CLAIM-2.** Immediately before that check, a writer takes one transaction-scoped
  Postgres advisory lock per claimed endpoint, keyed `forkport:<device_id>:<port>`,
  distinct and sorted, so two concurrent writers on different forks serialize. On
  SQLite the lock is a no-op. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`lock_port_claims`); `services/common/herd_common/advisory_lock.py` (`xact_lock`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_lock_port_claims_locks_sorted_deduped_canonical_keys`, `test_lock_port_claims_noop_on_empty_to_build`); `services/cabling/tests/test_fork_port_claim_race_live_pg.py` (`test_concurrent_saves_on_two_forks_cannot_both_claim_the_same_port`)
- **TOPO-CLAIM-3.** Only wires being built are claim-checked, so a prune, which builds
  nothing, can never be refused by a claim. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`assert_no_port_claims`, `prune_fork_devices`) \
  Pinned by: `services/cabling/tests/test_fork_prune.py` (`test_prune_never_409s_on_foreign_port_claims`)
- **TOPO-CLAIM-4.** A version-race retry re-runs the claim query against the committed
  rows. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`save_fork`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_save_fork_port_claim_query_reruns_on_retry`)
- **TOPO-PRUNE-1.** Prune releases a wire when its edge id belongs to a saved edge
  touching a removed device (all hops of that edge, far ones too), or when it touches a
  removed device and its edge id is not a remaining saved edge; a through-hop serving a
  remaining edge stays, and a wire with a null or stale edge id touching a removed device
  is released. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`_rows_released_by_prune`, `prune_canvas_for_devices`) \
  Pinned by: `services/cabling/tests/test_fork_prune.py` (`test_prune_releases_removed_device_rows_and_bumps_version`, `test_prune_keeps_through_hop_serving_remaining_edge`, `test_prune_releases_far_hops_of_pruned_edges`, `test_prune_releases_stale_and_null_edge_key_rows`)
- **TOPO-PRUNE-2.** Prune decides from the stored wires and the last saved version's
  canvas, never the draft; it removes the removed devices' nodes and their edges from the
  draft and keeps every other unsaved edit. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`prune_fork_devices`) \
  Pinned by: `services/cabling/tests/test_fork_prune.py` (`test_prune_ignores_draft_and_preserves_unsaved_edits`, `test_prune_scrubs_draft_only_content_without_a_version`)
- **TOPO-PRUNE-3.** Prune deletes every route of a removed device, also when the device
  had no wiring. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`prune_fork_devices`) \
  Pinned by: `services/cabling/tests/test_fork_l3_routes.py` (`test_prune_deletes_removed_devices_l3_routes`, `test_prune_l3_only_device_with_no_wiring_still_releases`)
- **TOPO-PRUNE-4.** A prune that releases nothing answers `changed` false with the current
  version number. \
  Enforced in: `services/cabling/app/services/fork_save_service.py` (`prune_fork_devices`, `ForkPruneResult`) \
  Pinned by: `services/cabling/tests/test_fork_prune.py` (`test_prune_is_idempotent_on_replay`); `services/cabling/tests/test_route_handlers_direct.py` (`test_prune_fork_devices_no_release_no_draft_change_is_a_pure_replay`)
- **TOPO-LISTF-1.** The active fork listing holds only `ACTIVE` forks, oldest first, with
  each fork's latest version number (0 when none), `limit` 1 to 1000 (default 200), and
  the total. \
  Enforced in: `services/cabling/app/routes/forks.py` (`list_active_forks_internal`) \
  Pinned by: `services/cabling/tests/test_forks.py` (`test_list_active_forks_excludes_archived`, `test_list_active_forks_pagination`, `test_list_active_forks_reports_latest_fork_version`, `test_list_active_forks_empty`)
- **TOPO-BYDEV-1.** The by-device lookup answers the reservation ids, distinct and
  sorted, of every non-archived fork with a wire naming the device at either end and any
  layer, transit hops included; an unknown device answers empty, not 404. \
  Enforced in: `services/cabling/app/routes/forks.py` (`list_forks_by_device_internal`) \
  Pinned by: `services/cabling/tests/test_fork_by_device.py` (`test_device_as_source`, `test_device_as_target`, `test_device_as_middle_hop_only`, `test_archived_fork_does_not_count`, `test_two_active_forks_sorted_and_archived_excluded`, `test_no_forks_is_empty`)
- **TOPO-BYDEV-2.** The same answer carries `connection_count`, the true number of
  connections naming the device at either end (a loopback once), and `connection_ids`,
  at most 10 ids sorted by their text. \
  Enforced in: `services/cabling/app/routes/forks.py` (`list_forks_by_device_internal`, `CONNECTION_ID_SAMPLE_LIMIT`) \
  Pinned by: `services/cabling/tests/test_fork_by_device.py` (`test_connections_named_at_either_end_are_reported`, `test_loopback_connection_counts_once`, `test_connection_id_sample_is_capped_but_count_is_true_total`, `test_uncabled_or_unknown_device_has_zero_connections`)
- **TOPO-DEVB-1.** The devices batch takes 1 to 500 reservation ids and answers, for each
  with a fork of either status, the distinct sorted devices its wires name; a reservation
  with no fork is absent. \
  Enforced in: `services/cabling/app/routes/forks.py` (`get_fork_devices_batch_internal`); `services/cabling/app/schemas/fork.py` (`ForkDevicesBatchRequest`) \
  Pinned by: `services/cabling/tests/test_fork_devices_batch.py` (`test_batch_returns_sorted_distinct_ids_active_and_archived`, `test_batch_id_with_no_fork_is_absent`, `test_batch_empty_list_is_422`, `test_batch_over_cap_is_422`, `test_batch_exactly_at_cap_is_accepted`)

**Out of scope.** When reservations creates, saves, prunes, or archives a fork, and the
user-facing forwarding, are `reservations.md` (RES-FORK-1 to RES-FORK-19). What execution
builds from the wires and routes is `provisioning-and-wiring.md`.

### 8.13 Topology templates

**What it does.** A user can save a diagram as a reusable template whose devices become
roles, then create a new topology from it by picking a device for each role.

**Surfaces.** User interface `frontend/src/pages/TopologyTemplatesPage.tsx` and the
editor's Save as Template; the seven `/templates` routes in section 5.

**Rules.**

- **TOPO-TMPL-1.** Any signed-in user lists templates (most recently updated first) and
  reads one with its stripped canvas; an unknown id is 404 `Template not found`. \
  Enforced in: `services/cabling/app/routes/templates.py` (`list_templates`, `get_template`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_list_templates_paginated`, `test_get_template_not_found`); `services/cabling/tests/test_route_handlers_direct.py` (`test_template_create_and_list_and_get`)
- **TOPO-TMPL-2.** Any signed-in user creates a template, owned by the caller, with an
  optional stripped canvas; template names are unique across all users and a duplicate
  is 409 `Template name '<name>' already exists`. \
  Enforced in: `services/cabling/app/routes/templates.py` (`create_template`); `services/cabling/app/models/template.py` (`TopologyTemplate`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_create_blank_template`, `test_unique_template_name`); `services/cabling/tests/test_route_handlers_direct.py` (`test_template_create_duplicate_name_409`)
- **TOPO-TMPL-3.** Template `name` takes the topology name bound (1 to 100 characters)
  and `description` the topology description bound (2000 characters) on create, update,
  and from-topology; the instantiate `name` names a topology and takes the same name
  bound. A value outside the bound is 422. \
  Enforced in: `services/cabling/app/schemas/template.py` (`TemplateCreate`, `TemplateUpdate`, `TemplateFromTopologyRequest`, `InstantiateRequest`) \
  Pinned by: `services/cabling/tests/test_schema_bounds.py` (`test_template_name_bounds`, `test_template_description_bounds`, `test_template_update_name_bounds`, `test_instantiate_name_bounds`); `services/cabling/tests/test_templates.py` (`test_template_name_bounds_are_422_on_every_write_route`, `test_template_name_at_cap_accepted`)
- **TOPO-TMPL-4.** Updating or deleting a template is creator or admin (403 otherwise); an
  update changes only the fields sent and a duplicate name is 409. \
  Enforced in: `services/cabling/app/routes/templates.py` (`update_template`, `delete_template`, `_can_manage`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_update_template_owner`, `test_update_template_other_user_forbidden`, `test_update_template_admin_can_edit`, `test_delete_template_owner`, `test_delete_template_other_user_forbidden`); `services/cabling/tests/test_route_handlers_direct.py` (`test_template_update_duplicate_name_409`)
- **TOPO-TMPL-5.** Any signed-in user may make a template from any topology: every device
  node's `data.device` becomes `{role}`, the role being the device's `template_name` (else the
  node label, else `device`) lowercased with spaces as hyphens plus a per-name counter;
  edges are copied unchanged, and a null canvas gives an empty template. \
  Enforced in: `services/cabling/app/routes/templates.py` (`create_template_from_topology`, `_extract_role_template`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_from_topology_extracts_roles`, `test_from_topology_not_found`); `services/cabling/tests/test_route_handlers_direct.py` (`test_template_from_topology_empty_canvas`)
- **TOPO-TMPL-6.** Only device nodes become roles (`is_device_node`: an object under
  `data.device` on a node that is not a network element). Network element nodes and every
  other node pass through unchanged, so element attachments survive the round trip. A
  template stored before #1005 may carry a role on an element node: template reads and
  writes drop that `data.device`, and instantiate neither demands nor assigns a device
  for it, so no data migration is needed. \
  Enforced in: `services/cabling/app/routes/templates.py` (`_extract_role_template`, `_instantiate_canvas`, `_normalize_template_canvas`); `services/cabling/app/services/canvas_nodes.py` (`is_device_node`, `is_element_node`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_element_survives_save_as_template_and_instantiate`, `test_legacy_template_role_on_element_is_ignored`)
- **TOPO-TMPL-7.** Every template write route (create, update, from-topology) answers a
  taken name with the same 409 `Template name '<name>' already exists`. \
  Enforced in: `services/cabling/app/routes/templates.py` (`create_template_from_topology`, `_duplicate_name`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_duplicate_name_is_409_from_every_write_route`)
- **TOPO-TMPL-8.** Instantiate requires an assignment for every role on the canvas (422
  `missing assignment for role '<role>'` otherwise), writes the assigned id into each
  role node's `data.device.id`, leaves nodes without a role unchanged, and creates a
  topology owned by the caller with version 1. \
  Enforced in: `services/cabling/app/routes/templates.py` (`instantiate_template`, `_instantiate_canvas`) \
  Pinned by: `services/cabling/tests/test_templates.py` (`test_instantiate_substitutes_devices_and_creates_v1_snapshot`, `test_instantiate_missing_role_assignment`, `test_instantiate_not_found`); `services/cabling/tests/test_route_handlers_direct.py` (`test_template_instantiate_empty_canvas`)
- **TOPO-TMPL-9.** Instantiate does not check the assigned device ids against inventory
  or the caller's visibility. \
  Enforced in: `services/cabling/app/routes/templates.py` (`instantiate_template`) \
  Pinned by: none

**Out of scope.** Inventory device and port templates (`/api/inventory/templates`) are a
different thing (`inventory.md`).

### 8.14 Topology import and export

**What it does.** Anyone signed in can download every topology as JSON (lossless) or
CSV (one row per drawn line), and upload such a file to create topologies or update
their own by name. A dry run reports what would happen without writing.

**Surfaces.** User interface `frontend/src/components/ui/BulkImportExport.tsx` on the
topologies page; routes `GET /topologies/export` and `POST /topologies/import`; inventory
`POST /devices/resolve-by-name` (section 10). [BULK_IMPORT_EXPORT.md](../BULK_IMPORT_EXPORT.md)
is the user guide.

**Rules.**

- **TOPO-BULK-1.** Export answers every topology, ordered by name, as an attachment named
  `topologies.json` or `topologies.csv`; `format` other than `csv` or `json` is 422. \
  Enforced in: `services/cabling/app/routes/bulk.py` (`export_topologies`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_bulk_export_handler_json_and_csv`)
- **TOPO-BULK-2.** JSON export is `{resource: "topologies", version: 1, items: [{name,
  canvas}]}` with each device node's `id` removed and its `name` kept, then stripped. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`records_to_json`, `topology_to_record`, `canvas_ids_to_names`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_export_json_rewrites_device_id_to_name`, `test_export_then_import_full_roundtrip`)
- **TOPO-BULK-3.** CSV export writes one row per canvas edge with columns
  `topology_name, source_device, source_port, target_device, target_port, layer`, device
  names taken from `data.device.name`; nodes with no edge are not exported. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`records_to_csv`, `topology_to_csv_rows`, `TOPOLOGY_CSV_COLUMNS`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_export_csv_flattens_edges`); `services/cabling/tests/test_route_handlers_direct.py` (`test_records_to_csv_null_canvas_emits_header_only`)
- **TOPO-BULK-4.** CSV port cells read the edge's `data.source_port_name` and
  `data.target_port_name` (what the editor writes and the fork resolver honors,
  TOPO-FORK-17), else the legacy `data.sourcePort` and `data.targetPort`, else empty; the
  React Flow `sourceHandle` and `targetHandle` are handle ids, never read. CSV import
  writes a non-empty port cell to `source_port_name` or `target_port_name` and leaves an
  empty cell's side unconstrained, so export then import keeps every chosen port. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`topology_to_csv_rows`, `_edge_port_name`, `parse_csv_topologies`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_export_csv_writes_editor_port_names_not_handles`, `test_export_csv_port_precedence_and_empty_cells`, `test_csv_export_import_preserves_ports_through_fork_resolve`, `test_parse_csv_empty_port_cell_leaves_side_unconstrained`)
- **TOPO-BULK-5.** Every CSV text cell is written through `csv_safe_cell` and read back
  through `csv_unsafe_cell`, so a formula-led name is neutralized on export and
  round-trips exactly. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`topology_to_csv_rows`, `parse_csv_topologies`); `services/common/herd_common/csv_safety.py` (`csv_safe_cell`, `csv_unsafe_cell`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_export_csv_neutralizes_formula_trigger_cells`, `test_export_then_import_csv_roundtrips_formula_name`)
- **TOPO-BULK-6.** Import is open to any signed-in user; `dry_run=true` runs every check
  and returns the full report but writes nothing. \
  Enforced in: `services/cabling/app/routes/bulk.py` (`import_topologies_endpoint`); `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_import_json_creates_topology`, `test_dry_run_writes_nothing`, `test_dry_run_update_writes_nothing`)
- **TOPO-BULK-7.** JSON import takes an object with an `items` list or a bare list;
  invalid JSON, another shape, or a non-list `items` is 422 for the whole request. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`parse_json_topologies`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_parse_json_invalid_json_raises_422`, `test_parse_json_bare_list_accepted`, `test_parse_json_wrong_shape_raises_422`, `test_parse_json_items_not_a_list_raises_422`)
- **TOPO-BULK-8.** CSV import groups rows by topology name (rows without one are
  skipped), makes one node per distinct device name with id `node-<name>`, and one edge
  per row carrying `layer`, `sourcePort`, and `targetPort`. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`parse_csv_topologies`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_parse_csv_groups_rows_into_topologies`, `test_parse_csv_merges_same_name_topology_rows`); `services/cabling/tests/test_bulk.py` (`test_import_csv_roundtrip`)
- **TOPO-BULK-9.** All device names in the file are resolved in one inventory call with
  the internal token (10 s); a failure of that call is 503 for the whole request before
  any row is processed. \
  Enforced in: `services/cabling/app/services/device_resolver.py` (`resolve_device_names`); `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_import_resolver_failure_raises_503`, `test_device_resolver_returns_resolved_map`, `test_device_resolver_raises_on_transport_failure`, `test_device_resolver_empty_names_short_circuits`)
- **TOPO-BULK-10.** A row with no name is rejected `missing required field: name`; a row
  naming devices that did not resolve is rejected `unresolved device names: <names>`. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`, `rewrite_canvas_names_to_ids`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_missing_name_rejected`, `test_unresolved_device_name_is_rejected`); `services/cabling/tests/test_route_handlers_direct.py` (`test_import_missing_name_and_unresolved_rejected`)
- **TOPO-BULK-11.** For a non-admin, visibility is read once per request (503 and nothing
  processed when it fails); a name resolving to a hidden device is reported exactly like
  an unknown name, and a device node carrying a raw id with no name is redacted before
  validation. Admins cause no visibility call. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`, `_hidden_names_in_canvas`); `services/cabling/app/services/canvas_nodes.py` (`redact_invisible_device_nodes`) \
  Pinned by: `services/cabling/tests/test_visibility_oracle.py` (`test_import_non_admin_hidden_device_matches_nonexistent_device_report`, `test_import_non_admin_hidden_l3_switch_via_smuggled_id_is_redacted`, `test_import_non_admin_non_dry_run_hidden_device_creates_nothing`, `test_import_non_admin_fails_closed_when_visibility_unavailable`, `test_import_admin_never_calls_the_visibility_lookup`, `test_import_csv_hidden_device_matches_json_report`)
- **TOPO-BULK-12.** Each row is fully validated (both passes) before any write; an
  invalid row is rejected `topology validation failed: ` followed by
  `<reason>(<edge_id>)` and `<node_id>[<index>] (<reason>)` entries. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_unreachable_edge_rejected_by_validator`); `services/cabling/tests/test_route_handlers_direct.py` (`test_import_validation_failure_rejects_row`, `test_import_route_reasons_included_in_reject_message`)
- **TOPO-BULK-13.** A 503 raised while judging a row (`l3_config_unavailable`) stops the
  whole request with that 503; rows already processed keep their committed writes. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_route_handlers_direct.py` (`test_import_l3_config_unavailable_aborts_whole_request`)
- **TOPO-BULK-14.** A row matches existing topologies by exact name, preferring the
  caller's own, else the earliest created; with no match it creates a topology owned by
  the caller. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_owned_match_preferred_over_older_foreign_topology`, `test_admin_owned_match_preferred_on_name_collision`, `test_mixed_create_and_update_batch`)
- **TOPO-BULK-15.** A non-admin whose row matches another user's topology is rejected
  with the reason starting `not_authorized:`, before the edit lock is consulted; admins
  may update any. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`, `_NOT_OWNED_TOPOLOGY_REASON`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_cross_owner_update_rejected_for_nonadmin`, `test_cross_owner_update_succeeds_for_admin`, `test_self_update_succeeds_for_nonadmin`, `test_ownership_gate_precedes_reservation_lock`, `test_dry_run_reports_cross_owner_rejection_identically`)
- **TOPO-BULK-16.** An update whose canvas equals the stored one is reported `update`
  with no write and no version; a changed canvas is written with a version described
  `Updated via bulk import`. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_reimport_identical_is_noop_update`, `test_reimport_changed_canvas_updates_in_place`)
- **TOPO-BULK-17.** A non-admin's changed canvas on a topology another user's live
  reservation references is rejected with the pinned lock reason; the caller's own
  reservation does not block, admins skip the check, and the lookup fails open. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`, `_LOCKED_TOPOLOGY_REASON`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_update_blocked_by_other_users_active_reservation`, `test_update_allowed_when_reservation_is_own`, `test_admin_bypasses_reservation_lock`)
- **TOPO-BULK-18.** Each row commits on its own; any other error rejects only that row,
  rolled back, and the batch continues; the report counts `created`, `updated`,
  `skipped`, and `rejected`. \
  Enforced in: `services/cabling/app/services/bulk_service.py` (`import_topologies`, `_tally`) \
  Pinned by: `services/cabling/tests/test_bulk.py` (`test_one_bad_topology_does_not_abort_batch`); `services/cabling/tests/test_route_handlers_direct.py` (`test_import_unexpected_exception_rejects_row`, `test_import_http_exception_inside_loop_rejects_row`)

**Out of scope.** Device and inventory template import is `inventory.md`.

### 8.15 Fabric lookup

**What it does.** Execution asks which physical fabric a device is in, so a VLAN number
can be reused in fabrics that share no cable.

**Surfaces.** Route `GET /fabric/internal` (section 7).

**Rules.**

- **TOPO-FABRIC-1.** The fabric is the device's connected component in the cabling graph
  (just the device when it has no cable); `fabric_id` is the UUID5 of the sorted member
  ids joined by `|`, so every member gets the same id. \
  Enforced in: `services/cabling/app/services/fabric_service.py` (`find_connected_component`, `compute_fabric_id`); `services/cabling/app/routes/fabric.py` (`get_fabric_internal`) \
  Pinned by: `services/cabling/tests/test_fabric.py` (`test_fabric_id_deterministic`, `test_fabric_id_same_from_any_member`, `test_fabric_endpoint_same_component`, `test_fabric_endpoint_different_components`, `test_fabric_endpoint_unknown_device`, `test_fabric_endpoint_invalid_token`, `test_fabric_updates_after_connection_added`)

**Out of scope.** VLAN allocation is execution's (`provisioning-and-wiring.md`).

### 8.16 The topologies page

**What it does.** The topologies page lists every topology with search, an owner filter,
sortable headings, clone, and delete, singly or for several selected rows at once.

**Surfaces.** `frontend/src/pages/TopologyPage.tsx`, `frontend/src/lib/topologyFilters.ts`,
`frontend/src/lib/topologyBulk.ts`; routes `GET /topologies`, `POST /topologies`,
`POST /topologies/{id}/clone`, `DELETE /topologies/{id}`.

**Rules.**

- **TOPO-UILIST-1.** A heading click cycles ascending, descending, then back to the
  default (updated, newest first), resets to page 1, and persists under the preference
  key `sort:topologies`; a stale saved field falls back and is never sent. \
  Enforced in: `frontend/src/lib/topologyFilters.ts` (`nextTopologySort`, `parseTopologySort`, `TOPOLOGY_SORT_PAGE_KEY`) \
  Pinned by: `frontend/src/test/pages/TopologyPageListControls.test.tsx` (`a heading cycles ascending, descending, then back to the default, resetting to page 1`, `persists the choice under extras sort:topologies`, `a stale persisted sort field falls back to the default and is never sent`)
- **TOPO-UILIST-2.** Search is debounced, trimmed, and persisted, and whitespace alone
  sends nothing; Owner Mine sends `owner=mine` and persists, and a stale saved owner
  falls back to All. \
  Enforced in: `frontend/src/lib/topologyFilters.ts` (`parseSavedTopologyFilter`, `serializeTopologyFilter`); `frontend/src/pages/TopologyPage.tsx` (`TopologyPage`) \
  Pinned by: `frontend/src/test/pages/TopologyPageListControls.test.tsx` (`search is debounced, trimmed, sent, resets to page 1, and persisted`, `a whitespace-only search sends nothing`, `Owner Mine sends owner=mine and persists it; All removes it`, `a stale saved owner falls back to All and is never sent`)
- **TOPO-UILIST-3.** A filter change made before the saved preferences load is held,
  not sent: when the load settles, only the fields the user changed are applied over the
  loaded filter and the merged value is saved once, so the saved search survives
  (issue #985). \
  Enforced in: `frontend/src/stores/preferencesStore.ts` (`usePreferencesStore`, `mergePreLoadFilter`) \
  Pinned by: `frontend/src/test/stores/preferencesStore.test.ts` (`a status change before the load keeps the saved search (the issue's observation)`, `a search typed before the load wins over the saved search`, `nothing is sent while the load is pending, however many writes are made`)
- **TOPO-UILIST-4.** The selection covers the current page only and clears on any page,
  sort, search, or owner change. \
  Enforced in: `frontend/src/pages/TopologyPage.tsx` (`TopologyPage`) \
  Pinned by: `frontend/src/test/pages/TopologyPageListControls.test.tsx` (`a row checkbox selects without navigating, and select-all takes the page`, `clears on a page change`, `clears on a sort change`, `clears on a search change`, `clears on an Owner filter change`)
- **TOPO-UILIST-5.** Delete is offered to the creator or an admin, through one predicate
  shared by the row button and the bulk action. \
  Enforced in: `frontend/src/lib/topologyBulk.ts` (`canDeleteTopologyAs`, `partitionTopologies`) \
  Pinned by: `frontend/src/test/pages/TopologyPageListControls.test.tsx` (`the row Delete and the bulk eligibility share one rule: a non-admin non-owner gets neither`); `frontend/src/test/pages/TopologyPage.test.tsx` (`a non-admin sees Delete only on their own topology row`, `an admin sees Delete on every row regardless of owner`); `frontend/src/test/lib/topologyBulk.test.ts` (`undefined user is refused`, `an empty user id never matches an empty created_by`)
- **TOPO-UILIST-6.** Bulk delete sends one DELETE per eligible row; a failed row stays
  selected with the server's reason, the in-use refusal is worded from its reservation
  ids, and the summary quotes one shared reason or else gives counts only. \
  Enforced in: `frontend/src/lib/topologyBulk.ts` (`summarizeDelete`); `frontend/src/lib/errors.ts` (`topologyDeleteErrorText`, `topologyInUseDetail`) \
  Pinned by: `frontend/src/test/pages/TopologyPageListControls.test.tsx` (`a partial failure keeps the failed row selected with the server's reason; invalidates once`, `a topology in use by a live reservation stays selected with the in-use reason (#977)`, `different failure reasons give counts only and keep every failed row`); `frontend/src/test/lib/topologyBulk.test.ts` (`a shared reason is quoted`)
- **TOPO-UILIST-7.** Clone pre-fills `<name> (copy)` and opens the new topology on
  success. \
  Enforced in: `frontend/src/pages/TopologyPage.tsx` (`TopologyPage`) \
  Pinned by: `frontend/src/test/pages/TopologyPage.test.tsx` (`pre-fills the clone name as '<name> (copy)' and submits it`, `keeps the clone modal open and does not navigate when clone fails`)

**Out of scope.** The shared left filter panel component is
`frontend/src/components/ui/ListFilterPanel.tsx`, specified with the other list pages.

### 8.17 The topology editor

**What it does.** The editor is a canvas: a user drags devices, dynamic placeholders, and
network elements from a palette, draws lines between them, sees each line turn green or
red against the real cabling, saves the drawing as a new version, and reserves it.

**Surfaces.** `frontend/src/pages/TopologyEditorPage.tsx`,
`frontend/src/components/equipment-browser/EquipmentBrowser.tsx`,
`frontend/src/stores/topologyStore.ts`, `frontend/src/api/inventory.ts`
(`hydrateCanvasNodes`), `frontend/src/components/topology-editor/edges/edgeStatus.ts`;
routes `GET` and `PUT /topologies/{id}`, `POST /pathfind/batch`,
`POST /topologies/{id}/validate`, `POST /templates/from-topology/{id}`.

**Rules.**

- **TOPO-UI-1.** On load, every node with a device id is refreshed from inventory in one
  batch and typed `deviceNode`, also when its device is missing from the answer; a failed
  batch keeps the stored data. \
  Enforced in: `frontend/src/api/inventory.ts` (`hydrateCanvasNodes`); `frontend/src/lib/canvasHydration.ts` (`hydrateAndLoadCanvas`) \
  Pinned by: `frontend/src/test/api/inventory.test.tsx` (`fills thin nodes with the fetched name, topology_type, and label`, `sets type 'deviceNode' on a typeless device node so it renders, not as a blank box`, `sets type 'deviceNode' even when the device is omitted (typeless stale node)`); `frontend/src/test/lib/canvasHydration.test.ts` (`falls back to loading the original canvas when hydrateCanvasNodes rejects`)
- **TOPO-UI-2.** A stored node with no device, between two device nodes, crashes the
  editor into its error boundary. Known gap, see #989. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`TopologyEditorPage`) \
  Pinned by: none (issue #989)
- **TOPO-UI-3.** The palette lists only `dut_only` devices, hides devices already on the
  canvas, can hide exclusive `RESERVED` devices, refuses to drag an unavailable device,
  and offers dynamic templates and the four network element types as separate sections. \
  Enforced in: `frontend/src/components/equipment-browser/EquipmentBrowser.tsx` (`EquipmentBrowser`) \
  Pinned by: `frontend/src/test/components/EquipmentBrowser.test.tsx` (`excludes devices already placed on the canvas`, `hides exclusive reserved devices when the show-reserved toggle is off`, `refuses to start a drag for an unavailable device card`, `renders dynamic templates in their own section as drag sources`, `drags a card with the application/herd-network-element MIME carrying element_type and a default label`)
- **TOPO-UI-4.** Dropping a device already on the canvas adds nothing; a dynamic template
  adds one placeholder per template (a re-drop is a no-op, and none in live edit); a
  network element always adds a new node with a fresh element id. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`onDrop`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.ElementAttachAndDrop.test.tsx` (`dropping a device creates a deviceNode at the drop position`, `dropping a device already on the canvas is a no-op (no duplicate node)`); `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`dropping a dynamic template creates one placeholder with count 1; re-dropping the same template is a no-op`); `frontend/src/test/pages/TopologyEditorNetworkElements.test.tsx` (`dropping a network element creates a networkElementNode with a fresh client-minted UUID element id`, `allows multiple elements of the same type, unlike the one-placeholder-per-template rule`)
- **TOPO-UI-5.** A line is refused with a toast when either end is a dynamic placeholder,
  when both ends are network elements, or when two devices differ in topology type; a
  device-to-element line opens the attach dialog instead of the wiring dialog. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`isValidConnection`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`refuses a connection to a placeholder with a toast and creates no edge`); `frontend/src/test/pages/TopologyEditorNetworkElements.test.tsx` (`isValidConnection refuses element-to-element with the exact toast text and creates no edge`, `connecting a device to an element opens ElementAttachDialog with the right device and element props`); `frontend/src/test/pages/TopologyEditorPage.HistoryAndSave.test.tsx` (`refuses connecting two devices of different topology types with the exact toast text`)
- **TOPO-UI-6.** A line shows red `uncabled port` when a chosen port has no cable, red
  `no path` when the batch pathfind finds none, and green with the hop count otherwise;
  pairs beyond 2000 go in sequential batches. \
  Enforced in: `frontend/src/components/topology-editor/edges/edgeStatus.ts` (`resolveEdgeStroke`); `frontend/src/api/connections.ts` (`usePathfindPairs`, `PATHFIND_BATCH_LIMIT`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.HistoryAndSave.test.tsx` (`reconciles pathValid and hopCount from a pathfind response onto matching edges`); `frontend/src/test/api/connections.test.tsx` (`usePathfindPairs issues ONE batch request and maps results by pair`, `usePathfindPairs splits pair lists beyond the batch cap into chunks`)
- **TOPO-UI-7.** Save sends the canvas without dynamic placeholders (the toast says so
  when there were some) and with network elements kept. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleSave`, `persistableCanvas`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`saving the parent topology excludes placeholder nodes from canvas_data`, `saving without placeholders keeps the plain success toast`); `frontend/src/test/pages/TopologyEditorNetworkElements.test.tsx` (`persistableCanvas KEEPS network element nodes and their edges (the placeholder-opposite rule)`)
- **TOPO-UI-8.** After a save, when the canvas carries routing intent, the editor
  validates and toasts the blocking problems only; with no intent it does not validate. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleSave`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.L3Routing.test.tsx` (`does not call validate after a save when the canvas carries no data.l3`, `calls validate after a save when the canvas carries data.l3, and toasts the problem count with device[index] reason labels`, `does not toast an error or mark the badge invalid for a duplicate-only validate result`)
- **TOPO-UI-9.** Reserve is enabled for a canvas with devices or placeholders and
  disabled while any line is invalid; it sends placeholders as dynamic requests, count
  per template. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`TopologyEditorPage`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorDynamicPlaceholders.test.tsx` (`enables Reserve for a placeholder-only canvas (dynamic-only booking)`, `reserving sends device_ids plus dynamic_requests expanded count-per-template`)
- **TOPO-UI-10.** Save as Template sends the topology id and name; a failure shows the
  server's detail and keeps the dialog open. \
  Enforced in: `frontend/src/api/topologyTemplates.ts` (`useCreateTemplateFromTopology`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.HistoryAndSave.test.tsx` (`submitting creates a template, closes the modal, and clears the name field`, `a failed submit shows the server's detail message and keeps the modal open`)
- **TOPO-UI-11.** A version restore refused for live reservations shows the blocking
  list instead of restoring. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleRestoreConfirm`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.HistoryAndSave.test.tsx` (`Restore blocked by active reservations (409) surfaces the blocking list instead of restoring`, `Restore succeeds: loads the returned canvas, shows a success toast, and clears the restore target`)
- **TOPO-UI-12.** The template page derives a template's roles from its canvas and
  refuses to instantiate until every role has a device. \
  Enforced in: `frontend/src/pages/TopologyTemplatesPage.tsx` (`TopologyTemplatesPage`) \
  Pinned by: `frontend/src/test/pages/TopologyTemplatesPage.test.tsx` (`derives unique roles from canvas_data nodes, filtering out nodes with no role`, `blocks submit with a toast when a role has no device assigned`, `submits name and role_assignments once every role has a device, then navigates`)

**Out of scope.** The AI generate and commit dialogs are `ai-features.md`; the reserve
modal is `reservations.md`.

### 8.18 Wiring dialog, quick connect, bundled lines, and element attachments

**What it does.** Drawing a line between two devices opens a two-column port picker
where a user connects specific ports, one or many lines at a time, each with its own
layer. Many lines between the same two devices draw as one thick line with a count.

**Surfaces.** `frontend/src/components/topology-editor/WiringDialog.tsx`,
`frontend/src/components/topology-editor/QuickConnectPopover.tsx`,
`frontend/src/components/topology-editor/wiring/`,
`frontend/src/components/topology-editor/edges/groupEdgesForRender.ts`,
`frontend/src/components/topology-editor/edges/BundledEdge.tsx`,
`frontend/src/components/topology-editor/ElementAttachDialog.tsx`.

**Rules.**

- **TOPO-WIRE-1.** Drawing a line between two devices opens the wiring dialog; the Quick
  connect toggle opens the one-pair popover instead, which links back to the dialog. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`TopologyEditorPage`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorWiring.test.tsx` (`drawing a line opens the full wiring dialog by default (primary post-draw surface)`, `toggling Quick connect opens the compact popover instead, with an escalation link back to the full dialog`)
- **TOPO-WIRE-2.** Confirming N lines adds N edges in one store update, each carrying its
  port ids, port names, layer, and cabled flag; edges are never merged or deduplicated. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleWiringConfirm`, `handleConnectionConfirm`); `frontend/src/stores/topologyStore.ts` (`useTopologyStore`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorWiring.test.tsx` (`confirming N lines in the wiring dialog adds N edges to the store in a single commit`, `a second quick-connect line for an already-wired pair is not silently dropped (review item 5)`); `frontend/src/test/components/WiringDialog.test.tsx` (`confirm calls onConfirm once with every session line's port ids, names, and layer`)
- **TOPO-WIRE-3.** A port with any registered cable is selectable and tagged CABLED; a
  port already wired on the canvas, to any device, is unavailable; a port used earlier in
  the session cannot take a second line. \
  Enforced in: `frontend/src/components/topology-editor/wiring/usePortAvailability.ts` (`usePortAvailability`); `frontend/src/components/topology-editor/wiring/portAvailability.ts` (`computeCabledNames`); `frontend/src/pages/TopologyEditorPage.tsx` (`existingWiredPortIds`) \
  Pinned by: `frontend/src/test/components/WiringDialog.test.tsx` (`a cabled port is fully wireable (not blocked): the fabric shape lands portsCabled true`, `existing canvas-wired ports render WIRED and are unavailable, with a distinct tooltip and error (review item 8)`, `clicking a session-wired port a second time shows the session error and does not add another line`); `frontend/src/test/pages/TopologyEditorWiring.test.tsx` (`a port wired to a THIRD device is canvas-wired too, regardless of who the other end is (review round 3 item 5)`)
- **TOPO-WIRE-4.** While the cabling query loads, ports are inert and confirm is
  disabled. \
  Enforced in: `frontend/src/components/topology-editor/WiringDialog.tsx` (`WiringDialog`); `frontend/src/components/topology-editor/QuickConnectPopover.tsx` (`QuickConnectPopover`) \
  Pinned by: `frontend/src/test/components/WiringDialog.test.tsx` (`while connections are loading, ports are inert, 1:1 is disabled, and confirm stays disabled`); `frontend/src/test/components/QuickConnectPopover.test.tsx` (`while connections are loading, both selects are disabled and Connect stays disabled`)
- **TOPO-WIRE-5.** Connect 1:1 in order pairs the free ports each column's filter leaves
  visible, top to bottom, skipping session-wired and canvas-wired ports, and says so when
  nothing can pair. \
  Enforced in: `frontend/src/components/topology-editor/WiringDialog.tsx` (`WiringDialog`); `frontend/src/components/topology-editor/wiring/filterPorts.ts` (`filterPorts`) \
  Pinned by: `frontend/src/test/components/WiringDialog.test.tsx` (`connect 1:1 in order pairs free ports top to bottom, skipping session-wired`, `connect 1:1 in order respects each column's active filter (review item 7)`, `connect 1:1 in order shows the blunt error when nothing can pair`, `existing canvas-wired ports are excluded from Connect 1:1 in order`)
- **TOPO-WIRE-6.** Two or more edges between one unordered device pair render as one
  bundled line with a count; the store keeps every edge, and proposal and diff-overlay
  edges are never bundled. \
  Enforced in: `frontend/src/components/topology-editor/edges/groupEdgesForRender.ts` (`groupEdgesForRender`, `isAnnotationEdge`) \
  Pinned by: `frontend/src/test/components/BundledEdge.test.tsx` (`two or more edges sharing an unordered device pair collapse into one bundledEdge`, `does not mutate or drop the underlying edges: grouping is render-only`, `keeps proposal edges and edges for other device pairs rendered individually`); `frontend/src/test/components/groupEdgesForRender.test.tsx` (`excludes a diff overlay edge from bundling even when it shares a pair with another diff edge`)
- **TOPO-WIRE-7.** A bundle is selected when any member is, turns red when any member is
  invalid, offers per-member delete except in read-only mode, and a remove on the bundle
  removes every member while a replace is not expanded. \
  Enforced in: `frontend/src/components/topology-editor/edges/groupEdgesForRender.ts` (`groupEdgesForRender`); `frontend/src/components/topology-editor/edges/BundledEdge.tsx` (`BundledEdge`); `frontend/src/pages/TopologyEditorPage.tsx` (`handleEdgesChange`) \
  Pinned by: `frontend/src/test/components/BundledEdge.test.tsx` (`projects selected true onto the bundle when ANY member is selected, so React Flow's controlled reconciliation can see it (review item 1)`, `renders red the moment any member is invalid (uncabled port), never averaging it away (review item 4b)`, `hides the per-member delete control in read-only mode, closing the store-bypass (review round 3 item 3)`); `frontend/src/test/pages/TopologyEditorWiring.test.tsx` (`a remove change on a bundled edge id expands to every member id before reaching the store`, `a replace change targeting a bundle id is NOT expanded, so the synthetic bundle shape never gets written into a real edge slot (review round 3 item 11)`)
- **TOPO-WIRE-8.** The attach dialog lets a user pick several ports of the device, never a
  port already wired on the canvas, and adds one attachment edge per port with the device
  as source and the port in `source_port_name`; an element-first line is turned around
  when both ends exist. \
  Enforced in: `frontend/src/components/topology-editor/ElementAttachDialog.tsx` (`ElementAttachDialog`); `frontend/src/pages/TopologyEditorPage.tsx` (`handleElementAttachConfirm`); `frontend/src/stores/topologyStore.ts` (`normalizeElementDirection`) \
  Pinned by: `frontend/src/test/components/ElementAttachDialog.test.tsx` (`multi-select: clicking multiple ports selects all of them (not an arm-then-pair single selection)`, `a port already wired on the canvas (existingWiredPortIds) is unavailable`, `Confirm emits every selected port as one attach selection in a single onConfirm call`); `frontend/src/test/pages/TopologyEditorPage.ElementAttachAndDrop.test.tsx` (`confirm adds one attachment edge per selected port, device as source, and clears the pending state`); `frontend/src/test/stores/topologyStore.test.ts` (`addEnrichedEdge normalizes an element-first connection so the device becomes source`)

**Out of scope.** The admin multi-connect dialog, which shares the port primitives but
creates real cables, is section 8.19. The shared-shell refactor is issue #539.

### 8.19 The admin cabling page

**What it does.** An admin records cables by staging lines between two devices' ports and
creating them in one batch, or one pair at a time.

**Surfaces.** `frontend/src/pages/admin/ConnectionsPage.tsx`,
`frontend/src/components/admin/connections/MultiConnectDialog.tsx`,
`frontend/src/components/admin/connections/bulkStaging.ts`; routes
`POST /connections/bulk`, `POST /connections`, `DELETE /connections/{id}`.

**Rules.**

- **TOPO-ADMINUI-1.** The page creates through the multi-line dialog by default, with a
  toggle back to the single-pair form. \
  Enforced in: `frontend/src/pages/admin/ConnectionsPage.tsx` (`ConnectionsPage`) \
  Pinned by: `frontend/src/test/pages/ConnectionsPage.test.tsx` (`defaults to the multi-connection dialog`, `the Single toggle switches the create button back to the single-pair modal`)
- **TOPO-ADMINUI-2.** A port that already has a cable is tagged but stays selectable, and
  a line duplicating an existing connection is flagged but can still be created. \
  Enforced in: `frontend/src/components/admin/connections/MultiConnectDialog.tsx` (`MultiConnectDialog`); `frontend/src/components/admin/connections/bulkStaging.ts` (`existingPairKeys`) \
  Pinned by: `frontend/src/test/components/MultiConnectDialog.test.tsx` (`an already-cabled port is flagged CABLED but stays fully selectable (warn, never block)`, `a pair duplicating an existing connection is flagged, and is still confirmable`)
- **TOPO-ADMINUI-3.** With the same device on both sides, Connect 1:1 in order pairs
  adjacent free ports, leaves an odd one unpaired, and needs at least two free ports. \
  Enforced in: `frontend/src/components/admin/connections/MultiConnectDialog.tsx` (`MultiConnectDialog`) \
  Pinned by: `frontend/src/test/components/MultiConnectDialog.test.tsx` (`Connect 1:1 in order pairs adjacent free ports when the same device is picked on both sides`, `Connect 1:1 in order leaves an odd free port unpaired on a same-device pick`, `Connect 1:1 in order requires at least two free ports on a same-device pick`)
- **TOPO-ADMINUI-4.** A batch over 200 lines is refused before sending. \
  Enforced in: `frontend/src/components/admin/connections/MultiConnectDialog.tsx` (`BULK_CONNECTION_LIMIT`) \
  Pinned by: `frontend/src/test/components/MultiConnectDialog.test.tsx` (`refuses to submit a batch past the server cap instead of letting the whole batch fail`)
- **TOPO-ADMINUI-5.** After a batch, created lines leave the staging list, rejected lines
  stay with the server's reason, and a line with no answer row stays staged. \
  Enforced in: `frontend/src/components/admin/connections/bulkStaging.ts` (`applyBulkResult`) \
  Pinned by: `frontend/src/test/components/bulkStaging.test.ts` (`keeps only the rejected line, carrying the server reason`, `keeps a line whose index has NO row: an absent row is not evidence of creation`); `frontend/src/test/components/MultiConnectDialog.test.tsx` (`PARTIAL SUCCESS is never reported as success: rejected rows stay staged with their reason`)

**Out of scope.** Inventory's device and port pages.

### 8.20 Editing a live reservation's fork in the editor

**What it does.** The owner of a live reservation opens its fork in the editor, edits the
drawing (saved as a draft as they go), and commits it to rewire the reservation. The
history panel lets them preview, compare, and restore earlier versions. An ended
reservation's fork opens read-only.

**Surfaces.** `frontend/src/pages/TopologyEditorPage.tsx` (live-edit mode),
`frontend/src/components/topology-editor/LiveEditBar.tsx`,
`frontend/src/components/topology-editor/ForkHistoryPanel.tsx`,
`frontend/src/hooks/useForkAutosave.ts`, `frontend/src/hooks/useForkVersionPreview.ts`,
`frontend/src/lib/forkDiff.ts`; the reservations fork routes (`reservations.md`, section
5).

**Rules.**

- **TOPO-UIFORK-1.** Live edit loads the fork's canvas, never the parent's, and Commit
  stays disabled, reading `Loading fork...`, until it has loaded. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleCommitToReservation`); `frontend/src/components/topology-editor/LiveEditBar.tsx` (`LiveEditBar`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorForkMode.test.tsx` (`loads the reservation fork canvas, not the parent topology canvas`, `disables Commit and reads 'Loading fork...' until the fork hydrates; a click during that window issues no fork save or device PATCH`)
- **TOPO-UIFORK-2.** Commit is blocked while any line is invalid; it adds newly drawn
  devices to the reservation before saving the fork (a failure there blocks the save),
  removes dropped devices only after the save succeeds, and never writes the parent
  topology. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleCommitToReservation`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorForkMode.test.tsx` (`disables the Commit button with the invalid-edge count when an edge has no physical path, and never calls the fork save`, `PATCH-adds a newly drawn device to the reservation BEFORE saving the fork (issue #701)`, `a failed device-set PATCH blocks the fork save entirely (issue #701)`, `PATCH-removes a device from the reservation only AFTER the fork save succeeds (issue #701)`, `commit calls the fork save and the device PATCH, never the parent topology PUT`)
- **TOPO-UIFORK-3.** A port-claim 409 opens the conflict dialog and keeps the drawing; a
  membership 409 names the devices; routing refusals toast their count, the parser's
  message, or the inventory outage; any other string detail is shown as sent. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleCommitToReservation`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorForkMode.test.tsx` (`a structured 409 port-claim conflict opens the conflict dialog and keeps the drawing`, `a fork_device_not_member 409 names the offending devices in plain words (issue #701)`, `an l3_intent_invalid 409 toasts the problem count`, `an l3_intent_malformed 422 toasts the parser's message`, `an l3_config_unavailable 503 toasts that inventory could not be reached`, `a plain 409 with a string detail toasts that string verbatim`)
- **TOPO-UIFORK-4.** The draft is sent as a canvas PUT 2000 ms after the last edit, never
  for the canvas as loaded, and flushed when the page closes; a read-only fork never
  autosaves. \
  Enforced in: `frontend/src/hooks/useForkAutosave.ts` (`useForkAutosave`, `FORK_AUTOSAVE_DELAY_MS`) \
  Pinned by: `frontend/src/test/hooks/useForkAutosave.test.tsx` (`does not autosave the freshly loaded canvas (baseline is not an edit)`, `debounces rapid edits into a single PUT of the final canvas`, `flushes an unsaved draft on unmount (navigate-away)`, `stays inert when disabled (read-only fork)`)
- **TOPO-UIFORK-5.** An archived fork, or a fork while a version is previewed, renders
  read-only. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`TopologyEditorPage`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorForkMode.test.tsx` (`renders read-only when the fork is archived (ended reservation as-built)`, `previewing a fork version shows the history banner and locks editing (issue #622)`)
- **TOPO-UIFORK-6.** Restore is shown only for an `ACTIVE` reservation and asks for
  confirmation; a draft restored and not yet saved shows a chip from the fork's restore
  marker. \
  Enforced in: `frontend/src/components/topology-editor/ForkHistoryPanel.tsx` (`ForkHistoryPanel`) \
  Pinned by: `frontend/src/test/components/ForkHistoryPanel.test.tsx` (`renders Restore only for an ACTIVE reservation`, `Restore opens a confirm dialog and only calls restoreVersion after confirming`, `shows a draft-restored chip derived from draft_restored_from_id, not a new version row`)
- **TOPO-UIFORK-7.** Preview flushes the autosave, loads the version as a hydrated ghost
  canvas, and exit restores the live draft; closing the panel exits the preview, and a
  failed fetch exits with a toast. \
  Enforced in: `frontend/src/hooks/useForkVersionPreview.ts` (`useForkVersionPreview`) \
  Pinned by: `frontend/src/test/hooks/useForkVersionPreview.test.tsx` (`startPreview flushes the autosave before hijacking the canvas store`, `preview loads the fetched version's canvas, ghosted as a proposal, hydrated first`, `exit restores the preserved live draft and resets to idle`, `a failed preview fetch exits back to idle and shows an error toast`); `frontend/src/test/pages/TopologyEditorForkMode.test.tsx` (`closing the history panel while previewing also exits the preview (issue #622 review)`)
- **TOPO-UIFORK-8.** The fork diff matches lines by source node, target node, and both
  port names (not the edge id) with multiset counting, and compares routes as raw text. \
  Enforced in: `frontend/src/lib/forkDiff.ts` (`edgeIdentityKey`, `diffForkCanvases`) \
  Pinned by: `frontend/src/test/lib/forkDiff.test.ts` (`does not report churn for the same wire re-drawn under a new edge id`, `uses multiset semantics for same-key edges, not set membership (coordinator review)`, `differs when the port names differ`, `reports a change when the destination text differs, even if it would canonicalize to the same network`)
- **TOPO-UIFORK-9.** A successful commit shows the version and the released, built, and
  unchanged counts, with the skipped attachment count only when above zero. \
  Enforced in: `frontend/src/components/topology-editor/ForkSaveResultToast.tsx` (`ForkSaveResultToast`) \
  Pinned by: `frontend/src/test/components/ForkSaveResultToast.test.tsx` (`shows the version and released/built/unchanged counts`, `omits the element attachments clause when the count is undefined or zero`, `shows the element attachments clause when the count is greater than zero`)

**Out of scope.** Who may open the fork, and the Edit topology entry point, are
`reservations.md` (RES-FORK-4, RES-FORK-19). The Wiring tab is
`provisioning-and-wiring.md`.

### 8.21 The routing panel

**What it does.** Selecting one Layer 3 switch on the canvas opens a table of its
routes, which a user edits, imports from the switch's configuration, and sees validation
problems against.

**Surfaces.** `frontend/src/components/topology-editor/RoutingPanel.tsx`,
`frontend/src/lib/l3.ts`, `frontend/src/lib/canvasNodes.ts`,
`frontend/src/components/topology-editor/nodes/DeviceNode.tsx`.

**Rules.**

- **TOPO-UIL3-1.** The panel opens only when exactly one Layer 3 switch node is selected. \
  Enforced in: `frontend/src/lib/l3.ts` (`selectRoutingPanelNode`, `isLayer3Switch`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.L3Routing.test.tsx` (`opens the Routing panel only for a single-selected Layer 3 Switch node, and shows the per-row reason line (E3/E5)`)
- **TOPO-UIL3-2.** Row edits commit on blur or Enter, trimmed, with a blank optional field
  stored as null, at most 64 characters; an empty required field is not written; removing
  the last row removes `data.l3` entirely. \
  Enforced in: `frontend/src/components/topology-editor/RoutingPanel.tsx` (`RoutingPanel`); `frontend/src/stores/topologyStore.ts` (`setNodeL3Routes`) \
  Pinned by: `frontend/src/test/components/RoutingPanel.test.tsx` (`does not commit to the store until blur`, `commits on Enter`, `blanking next hop and committing stores null, not an empty string`, `clearing destination then blurring does not write an empty string to the store, and shows an inline error`, `refuses a 65th character`, `removing the only row writes an empty list, which the store turns into no intent`)
- **TOPO-UIL3-3.** Import fetches the switch's config version only when clicked, replaces
  an existing table only after confirmation, and changes nothing for an empty or missing
  route list or a failed fetch. \
  Enforced in: `frontend/src/components/topology-editor/RoutingPanel.tsx` (`RoutingPanel`) \
  Pinned by: `frontend/src/test/components/RoutingPanel.test.tsx` (`fetches the config version lazily: no detail query until Import is clicked`, `asks for confirmation and REPLACES the table when rows already exist`, `an empty routes list toasts and changes nothing (review fix F3)`, `an errored fetch toasts the error and changes nothing (review fix F3)`)
- **TOPO-UIL3-4.** Problems attach to the row with the same field values, not the
  original index; a duplicate shows amber and never counts as blocking. \
  Enforced in: `frontend/src/lib/l3.ts` (`matchRouteProblems`, `isBlockingRouteProblem`) \
  Pinned by: `frontend/src/test/components/RoutingPanel.test.tsx` (`matches a per-row reason to the row with the same field values, not by original position`, `renders l3_duplicate_route as an amber informational line, not red`); `frontend/src/test/lib/l3.test.ts` (`ignores the informational duplicate reason`)
- **TOPO-UIL3-5.** A malformed `data.l3` shows a repair line with Remove all instead of
  throwing, and `{routes: []}` counts as no intent. \
  Enforced in: `frontend/src/lib/canvasNodes.ts` (`l3RoutesOf`, `l3IsMalformed`) \
  Pinned by: `frontend/src/test/components/RoutingPanel.test.tsx` (`shows a repair line and Remove all for {} rather than throwing`, `Remove all clears the malformed data.l3 entirely`, `{routes: []} counts as no intent, not malformed (the documented case)`)
- **TOPO-UIL3-6.** A device node shows a route count badge, red when the last validation
  found a problem on it. \
  Enforced in: `frontend/src/components/topology-editor/nodes/DeviceNode.tsx` (`DeviceNode`) \
  Pinned by: `frontend/src/test/components/DeviceNode.test.tsx` (`shows a singular-count, non-red badge for one route with no validation problem`, `shows the red variant when l3ValidationInvalid is set`)

**Out of scope.** Switch configuration versions are `device-configuration.md`.

## 9. Errors

FastAPI validation errors (422) carry `detail` as a list of `{loc, msg, type}`; every
other error carries `detail` as a string or the object shown.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 401 | `Not authenticated`, `Could not validate credentials`, or `Invalid subject in token` | a user route with no, a bad, or a subject-less bearer token | TOPO-CONN-1 |
| 403 | `Admin or superadmin role required` | create, bulk create, or delete a connection as a non-admin | TOPO-CONN-1, TOPO-CONN-10 |
| 403 | `Invalid internal token` | any internal route with a wrong token, or with no token configured | TOPO-FORK-1, TOPO-CONNINT-1, TOPO-FABRIC-1, TOPO-VAL-12 |
| 403 | `Not authorized to update this topology` | PUT by a non-creator non-admin | TOPO-EDIT-1 |
| 403 | `Not authorized to delete this topology` | DELETE by a non-creator non-admin | TOPO-DEL-1 |
| 403 | `Not authorized to validate this topology` | user validate by a non-creator non-admin | TOPO-VAL-9 |
| 403 | `Not authorized to modify this topology` | version restore by a non-creator non-admin | TOPO-VER-4 |
| 403 | `Not authorized to modify this template` or `Not authorized to delete this template` | template update or delete by a non-creator non-admin | TOPO-TMPL-4 |
| 404 | `Connection not found` | read or delete an unknown connection; a non-admin's read of a connection with no visible end | TOPO-CONN-9, TOPO-CONN-10 |
| 404 | `Device not found` | a non-admin's pathfind naming a hidden or unknown device | TOPO-PATH-8 |
| 404 | `Topology not found` | any topology, version, validate, clone, or from-topology route on an unknown topology | TOPO-CRUD-2, TOPO-EDIT-1, TOPO-DEL-1, TOPO-VER-1, TOPO-VAL-9, TOPO-CLONE-1, TOPO-TMPL-5 |
| 404 | `Version not found` | a topology version of another topology, or a fork version of another fork | TOPO-VER-2, TOPO-FORK-10 |
| 404 | `Template not found` | any template route on an unknown template | TOPO-TMPL-1, TOPO-TMPL-4, TOPO-TMPL-8 |
| 404 | `Fork not found` | any fork route but create, archive, listing, by-device, and batch, on a reservation with no fork | TOPO-FORK-9 |
| 409 | `{message, reservations: [{id, status, end_time}]}` | topology PUT blocked by another user's live reservation | TOPO-EDIT-4 |
| 409 | `{message, reservations: [...]}` with message `Topology has active reservations; restore blocked` | topology version restore while any live reservation references it | TOPO-VER-5 |
| 409 | `{"error": "topology_in_use", "reservation_ids": [...]}` | delete while a live reservation references the topology | TOPO-DEL-2 |
| 409 | `Template name '<name>' already exists` | template create, update, or from-topology with a taken name | TOPO-TMPL-2, TOPO-TMPL-4, TOPO-TMPL-7 |
| 409 | `Fork is archived and cannot be edited` | canvas PUT, save, restore, or prune on an archived fork | TOPO-FSTATE-3, TOPO-FSTATE-4 |
| 409 | `{"error": "fork_device_not_member", "device_ids": [...]}` | fork create or save naming a device outside the reservation | TOPO-FORK-3, TOPO-FORK-13 |
| 409 | `{message, conflicts: [{reservation_id, device_id, port}]}` | fork create or save claiming a port another active fork holds | TOPO-CLAIM-1 |
| 409 | `{"error": "l3_intent_invalid", "invalid_routes": [...]}` | fork save whose changed routing intent fails validation | TOPO-FORK-21 |
| 422 | `Cannot connect a port to itself` | a connection from a port to itself; per row in bulk | TOPO-CONN-3 |
| 422 | `Device <id> does not exist` | inventory answers 404 for a device being cabled; per row in bulk | TOPO-BOUND-2 |
| 422 | `Devices belong to different device groups and share none; cross-group cabling is disabled.` | cabling across disjoint device groups; per row in bulk | TOPO-BOUND-1 |
| 422 | `missing assignment for role '<role>'` | instantiate without a device for a role | TOPO-TMPL-8 |
| 422 | `Invalid JSON: <error>`, `JSON import must be a list of topologies or an object with an 'items' list`, `'items' must be a list` | an unparseable or misshapen JSON import | TOPO-BULK-7 |
| 422 | `{"error": "l3_intent_malformed", "node_id", "message"}` | fork save with malformed routing intent | TOPO-FORK-14 |
| 422 | validation list | a schema bound: name lengths, bulk item count, pair count, unknown sort or owner value, missing `member_device_ids`, missing internal token header, devices batch size | TOPO-CONN-2, TOPO-CONN-12, TOPO-PATH-7, TOPO-LIST-4, TOPO-CRUD-1, TOPO-FORK-3, TOPO-FORK-13, TOPO-DEVB-1 |
| 500 | `internal: missing Authorization header while resolving device visibility` | a non-admin request reaching a visibility-filtered route with no header (only a test harness does this) | TOPO-VIS-1 |
| 503 | the route's own visibility wording, for example `Could not verify device visibility; connections were not returned. Retry the request.` | a non-admin's visibility lookup failed | TOPO-CONN-7, TOPO-CONN-9, TOPO-FORK-25, TOPO-PATH-11, TOPO-VAL-11, TOPO-BULK-11 |
| 503 | `Could not verify device-group membership for one or more devices in this batch; no connections were created. Retry the request.` | bulk create with an unverifiable device | TOPO-BOUND-4 |
| 503 | `Could not verify topology is not in use` | delete guard could not read reservations | TOPO-DEL-3 |
| 503 | `{"error": "l3_config_unavailable"}` | the routing pass could not read inventory in time | TOPO-L3-11, TOPO-BULK-13 |
| 503 | `could not resolve device names via inventory: <error>` | import name resolution failed | TOPO-BULK-9 |

Import reports a refused row inside a 200 report, never as an error status, except the
503s above.

## 10. Interactions with other services

Calls into this area are in section 7. Cabling publishes no event.

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out | inventory | `GET /device-groups/device/{id}` (caller's JWT, 5 s) | device-group boundary and device existence on cabling | Single create: fail open, the connection is created (TOPO-BOUND-3); a 404 refuses with 422. Bulk: fail closed, 503, nothing created (TOPO-BOUND-4) |
| Out | inventory | `GET /device-groups/visible-devices?user_id` (caller's JWT, 5 s) | non-admin visibility for the connection list, pathfind, validate, import | Fail closed: 503, nothing returned (TOPO-VIS-1) |
| Out | inventory | `POST /devices/resolve-by-name` (internal token, 10 s) | import name resolution | Fail closed: 503 for the whole import (TOPO-BULK-9) |
| Out | inventory | `POST /internal/devices/batch` (internal token, 4 s per call) | device type for the routing pass | Fail closed: 503 `l3_config_unavailable` (TOPO-L3-11) |
| Out | inventory | `GET /devices/{id}/config-versions/latest/internal` (internal token, 4 s per call) | latest switch config for the routing pass | A 404 means unconfigured; anything else fails closed with 503 (TOPO-L3-7, TOPO-L3-11) |
| Out | reservations | `GET /internal/by-topology/{topology_id}` (internal token, 5 s) | edit lock on PUT, version restore, and import | Fail open: treated as no reservation (TOPO-EDIT-6) |
| Out | reservations | `GET /internal/by-topology/{topology_id}` (internal token, 5 s) | delete guard | Fail closed: 503, nothing deleted (TOPO-DEL-3) |

## 11. Configuration

Cabling-service environment variables; [ENV_VARS.md](../ENV_VARS.md) has the full list.

| Setting | Default | Effect |
|---|---|---|
| `ENFORCE_DEVICE_GROUP_BOUNDARIES` | `true` | Turns the device-group boundary and the existence check on cabling on or off (TOPO-BOUND-6) |
| `INTERNAL_API_TOKEN` | empty | Shared service token. Empty makes every internal route refuse with 403 and every outbound internal call send an empty token |
| `INVENTORY_SERVICE_URL` | `http://inventory:8000` | Inventory base URL for every inventory call in section 10 |
| `RESERVATIONS_SERVICE_URL` | `http://reservations:8000` | Reservations base URL for the edit lock and the delete guard |
| `SECRET_KEY`, `ALGORITHM` | required, `HS256` | JWT verification |
| `CORS_ORIGINS` | empty | Allowed browser origins |
| `DB_SCHEMA` | `cabling` | Postgres schema |
| `LOG_LEVEL` | `INFO` | Log level |

Fixed in code, not configurable: 5 version-allocation attempts; 256 enumerated paths per
query; 2000 pairs per pathfind batch; 200 items per bulk connection request; 500
reservation ids per devices batch; 10 connection ids in the by-device sample; active fork
listing `limit` up to 1000 (default 200); 5 s for inventory group and visibility calls and
for the reservations lookup; 10 s for name resolution; 4 s per routing-pass inventory call,
12 s for the whole pass, 500 devices per type batch, 8 concurrent config reads; 64
characters per route field; editor draft autosave after 2000 ms.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/cabling/tests/` (in-memory SQLite, direct handler calls in `test_route_handlers_direct.py`); `services/common/tests/test_l3_validation.py`; `frontend/src/test/lib/`, `frontend/src/test/components/`, `frontend/src/test/stores/topologyStore.test.ts`, `frontend/src/test/hooks/` | Advisory locks and `FOR UPDATE` are no-ops on SQLite, so concurrency is proven only by the live-Postgres suites |
| Functional (through the service API) | `services/cabling/tests/test_topologies.py`, `test_connections.py`, `test_connections_bulk.py`, `test_pathfind.py`, `test_bulk.py`, `test_forks.py`, `test_fork_versions.py`, `test_fork_l3_routes.py`, `test_topology_list_controls.py`, `test_visibility_oracle.py` (httpx against the app); the live-Postgres suites `test_fork_restore_save_race_live_pg.py`, `test_fork_port_claim_race_live_pg.py`, `test_l3_route_key_width_live_pg.py`, `test_topology_list_order_live_pg.py` | The live suites run in the `make master` and `make everything` gates |
| Integration (running stack) | `tests/integration/test_pathfinding.py`, `test_cabling_group_boundary.py`, `test_connection_device_existence.py`, `test_bulk_import_export.py`, `test_topology_validate_gate.py`, `test_topology_delete_guard.py`, `test_topology_list_controls.py`, `test_reservation_fork_flow.py`, `test_fork_save_port_resolution.py`, `test_live_edit_topology.py`, `test_l3_intent_validate_and_fork.py`, `test_template_identity.py`, `test_device_group_visibility.py` | Advisory job on pull requests; nightly |
| Stress and load | `tests/load/locustfile.py` (`InventoryBrowser` topology list with search and sort, `BulkExporter` export, `RoutedTopologyValidator` create, PUT, validate, delete, `BulkConnectionAdmin` bulk create and delete) | No load test covers pathfind batch, fork save, import, or the delete guard |
| Browser end-to-end | `tests/e2e/test_topology_editor.py`, `test_topologies_list_playwright.py`, `test_topology_clone.py`, `test_topology_delete_in_use_playwright.py`, `test_topology_validator.py`, `test_wiring_dialog_playwright.py`, `test_network_elements_playwright.py`, `test_l3_routing_playwright.py`, `test_fork_live_edit.py`, `test_live_edit_reservation_topology.py`, `test_topology_templates_ui.py`, `test_bulk_import_export.py`, `test_connections_playwright.py`, `test_connections_bulk_playwright.py` | Nightly and in the `make master` and `make everything` gates, not per pull request |

Not run for this document: nothing was checked against a running stack. The
live-Postgres, integration, load, and browser suites were read, not run. The unit suite of
`tests/unit/` was run. Four behaviors were confirmed by a throwaway probe against the
cabling app on SQLite (not committed): TOPO-TMPL-3 (an empty and a 300-character template
name are accepted; Postgres enforces 255 at the column), TOPO-TMPL-6, TOPO-TMPL-7, and
TOPO-BULK-4.

## 13. Known limits and gaps

### Open defects

- #989 (TOPO-UI-2): the editor crashes on a stored node with no device placed between
  two device nodes, as in the seeded "BROKEN - Half-Wired Chain" topology.
- #1007 (TOPO-FORK-18): validation now reports an unresolvable port-constrained edge,
  but the fork save's own answer still does not name a skipped constrained edge.

### Limits by decision

- Duplicate, reverse-duplicate, and second-cable-on-a-port connections are accepted, and
  ports are not checked against inventory (TOPO-CONN-4). Recorded in the docstring of
  `create_connections_bulk`, the "warn, never block" comment in `MultiConnectDialog.tsx`,
  and the comment on `BulkConnectionAdmin` in `tests/load/locustfile.py`.
- Single create fails open on an unverifiable device group while bulk create fails
  closed (TOPO-BOUND-3, TOPO-BOUND-4). Recorded in the docstrings of
  `_enforce_device_group_boundary` and `_resolve_group_cache_for_batch` (issue #392).
- The device-group boundary is checked at create only; existing connections are never
  re-validated. Recorded in the comment on `enforce_device_group_boundaries` in
  `services/cabling/app/config.py` and in [ENV_VARS.md](../ENV_VARS.md).
- Every role reads every topology, canvas included (TOPO-LIST-1, TOPO-CRUD-2); export
  and clone follow from that. Recorded in [ROLES.md](../ROLES.md), Get a topology
  (issue #763).
- The edit lock fails open while the delete guard fails closed on the same reservations
  answer (TOPO-EDIT-6, TOPO-DEL-3). Recorded in `reservation_guard.py` and
  [ROLES.md](../ROLES.md), Delete a topology.
- A reservation created between the delete guard's check and the delete's commit can
  still reference a deleted topology. Recorded as a known limit in `reservation_guard.py`
  and [ROLES.md](../ROLES.md).
- Topology version restore is blocked for every caller, owner and admin included, while
  any live reservation references the topology, unlike the PUT lock (TOPO-VER-5).
  Recorded in [USER_GUIDE.md](../USER_GUIDE.md) and [TOPOLOGY_EDITOR.md](../TOPOLOGY_EDITOR.md).
- Every fork hop is recorded at layer `L1`; the drawn layer is a canvas annotation only
  (TOPO-FORK-16). Recorded in ADR 0009 (option C), issue #531, and the module docstring
  of `fork_save_service.py`.
- Fork create never judges routing intent (TOPO-FORK-6), and a wiring-only save with
  unchanged intent does not re-check that each switch is still attached (TOPO-FORK-21).
  Recorded in ADR 0014's phase 1 review-fixes amendment and the docstrings of
  `fork_service.py` and `l3_intent_changed`.
- The fork canvas draft is last-writer-wins; only the restore marker is protected by the
  row lock (TOPO-MARKER-3). Recorded in [ARCHITECTURE.md](../ARCHITECTURE.md) and the
  docstring of `update_fork_canvas_internal` (issue #626).
- Deleting a cable that live fork wiring references is not refused (TOPO-CONN-11).
  Recorded in ADR 0007 (Decision 5): a graph change under a live reservation is an
  accepted failure mode, and the recovery is a re-save.
- CSV import and export do not carry nodes with no edge or network elements; JSON is the
  lossless format (TOPO-BULK-3). Recorded in [BULK_IMPORT_EXPORT.md](../BULK_IMPORT_EXPORT.md).

### Rules with no test

- TOPO-CONN-11: deleting a connection that a live fork's wiring uses.
- TOPO-VAL-14: `l3=0` on the internal validate skipping the routing pass (reservations
  pins that it sends `l3=0`; nothing in cabling pins the skip).
- TOPO-STRIP-4: a PUT differing only in a non-allowlisted device key appending no
  version.
- TOPO-TMPL-9: instantiate not checking assigned device ids.
- TOPO-UI-2: the device-less node crash (issue #989 asks for the test).
