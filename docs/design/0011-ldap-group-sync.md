# Decision: Directory Group Mapping and Sync, Issue #38

Status: Draft 2026-08-11, awaiting approval. The four decision points below
(pre-provisioning, reactivation provenance, deactivation fail-safety, audit
persistence) were resolved with vendra on 2026-08-11 to the options recorded
here. Numbered 0011 because 0008 is claimed by an unmerged soak-test draft.
No code in this doc. Context verified against the live HERD-public tree on
2026-08-11.

## Context

HERD's LDAP mode provisions a `User` row just-in-time on first bind but
deliberately does not mirror directory group membership; `docs/ROLES.md` and
`docs/ARCHITECTURE.md` both record the omission as a follow-up. Issue #38
asks for an admin-managed mapping from directory groups to HERD `UserGroup`
rows, interval-based reconciliation of membership, and deactivation of users
removed or disabled upstream.

Relevant existing fabric, verified:

- LDAP client: `services/auth/app/services/ldap_service.py` implements
  service-account bind, user search, and user bind only. Zero group-related
  settings keys exist (`services/auth/app/config.py:29-47`). All blocking
  ldap3 calls dispatch through `anyio.to_thread`.
- JIT provisioning: `_authenticate_ldap`
  (`services/auth/app/services/auth_service.py:200`) resolves the submitted
  username through `ldap_user_filter`, binds, and provisions by the
  directory's email attribute via `create_ldap_user` (`auth_source="ldap"`,
  `hashed_password=None`). Username collisions with local accounts are
  detected and refused.
- Groups: `UserGroup`/`GroupMember` in `services/auth/app/models/group.py`
  are a plain association keyed (group_id, user_id).
  `group_service.add_member` auto-removes the user from "Not Grouped";
  `remove_member` does NOT re-add to it (the invariant is one-directional).
  Applying sync changes through these existing operations reproduces manual
  admin behavior exactly.
