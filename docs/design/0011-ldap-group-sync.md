# Decision: Directory Group Mapping and Sync, Issue #38

Status: Accepted 2026-08-12. Phase 1 (directory client) delivered in
PR #507, 2026-08-12; its adversarial review parked three design questions
on that PR for phases 2 (mapping validation of non-group DNs), 3
(out-of-base member policy), and 4 (presence-probe shape).
Seven decision points were
resolved with vendra on 2026-08-11: the original four (pre-provisioning,
reactivation provenance, deactivation fail-safety, audit persistence) plus
three raised by the same-day adversarial review of the first draft (mapping
identity survives renames via fail-closed dangling, disabled-account
detection via a configurable filter, and an absolute-count floor of 3 on
the deactivation circuit breaker). The review's other findings (fail-closed
group reconcile, email-keyed absence proof, decoupled deactivation knob,
apply-loop error handling, advisory-lock serialization, async sync-now) are
incorporated below. Numbered 0011 because 0008 is claimed by an unmerged
soak-test draft. No code in this doc. Context verified against the live
HERD-public tree on 2026-08-11.

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
  `hashed_password=None`). The HERD row is resolved by EMAIL on every later
  login; the stored username is written once at provisioning and never
  refreshed. Username collisions with local accounts are detected and
  refused, as is an email that belongs to a non-LDAP account.
- Deactivation enforcement exists with no writer: `is_active` is checked at
  login (local and LDAP paths), on refresh, in the auth dependencies, and
  in the internal router. Token issuance itself has no independent check;
  it is gated transitively by the login checks. No code writes
  `User.is_active` today; the sync and the manual admin endpoint this
  feature adds become its first writers (issue #38 audit comment,
  2026-07-31).
- Groups: `UserGroup`/`GroupMember` in `services/auth/app/models/group.py`
  are a plain association keyed (group_id, user_id).
  `group_service.add_member` auto-removes the user from "Not Grouped" and
  RAISES IntegrityError on a duplicate; `remove_member` does NOT re-add to
  "Not Grouped" (the invariant is one-directional). Each singular op
  commits individually; `bulk_add_members`/`bulk_remove_members` exist with
  one commit per batch. Applying sync changes through these existing
  operations reproduces manual admin behavior exactly.
