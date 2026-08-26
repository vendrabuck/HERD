# Decision: Directory Group Mapping and Sync, Issue #38

Status: Accepted 2026-08-12. Phase 1 (directory client) delivered in
PR #507, 2026-08-12. The three design questions its adversarial review
parked were resolved with vendra on 2026-08-12 and are amended into the
relevant sections below: mapping validation accepts a member-attribute-less
entry with a warning (never refuses), pre-provisioning mirrors the
directory exactly including members outside ldap_user_base_dn, and the
deactivation sweep uses one paged enumeration instead of per-user
presence probes (the per-user probe survives for the disabled-filter
check). Phase 4 (deactivation and reactivation sweep) delivered 2026-08-14:
the circuit breaker requires STRICT exceeds on both terms (boundary-equal
on max_percent or min_count never aborts), and the disabled filter
overrides group-presence credit (checked last, so a disabled account still
listed in a mapped group still deactivates); both are amended into the
deactivation section below. Phase 5 (interval loop, config-service bootstrap
schema keys, run-retention pruning) delivered 2026-08-14 with no open
questions against this doc. Phase 6 (admin UI: mappings CRUD, warning
banner on the memberless-mapping accept-with-warning case, run history,
sync-now) delivered 2026-08-15, closing the epic; the phase also added a
status endpoint (`GET /api/auth/admin/ldap-sync/status`) beyond the
mapping/run CRUD this doc specifies, so the page can gate on the current
auth_method and show interval-loop context, and no design question was
raised against this doc. Seven decision points were
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
- group_dn Text, unique: the directory group's CANONICAL DN as the
  directory returned it (not the admin-typed form; DNs are
  case-insensitive, so storing raw input would let case variants map one
  directory group twice), the mapping key
- directory_name String(255), cached display name (the group's
  `ldap_group_name_attribute`, truncated to the column), refreshed by the
  phase 3 reconciler on each successful group fetch; a failed refresh
  keeps the last cached value