- Deactivation enforcement exists end to end with no writer: `is_active` is
  checked on login, token issuance, and refresh
  (`services/auth/app/services/auth_service.py`), and in the auth
  dependencies and internal router. The sync becomes the first writer
  (issue #38 audit comment, 2026-07-31).
- Config bootstrap: the first-start config UI schema
  (`services/config/app/config_schema.py`, LDAP block) must carry the new
  keys or first-start deployments cannot configure the sync (same gap class
  as #37; issue audit comment).
- Background-loop precedent: `conversation_sweeper_loop`
  (`services/ai-orchestrator/app/tasks/conversation_sweeper.py:25`).
- Test infrastructure: the checked-in `infra/ldap-test/` directory (PR #504)
  boots seeded and hard-required in the master/everything gates, so group
  sync is live-testable hardware-free by adding group entries to its LDIF.

## Decision

### Mapping store keyed by directory DN

New table `ldap_group_mappings` in the `auth` schema (Alembic revision under
`services/auth/migrations/versions/`):

- id UUID PK
- group_dn Text, unique: the directory group's DN, the stable identity
- directory_name String, cached display name (the group's `cn`), refreshed
  on each sync
- herd_group_id UUID FK to `user_groups.id` ON DELETE CASCADE: deleting the
  HERD group deletes the mapping; the directory is never written
- created_by UUID, created_at timestamptz

Mapping creation validates the DN against the live directory (base-scope
search must find exactly one entry) and refuses unknown DNs with a 422.
Admin CRUD lives in a new router (`services/auth/app/routers/ldap_sync.py`),
gated admin-or-superadmin consistent with existing group management.

### Member resolution: group-side DN attribute

For each mapped group, the directory client reads the group entry's member
attribute (new setting `ldap_group_member_attribute`, default `member`,
values interpreted as DNs). This covers Active Directory groups and OpenLDAP
`groupOfNames`. Each member DN resolves to (email, username) via a
base-scope search on that DN retrieving `ldap_email_attribute` and
`ldap_username_attribute`, the same attributes the JIT path trusts. Entries
with no email are skipped with a warning (the JIT path refuses them too).
posixGroup `memberUid` semantics and nested-group flattening are out of
scope, per the issue.

### Reconciliation: set arithmetic through existing membership ops

Per mapped group, one pass computes:

- desired: the resolved directory members
- current: HERD `group_members` rows for the mapped group, restricted to
  `auth_source='ldap'` users

Adds are `desired - current`; removes are `current - desired`. Both apply
through `group_service.add_member` / `remove_member`, so the "Not Grouped"
invariant and cascade behavior match manual administration. Locally-created
accounts (`auth_source='local'`) in a mapped group are invisible to the
sync in both directions. The pass is idempotent: a second run against an
unchanged directory produces zero changes.

### Pre-provisioning (decided: yes)

A desired member with no HERD user row is provisioned during sync through
the existing `create_ldap_user` path (`auth_source="ldap"`, no password),
then added to the group. Directory and HERD membership mirror exactly, and
a user's device visibility is correct at first login rather than one sync
cycle later. A username collision with a local account is skipped with a
warning, matching the login-path behavior.

### Deactivation sweep: absence-proof plus circuit breaker (decided)

A separate per-run sweep walks all `auth_source='ldap'` users and searches
the directory for each by username through the existing `ldap_user_filter`
(the same key the login path uses):

- A search that ERRORS (bind failure, timeout, LDAP exception) proves
  nothing; the user is left untouched and the run is marked partial. Error
  is never absence.
- A search that SUCCEEDS with zero results marks the user
  directory-absent.
- Circuit breaker: if the proven-absent set exceeds
  `ldap_sync_deactivation_max_percent` (default 20) of swept users, the
  entire deactivation pass aborts, no user is deactivated, and the run is
  marked aborted with the reason. A misconfigured `ldap_user_base_dn` or
  filter edit therefore cannot mass-deactivate the deployment in one sweep.
- Below the threshold, each absent user gets `is_active=False` and
  `deactivated_by_sync=True` (new column on `users`, migration). The
  existing `is_active` checks then block login, token issuance, and refresh
  with no new enforcement code.

Deactivation is a flag flip, never a delete: reservation references by UUID
and audit history stay intact, per the issue.

### Reactivation: automatic only with sync provenance (decided)

A swept user who IS found in the directory, has `is_active=False`, and has
`deactivated_by_sync=True` is reactivated (`is_active=True`, provenance
flag cleared). A user an admin manually deactivated (provenance flag false)
is never touched: admin intent always outranks the directory. The manual
admin deactivation endpoint this feature adds (the first `is_active` writer
alongside the sync) always writes `deactivated_by_sync=False`.

### Audit: persisted sync-runs table (decided)

New table `ldap_sync_runs` in the `auth` schema:

- id UUID PK, started_at, finished_at
- trigger: `interval` or `manual`
- status: `success`, `partial` (some directory lookups errored), `aborted`
  (circuit breaker), `failed` (run-level exception)
- counts: users_provisioned, members_added, members_removed,
  users_deactivated, users_reactivated
- detail JSONB, capped (first N change records per category plus a
  truncation marker), and an error Text for aborted/failed runs

`POST /admin/ldap-sync/run` (sync now) executes a run inline and returns
its row; `GET /admin/ldap-sync/runs` lists recent runs. A retention sweep
in the same background loop prunes rows older than
`ldap_sync_runs_retention_days` (default 90).

### Scheduling

A background task started from auth's `main.py` lifespan, following the
conversation-sweeper pattern: run every `ldap_sync_interval_seconds`
(default 3600) when `ldap_group_sync_enabled` is true and
`auth_method == "ldap"`. A run already in progress is never overlapped (an
asyncio lock; sync-now returns 409 while a run holds it). All new settings
are added to `services/auth/app/config.py` AND the config service's
bootstrap schema.

New settings keys (all consulted only when `auth_method == "ldap"`):

- `ldap_group_sync_enabled` bool, default false (dark by default)
- `ldap_sync_interval_seconds` int, default 3600
- `ldap_group_member_attribute` str, default `member`
- `ldap_group_name_attribute` str, default `cn`
- `ldap_sync_deactivation_max_percent` int, default 20
- `ldap_sync_runs_retention_days` int, default 90

### Blast radius, documented

Group membership drives device visibility and ACL grant evaluation, so a
sync run can change what devices a user sees and what topologies and
reservations they can manage. `docs/ROLES.md` and `docs/ARCHITECTURE.md`
replace the "not mirrored in this release" note with the sync behavior and
this blast-radius statement. Role assignment never syncs; HERD stays the
authority for role, per the issue.

## Delivery phases

1. Directory client group support: group entry fetch, member enumeration,
   per-DN identity resolution in `ldap_service.py`; new settings; LDIF
   group fixtures in `infra/ldap-test/ldif/` (ou=groups, groupOfNames);
   live tests alongside `test_ldap_service_live.py`, gate-covered.
2. Mapping store: model, migration, admin CRUD router, DN validation.
3. Reconciler: membership set arithmetic, pre-provisioning, `ldap_sync_runs`
   table, sync-now endpoint with 409-on-overlap.
4. Deactivation and reactivation sweep: `deactivated_by_sync` migration,
   absence-proof rule, circuit breaker, manual admin activate/deactivate
   endpoints (the non-sync writer).
5. Interval loop, config-service bootstrap schema keys, `docs/ROLES.md` and
   `docs/ARCHITECTURE.md` updates, run-retention sweep.
6. Frontend admin surface: mappings CRUD, run history, sync-now button
   (every backend feature keeps a frontend path, the #397/#398 precedent).

## Testing

- Unit (`services/auth/tests/`): set-difference reconcile, pre-provisioning
  and collision skip, absence-proof rule (error never deactivates),
  circuit-breaker threshold (boundary-exact), provenance-gated
  reactivation, run-status vocabulary, mocked directory client throughout.
- Live LDAP (gate, hard-required): mapped group builds membership; upstream
  remove drops membership; member with no HERD row is provisioned; user
  deleted from the directory is deactivated and reactivated on restore;
  second run is a no-op. Fixtures ride `infra/ldap-test`.
- Contract: new admin endpoints land in the auth OpenAPI snapshot.
- Integration/e2e: the existing `HERD_INTEGRATION_LDAP`-gated stack suite
  gains a sync path only if the stack-mode LDAP flow is extended; otherwise
  live coverage stays in the auth suite by design (the gate runs it
  hard-required).

## Out of scope

Per issue #38: role mapping, push/event-driven sync, SCIM, nested-group
flattening, and SSO/OIDC claims as a sync source (the mapping store is
reusable for that later).