- Config bootstrap: the first-start config UI schema
  (`services/config/app/config_schema.py`, LDAP block) must carry the new
  keys or first-start deployments cannot configure the sync (same gap class
  as #37; issue audit comment).
- Background-loop precedent: `conversation_sweeper_loop`
  (`services/ai-orchestrator/app/tasks/conversation_sweeper.py:25`). Its
  operation is idempotent expiry, so uncoordinated replicas are harmless;
  this feature does directory-wide writes, so it adds coordination (below).
- Fail-closed precedent: issue #460 / PR #476 made execution's
  `_fetch_fork_intended_wires` raise on an unreadable intended set instead
  of reconciling against empty and tearing down live state. The group
  reconcile here is the same shape and adopts the same rule.
- Test infrastructure: the checked-in `infra/ldap-test/` directory (PR #504)
  boots seeded and hard-required in the master/everything gates, so group
  sync is live-testable hardware-free by adding group entries to its LDIF.

## Decision

### Mapping store keyed by directory DN, dangling fail-closed (decided)

New table `ldap_group_mappings` in the `auth` schema (Alembic revision under
`services/auth/migrations/versions/`):

- id UUID PK
- group_dn Text, unique: the directory group's DN, the mapping key
- directory_name String, cached display name (the group's
  `ldap_group_name_attribute`), refreshed on each successful group fetch; a
  failed refresh keeps the last cached value
- herd_group_id UUID FK to `user_groups.id` ON DELETE CASCADE: deleting the
  HERD group deletes the mapping; the directory is never written
- created_by UUID, created_at timestamptz

A DN is not a stable directory identity (it changes on rename or OU move).
Decided handling: a mapped DN that no longer resolves is DANGLING, which is
fail-closed: zero membership changes are applied for that group, the run is
marked partial with the dangling DN in its detail, and the admin re-creates
the mapping after a rename. Stable-id tracking (entryUUID/objectGUID) is a
follow-up if rename churn proves real.

Mapping creation validates the DN against the live directory: a base-scope
search that finds no entry refuses with 422; a directory that cannot be
reached (bind failure, timeout, LDAP error) refuses with 503, the
distinguishes-not-found-from-cannot-ask convention (#337/#456). Admin CRUD
lives in a new router (`services/auth/app/routers/ldap_sync.py`), gated
admin-or-superadmin consistent with existing group management.

### Member resolution: group-side DN attribute

For each mapped group, the directory client reads the group entry's member
attribute (new setting `ldap_group_member_attribute`, default `member`,
values interpreted as DNs). This covers Active Directory groups and OpenLDAP
`groupOfNames`. Each member DN resolves to (email, username) via a
base-scope search on that DN retrieving `ldap_email_attribute` and
`ldap_username_attribute`, the same attributes the JIT path trusts. An
entry with no email is skipped and counted (the JIT path refuses them too).
posixGroup `memberUid` semantics and nested-group flattening are out of
scope, per the issue.

### Reconciliation: fail-closed set arithmetic through existing ops

Per mapped group, one pass computes:

- desired: the resolved directory members
- current: HERD `group_members` rows for the mapped group, restricted to
  `auth_source='ldap'` users

Membership changes for a group are applied ONLY from a fully resolved
desired set (the #460 rule). A group entry fetch that errors, a group
search that returns zero entries (dangling), or ANY member DN resolution
that errors skips that group's reconcile entirely for this run and marks
the run partial. An unreadable or half-readable directory can therefore
never strip a team's membership; it defers convergence to the next run.

Adds are `desired - current`; removes are `current - desired`. Both apply
through `group_service.add_member` / `remove_member`, so the "Not Grouped"
invariant and cascade behavior match manual administration. The apply loop
is per-operation fault-isolated: a concurrent admin action that makes an
add raise IntegrityError (the row already exists) is a benign no-op, any
other per-op failure is counted and the run marked partial; one racing op
never fails the whole run. Locally-created accounts (`auth_source='local'`)
in a mapped group are invisible to the sync in both directions. The pass is
idempotent: a second run against an unchanged directory produces zero
changes.

Username drift repair: the reconcile already resolves each member's current
directory username; when it differs from the stored HERD username for the
email-matched user, the sync updates the stored username
(collision-guarded: a conflict with an existing username is skipped and
counted).

### Pre-provisioning (decided: yes)

A desired member with no HERD user row is provisioned during sync through
the existing `create_ldap_user` path (`auth_source="ldap"`, no password,
callable outside the login flow), then added to the group. Directory and
HERD membership mirror exactly, and a user's device visibility is correct
at first login rather than one sync cycle later. Collisions are skipped,
counted, and distinguished in the run detail: username taken by another
account, email owned by a non-LDAP account, or a race with concurrent JIT
login provisioning (retried as a lookup, since the row now exists).

### Deactivation sweep: absence-proof plus disabled-filter plus breaker

The sweep is a separate per-run pass over all `auth_source='ldap'` users,
gated by its OWN setting `ldap_sync_deactivation_enabled` (default false):
enabling group mirroring alone never opts a deployment into deactivation.

Identity key (review-corrected): the sweep searches the directory by EMAIL
(`({ldap_email_attribute}={email})` under `ldap_user_base_dn`), the same
key JIT provisioning and login resolution trust, NOT the stored username
(which is never refreshed by the login path and drifts on directory
renames). Additionally, any user resolved as a member of any mapped group
in this run's reconcile pass counts as present without a second search.
The original username-keyed design was disqualified in review: a directory
username rename made the same run confirm the user as a group member and
deactivate them, with auto-reactivation unreachable.

- A search that ERRORS (bind failure, timeout, LDAP exception) proves
  nothing; the user is left untouched and the run is marked partial. Error
  is never absence.
- A search that SUCCEEDS with zero results marks the user proven-absent.
- Disabled detection (decided): when `ldap_disabled_filter` is set (for
  example the AD `userAccountControl` lockout bit filter), a user whose
  entry matches it is treated like proven-absent. The empty default means
  absence-only detection, with the limitation documented; directories with
  no standard disabled flag model removal as absence.
- Circuit breaker (decided): the pass aborts, deactivating no one, only if
  the proven-absent-or-disabled count exceeds BOTH
  `ldap_sync_deactivation_max_percent` (default 20, denominator =
  successfully swept users; errored searches count in neither term) AND
  `ldap_sync_deactivation_min_count` (default 3). The floor keeps small
  deployments functional (1 leaver in a 4-user shop is 25% but under the
  floor, so it deactivates); the percent keeps a misconfigured base DN or
  filter from mass-deactivating a large one. An aborted pass is recorded
  with status `aborted` and the reason.
- Below the breaker, each proven user gets `is_active=False` and
  `deactivated_by_sync=True` (new column on `users`, migration). The
  existing `is_active` checks then block login and refresh with no new
  enforcement code; outstanding refresh tokens die at their next rotation.

Deactivation is a flag flip, never a delete: reservation references by UUID
and audit history stay intact, per the issue.

### Reactivation: automatic only with sync provenance (decided)

A swept user who IS found by the email search (and does not match the
disabled filter), has `is_active=False`, and has `deactivated_by_sync=True`
is reactivated (`is_active=True`, provenance flag cleared). A user an admin
manually deactivated (provenance flag false) is never touched: admin intent
always outranks the directory. Reactivation is the safe direction and is
EXEMPT from a breaker abort: an aborted deactivation pass still applies its
reactivations. The manual admin activate/deactivate endpoints this feature
adds always write `deactivated_by_sync=False`.

### Audit: persisted sync-runs table (decided)

New table `ldap_sync_runs` in the `auth` schema:

- id UUID PK, started_at, finished_at
- trigger: `interval` or `manual`
- status: `success`, `partial` (a group skipped fail-closed, a lookup
  errored, or a per-op apply failure), `aborted` (circuit breaker),
  `failed` (run-level exception)
- counts: users_provisioned, members_added, members_removed,
  members_skipped, users_deactivated, users_reactivated
- detail JSONB, capped (first N change records per category plus a
  truncation marker), including dangling DNs and skip reasons, and an
  error Text for aborted/failed runs

`POST /admin/ldap-sync/run` (sync now) launches the run as a background
task and returns 202 with the run id immediately (a full run does one
directory search per member DN plus one per LDAP user and must not sit
inline in a gateway-timed request); progress is polled via
`GET /admin/ldap-sync/runs`, which lists recent runs. 409 while a run is
already in progress. A retention sweep in the same background loop prunes
rows older than `ldap_sync_runs_retention_days` (default 90).

### Scheduling and coordination

A background task started from auth's `main.py` lifespan, following the
conversation-sweeper pattern: run every `ldap_sync_interval_seconds`
(default 3600) when `ldap_group_sync_enabled` is true and
`auth_method == "ldap"`. Runs are serialized two ways: an asyncio lock
within the process (sync-now 409s while a run holds it), and a Postgres
advisory lock per run so a scaled-out auth service cannot execute
concurrent directory-wide writes from multiple replicas (the
conversation-sweeper precedent tolerates uncoordinated replicas only
because its operation is harmless; this one does not). All new settings are
added to `services/auth/app/config.py` AND the config service's bootstrap
schema.

New settings keys (all consulted only when `auth_method == "ldap"`):

- `ldap_group_sync_enabled` bool, default false (dark by default)
- `ldap_sync_deactivation_enabled` bool, default false (independent opt-in)
- `ldap_sync_interval_seconds` int, default 3600
- `ldap_group_member_attribute` str, default `member`
- `ldap_group_name_attribute` str, default `cn`
- `ldap_disabled_filter` str, default empty (absence-only)
- `ldap_sync_deactivation_max_percent` int, default 20
- `ldap_sync_deactivation_min_count` int, default 3
- `ldap_sync_runs_retention_days` int, default 90

### Blast radius, documented

Group membership drives device visibility and ACL grant evaluation, so a
sync run can change what devices a user sees and what topologies and
reservations they can manage. A removed member is NOT re-added to
"Not Grouped" (matching manual removal), so a non-admin removed from their
last group sees no devices. `docs/ROLES.md` and `docs/ARCHITECTURE.md`
replace the "not mirrored in this release" note with the sync behavior and
this blast-radius statement. Role assignment never syncs; HERD stays the
authority for role, per the issue.

## Delivery phases

1. Directory client group support: group entry fetch, member enumeration,
   per-DN identity resolution, email-keyed user presence search in
   `ldap_service.py`; new settings; LDIF group fixtures in
   `infra/ldap-test/ldif/` (ou=groups, groupOfNames); live tests alongside
   `test_ldap_service_live.py`, gate-covered (both gates run the LDAP phase
   hard-required; the added runtime is seconds, within the gate budget).
2. Mapping store: model, migration, admin CRUD router, DN validation with
   the 422-vs-503 split.
3. Reconciler: fail-closed set arithmetic, pre-provisioning with
   distinguished collision skips, username-drift repair, `ldap_sync_runs`
   table, async sync-now (202 + poll, 409 on overlap), advisory-lock
   serialization.
4. Deactivation and reactivation sweep: `deactivated_by_sync` migration,
   email-keyed absence proof with group-presence credit, disabled-filter,
   two-term circuit breaker with reactivation exemption, manual admin
   activate/deactivate endpoints (the non-sync writer).
5. Interval loop, config-service bootstrap schema keys, `docs/ROLES.md` and
   `docs/ARCHITECTURE.md` updates, run-retention sweep.
6. Frontend admin surface: mappings CRUD, run history, sync-now button
   (every backend feature keeps a frontend path, the #397/#398 precedent).

## Testing

- Unit (`services/auth/tests/`): fail-closed group reconcile (fetch error,
  dangling DN, and single-member resolution error each apply zero changes
  for that group), set-difference reconcile, pre-provisioning with all
  three collision categories, username-drift repair and its collision
  skip, absence-proof rule (error never deactivates), disabled-filter
  matching, two-term breaker (boundary-exact on both terms, denominator
  excludes errored searches), reactivation provenance gate and its
  abort-exemption, group-presence credit (a renamed-username member is
  never deactivated), run-status vocabulary, per-op fault isolation
  (IntegrityError no-op), mocked directory client throughout.
- Live LDAP (gate, hard-required): mapped group builds membership; upstream
  remove drops membership; member with no HERD row is provisioned; user
  deleted from the directory is deactivated and reactivated on restore;
  dangling mapping applies nothing and marks the run partial; second run
  is a no-op. Fixtures ride `infra/ldap-test`.
- Contract: new admin endpoints land in the auth OpenAPI snapshot.
- Integration/e2e: the existing `HERD_INTEGRATION_LDAP`-gated stack suite
  gains a sync path only if the stack-mode LDAP flow is extended; otherwise
  live coverage stays in the auth suite by design (the gate runs it
  hard-required).

## Out of scope

Per issue #38: role mapping, push/event-driven sync, SCIM, nested-group
flattening, and SSO/OIDC claims as a sync source (the mapping store is
reusable for that later). Per the review resolutions: stable-id
(entryUUID/objectGUID) mapping identity is a follow-up, and disabled
detection on directories with no disabled flag reduces to absence.