- herd_group_id UUID FK to `user_groups.id` ON DELETE CASCADE, unique
  (amendment, resolved in phase 2 review: one directory group per HERD
  group, because per-mapping reconcile set arithmetic cannot converge
  when two mappings with different memberships fight over one group;
  deliberately revisitable by dropping the constraint if union semantics
  are ever designed): deleting the HERD group deletes the mapping; the
  directory is never written
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
distinguishes-not-found-from-cannot-ask convention (#337/#456). Amendment
(resolved 2026-08-12): a DN that resolves to an entry LACKING the member
attribute is accepted WITH a warning in the validation response and admin
UI, never refused; AD models empty groups that way, so refusal would block
legitimate mappings, while the warning surfaces the typo'd-to-a-non-group
case that would otherwise reconcile as an empty desired set. Admin CRUD
lives in a new router (`services/auth/app/routers/ldap_sync.py`), gated
admin-or-superadmin consistent with existing group management; creation
requires `auth_method == "ldap"` (validation needs a directory to ask),
while list and delete work in any mode so stale mappings stay cleanable.

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

**Amendments (phase 3 review resolutions, 2026-08-13):**

1. Removals for a group are driven only when no member skipped as
   identity-unresolvable-but-existing (skip_reason `missing_email` or
   `missing_username`: the directory still lists the entry but could not
   answer who it is). Such an entry could be any current row, so the
   group's whole removal set is unprovable that pass and the remove pass
   does not run; a suppressed pass is recorded in the run detail
   (`suppressed_removals`, `{group_dn, unresolved, would_remove}`) when it
   would otherwise have removed someone. A proven `not_found` skip (the
   directory affirmatively answered the DN is gone) does NOT block
   removals: a proven-absent entry shields nobody, so it carries no
   ambiguity about who else might be affected.
2. `is_active=False` users are invisible to membership sync in both
   directions, exactly like locally-created accounts: an inactive LDAP
   user already in a mapped group is neither removed nor re-added by a
   later pass, and one still listed in the directory is never (re)added.
   The phase 4 deactivation sweep is the only sync-side writer of
   `is_active`; this reconciler never flips it.
3. A username-drift collision (the repaired username collides with an
   existing account) degrades the run to "partial", but the member itself
   still reconciles into the group: only the drift repair was skipped, and
   the member is a proven directory answer. It is recorded in its own
   detail category (`drift_collisions`) apart from the member-skip
   counter, so the counters (an added-or-skipped member, never both) stay
   reconcilable against the run's total membership delta.

### Pre-provisioning (decided: yes)

A desired member with no HERD user row is provisioned during sync through
the existing `create_ldap_user` path (`auth_source="ldap"`, no password,
callable outside the login flow), then added to the group. Amendment
(resolved 2026-08-12): this mirrors the directory exactly, INCLUDING
members whose DN sits outside `ldap_user_base_dn`; such accounts cannot
log in until the base is widened (login resolves under the base only),
but directory layout is the admin's domain, and the group-presence credit
keeps the sweep from touching them while they remain members. Directory and
HERD membership mirror exactly, and a user's device visibility is correct
at first login rather than one sync cycle later. Collisions are skipped,
counted, and distinguished in the run detail: username taken by another
account, email owned by a non-LDAP account, or a race with concurrent JIT
login provisioning (retried as a lookup, since the row now exists).

### Deactivation sweep: absence-proof plus disabled-filter plus breaker

The sweep is a separate per-run pass over all `auth_source='ldap'` users,
gated by its OWN setting `ldap_sync_deactivation_enabled` (default false):
enabling group mirroring alone never opts a deployment into deactivation.

Identity key (review-corrected): the sweep proves presence by EMAIL, the
same key JIT provisioning and login resolution trust, NOT the stored
username (which is never refreshed by the login path and drifts on
directory renames). Amendment (resolved 2026-08-12): presence is answered
by ONE paged enumeration under `ldap_user_base_dn` fetching the email
attribute for every entry, checked by set membership locally, instead of
one search per user; a failed or truncated page aborts the whole sweep
deactivating no one, which is strictly more fail-closed than per-user
probes racing an outage, and directory load stops scaling with user
count. The disabled-account check still needs a per-user search that
conjoins `ldap_disabled_filter`; phase 4 builds that variant alongside
phase 1's presence-only `user_present_by_email`, whose hard-coded filter
cannot serve it as-is. Additionally, any user resolved as a member of any mapped group
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

**Amendment (phase 4 delivery, 2026-08-14):** two clarifications the
implementation needed that this section did not previously pin down:

1. The breaker's two terms are STRICT exceeds, not exceeds-or-equal: a
   proven-absent-or-disabled count exactly equal to either
   `ldap_sync_deactivation_max_percent` (as a share of swept candidates) or
   `ldap_sync_deactivation_min_count` does NOT abort. Both terms must be
   strictly exceeded together for the breaker to trip; a count sitting
   exactly on either boundary applies its deactivations.
2. The disabled filter overrides group-presence credit, not the reverse: a
   user resolved as a member of a mapped group this run (proven present via
   credit, no second search) is still deactivated if their directory entry
   also matches `ldap_disabled_filter`. The check order is credit-or-paged-
   presence first, disabled-filter last, so disabled can veto either kind of
   proven presence; this matches disabled-equals-proven-absent taking
   precedence over any presence signal.

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

Migration convention (recorded issue #513, phase 3 to phase 4 practice
made explicit): a counter or status value this table can carry lands in
the SAME migration as the phase that starts writing it, not
pre-allocated speculatively by an earlier phase. Phase 3's migration
(0007) deliberately omitted users_deactivated, users_reactivated, and
the `aborted` status even though this section already named them,
because phase 3 never writes them; phase 4's migration (0008) added the
two counter columns when it became their writer (`aborted` needed no
schema change, status is a plain string column). Columns land with
their writer.

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
   `docs/ARCHITECTURE.md` updates, run-retention sweep. Delivered
   2026-08-14: `services/auth/app/tasks/ldap_sync_loop.py` runs
   `ldap_sync_service.run_sync(trigger="interval")` every
   `ldap_sync_interval_seconds` (first tick sleeps a full interval before the
   first sync, so a rolling restart across replicas does not burst-sync at
   boot), started from `main.py`'s lifespan only when
   `ldap_group_sync_enabled` AND `auth_method == "ldap"`, and serialized
   through the existing `_SyncSlot` exactly like sync-now. A `SyncBusyError`
   from an overlapping run is swallowed as a routine skip, never an error.
   `ldap_sync_interval_seconds` is clamped to a 60-second floor at loop
   startup (logged, not rejected: a bad tuning value must not block auth
   from booting; no pydantic validator on the setting itself). The same loop
   prunes `ldap_sync_runs` rows older than `ldap_sync_runs_retention_days`:
   the FIRST due tick after a process starts prunes unconditionally (no
   restart-dependent wait for a full day), then at most once per 24h after
   that, checked at tick boundaries (the outbox relay's `last_prune`
   pattern, amended here so a raising prune does NOT advance the cadence
   timer, retrying on the very next tick instead of skipping a whole
   window), and a "running" row is never removed regardless of age.
   Retention is enforced ONLY by this loop, never by manual sync-now, so a
   deployment that never enables the loop accumulates audit rows
   indefinitely; a deliberate consequence of keeping pruning off the manual
   path, not an oversight. No migration: the table is small enough that an
   index was judged unnecessary for phase 5. Adversarial review (2026-08-14)
   also caught and fixed a `docker-compose.yml` gap (the new settings were
   undocumented-but-unwired: no compose environment block passed them
   through, so `.env` could not reach the container) and an
   `execute_run` defect where a task cancellation mid-run (the realistic
   case: this loop's task cancelled during service shutdown) committed a
   "failed" row with its cause silently lost, because `asyncio.CancelledError`
   is a `BaseException` the existing `except Exception` never caught;
   `execute_run` now has a dedicated `except asyncio.CancelledError` arm that
   records a fixed cause string and re-raises, so cancellation still
   propagates while the finalized row keeps recording its cause like every
   other failure path.
6. Frontend admin surface: mappings CRUD, run history, sync-now button
   (every backend feature keeps a frontend path, the #397/#398 precedent).
   Delivered 2026-08-15: `frontend/src/pages/admin/LdapSyncPage.tsx` at
   `/admin/ldap-sync`, gated by a new `GET /admin/ldap-sync/status`
   endpoint (`auth_method`, `group_sync_enabled`, `sync_interval_seconds`)
   this phase added to the router beyond what this doc's mapping/run CRUD
   specifies; create/sync-now stay disabled until the status query has
   positively confirmed `auth_method == "ldap"` (a load error or local mode
   both read as disabled, fail-closed), while list and delete work in any
   mode per the mapping-store section above. The memberless-mapping
   accept-with-warning response renders as a persistent inline banner
   naming the group DN rather than a toast. Sync-now polls the run list
   (2s) while a run is `"running"`; a `"running"` row older than 30 minutes
   stops holding polling open and renders as `"running (stale)"`, since
   `execute_run`'s crash-only failure mode (a cancelled or killed process)
   is the only way a row stays `"running"` that long. The two lock-busy
   409 detail strings (`_RUN_IN_PROGRESS_DETAIL`,
   `_RUN_IN_PROGRESS_REPLICA_DETAIL`) are matched verbatim to distinguish
   the informational "already running" toast from the auth_method mode
   refusal, which the same status code also carries.

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
- Integration/e2e (issue #572): the `HERD_INTEGRATION_LDAP`-gated stack suite
  now has a sync path, `tests/integration/test_ldap_sync_admin.py`: mapping
  create, sync-now, run polling, and group-membership reconcile, all
  asserted through the public API against a real running stack, plus a
  concurrent-sync-now race proving the loser gets the in-process busy 409.
  It runs in the Makefile's `_gate-ldap-stack-tests` phase (master,
  everything, and nightly), which switches the gate stack's auth service to
  LDAP mode after e2e and restores it afterward; before that phase existed,
  the stack always booted `AUTH_METHOD=local`, so `HERD_INTEGRATION_LDAP=1`
  was set nowhere and this suite never actually ran. A sibling
  `_gate-pg-live-tests` phase runs the Postgres-live advisory-lock and
  `_SyncSlot` replica-branch coverage
  (`services/auth/tests/test_ldap_sync_service_live_pg.py`,
  `services/common/tests/test_advisory_lock_live_pg.py`) against the gate
  stack's own Postgres. The e2e suite itself
  (`tests/e2e/test_ldap_sync_admin_playwright.py`) still only exercises the
  local-mode refused state, since the e2e phase runs before the stack
  switches to LDAP mode.

## Out of scope

Per issue #38: role mapping, push/event-driven sync, SCIM, nested-group
flattening, and SSO/OIDC claims as a sync source (the mapping store is
reusable for that later). Per the review resolutions: stable-id
(entryUUID/objectGUID) mapping identity is a follow-up, and disabled
detection on directories with no disabled flag reduces to absence.
