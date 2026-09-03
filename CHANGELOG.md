# Changelog

## [Unreleased]

- Fixed the notifications, execution, and integration NATS pull consumers to
  fetch one message per pull instead of a batch of 10, so nats-py can no
  longer hold an already-delivered message for up to the fetch timeout while
  waiting for the rest of a batch that never fills (issue #648).

## [0.3.0] - 2026-08-30

- Shipped LDAP directory group sync end to end (ADR 0011): a directory group
  client and mapping store, a fail-closed reconciler with an asymmetric skip
  taxonomy, a deactivation/reactivation sweep, an interval sync loop, and an
  admin UI at `/admin/ldap-sync`, plus a checked-in LDAP test gate and live
  Postgres/LDAP coverage wired into `make master` and `make everything`.
- Added fork version preview, diff, and restore to the reservation topology
  editor: a read-only ghosted preview of any saved version, a client-side diff
  against another version or the current draft, and restore-to-draft that
  stages a version's canvas for the next Commit rather than reconciling
  directly.
- Introduced network element objects (ADR 0012, issue #22): a persistent,
  non-device canvas node that many device ports can attach to for a shared
  VLAN segment, subnet, external cloud, or patch-panel trunk, with backend
  validation and fork-save handling, a frontend palette and attach dialog, and
  a live Playwright suite; provisioning is deferred to a later anchored-VLAN
  phase.
- Added a seeded end-to-end test pass (issue #629) so a device-gated Playwright
  test can no longer silently skip behind a green gate: `make test-e2e-seeded`
  runs after the gate stack is seeded, in both `make everything` and nightly,
  and fails the run on any unexpected skip.
- Hardened JetStream for production: `make prod` now persists stream data
  across container recreates via a mounted volume, with a shared add-or-update
  stream helper and a configurable retention cap; dev and gate stacks keep
  starting every stream empty by design.
- Migrated the AI orchestrator to the anthropic 1.x SDK and made service
  images lock-faithful: every backend Dockerfile now installs from the
  workspace `uv.lock` export instead of resolving dependencies fresh, with a
  new CI check that diffs an image's installed packages against the lock.
- Consolidated recurring backend patterns into `herd_common`: pagination,
  database setup, CORS, internal-service calls with TTL caching, and more,
  removing duplicated code across most of the 12 services with no behavior
  change.
- Shipped a multi-port wiring dialog and a matching admin multi-connect
  dialog, both backed by a new bulk connection-create endpoint, and fixed
  same-device ("loopback") 1:1 pairing in the admin dialog.
- Closed two reconcile edge cases left from the ADR 0009 epic: record-time L2
  supersession settles a stale cross-reservation row on frozen wiring instead
  of leaking its VLAN allocation, and stale-intent revalidation widens
  release-direction reconcile against failed-but-still-intended rows without
  weakening build-direction rebuild.

### Delivery detail

#### AI orchestrator

- Migrated to anthropic SDK 1.x (issue #592, PR #608): `anthropic_provider.py` now
  imports `httpx2` (aliased as `httpx`, kept local to that file) so its exception
  catch matches the `TransportError` type the 1.x SDK actually raises, and gets its
  own `_build_anthropic_http_client` returning `anthropic.DefaultAsyncHttpxClient`
  (ca_cert wins, verification fails closed) instead of the shared
  `openai_provider._build_http_client`, which stays on plain httpx for the
  unaffected OpenAI-compatible path. `pyproject.toml` moved `anthropic>=0.39.0,<1`
  to `>=1,<2`; `uv.lock` regenerated (anthropic 0.96.0 to 1.0.0, plus the new
  httpx2/httpcore2/truststore transitive entries). This is the fix for the
  construction-time `TypeError` PR #591 had worked around.
- `GET /api/ai/status` reports a degraded state when the configured provider
  fails to CONSTRUCT rather than claiming `enabled: true` from the static
  settings check alone (issue #606, split out of #592; the 2026-08-24 gate case
  was an anthropic 1.0.0 rejecting an httpx 0.x client that the settings check
  could not see). The status handler now attempts a cheap, no-network provider
  construction, through the same `_build_provider` path `get_ai_client` uses,
  after the settings check passes, and reports `enabled: false`, `degraded:
  true`, `reason: <exception class name>` on failure; the reason is the
  exception CLASS NAME only, never the message, which can carry a base URL or
  key material. The construction attempt is cached for 30 seconds (module-
  level, monotonic clock) so the unauthenticated endpoint cannot be used to
  hammer provider construction. Additive response shape: `degraded` and
  `reason` are new fields, `enabled` semantics are unchanged for the
  already-covered unconfigured and successfully-configured cases.

#### Auth and LDAP

- ADR 0011, LDAP directory group sync (issue #38, CLOSED), delivered across six phases:
  - Phase 1 (PR #507): directory group client in auth's `ldap_service.py`
    (`fetch_group`, `resolve_member`, `resolve_members` batched over one shared
    connection, `user_present_by_email`), under the contract that `None` and
    `skip_reason` are answers the directory gave while `LdapUnavailableError` means
    it could not be asked; the sync functions refuse anonymous binds via
    `require_service_account`.
  - Phase 2 (PR #510): mapping store, `ldap_group_mappings` (migration 0006,
    canonical-DN key, `UNIQUE herd_group_id` so one directory group maps to one HERD
    group), admin CRUD at `/admin/ldap-sync/mappings` with a 422-vs-503 validation
    split and accept-with-warning on a memberless entry; the warning is derived from
    `member_dns` emptiness rather than an attribute-presence flag, because ldap3
    back-fills missing attributes so presence itself is unobservable.
  - Phase 3 (PR #512): reconciler `ldap_sync_service.run_sync`, with a fail-closed
    asymmetric taxonomy (a transport error skips the whole group; a directory answer
    such as not_found, missing_email, or missing_username skips only what was
    answered; per-op apply failures are isolated); group-membership removals are
    suppressed whenever any member skipped as missing_email or missing_username
    (not_found never suppresses), so an incomplete read never silently deletes
    memberships it could not fully verify; `is_active=False` users are invisible to
    membership sync in both directions; `ldap_sync_runs` audit rows (migration 0007,
    committed running-first); sync-now (`POST /admin/ldap-sync/run` 202, `GET runs`
    and `runs/{run_id}`) serialized by an asyncio lock plus a Postgres session-scoped
    advisory lock, committed post-acquire so it never idles in a transaction.
  - Phase 4 (PR #514): deactivation/reactivation sweep, with paged enumeration
    answering directory presence (any failure or truncated page raises rather than
    partially applying); disabled-filter enumeration overrides group-presence credit;
    a strict-both-terms circuit breaker aborts the sweep but still applies any
    reactivations already computed; reactivation is gated on `deactivated_by_sync`
    provenance (migration 0008), and the manual `/users/{id}/activate` and
    `/deactivate` endpoints always clear that provenance; every sweep flip is a
    guarded compare-and-swap update recorded only after commit, so a concurrent
    admin write always wins.
  - Phase 5 (PR #518): interval loop
    (`services/auth/app/tasks/ldap_sync_loop.py`), gated on `auth_method=ldap` AND
    `ldap_group_sync_enabled` (default false); the first tick sleeps a full interval,
    and a sub-60s interval clamps with a warning rather than failing boot;
    `ldap_sync_runs` retention (`ldap_sync_runs_retention_days`, default 90) rides
    the same loop, pruning at most once per 24h and advancing only on success, never
    pruning a running row (retention is loop-only, so a sync-now-only deployment
    accumulates rows by design); closed the config-bootstrap schema debt for all 11
    LDAP keys and the docker-compose env-passthrough gap.
  - Phase 6 (PR #526): admin UI at `/admin/ldap-sync` (mappings CRUD with the
    memberless-DN warning banner, run history with status badges and 2s polling that
    stops on a terminal or 30-minute-stale running row, sync-now with 409
    discrimination on pinned detail strings) plus `GET /admin/ldap-sync/status`,
    admin-gated but deliberately ungated on `auth_method` so it can report sync is
    off. `errorDetail` lives in a single copy at `frontend/src/lib/errors.ts`.
  - Open follow-up: issue #511 (further auth/LDAP consolidation).
- Advisory-lock consolidation and LDAP reconciler scaling (issue #513, PR #529):
  `herd_common` gained a shared `advisory_lock` module with both the
  transaction-scoped blocking `xact_lock` idiom (reservations' device-lock
  acquisition) and the session-scoped try-lock `session_try_lock`/`session_unlock`
  idiom (auth's LDAP sync slot), each self-gating to a no-op on non-Postgres so
  SQLite unit tests need no branch.
- Checked-in LDAP test-gate phase (PR #504): `make ldap-up` boots a stateless,
  checked-in `infra/ldap-test/` directory seeded with the ADR 0011 group fixtures,
  replacing the old external `HERD_LDAP_DIR`/home-directory setup; `make master` and
  `make everything` gained a `_gate-ldap-tests` phase that runs the live-LDAP auth
  suite as a hard requirement.
- Stale-run reaper for `ldap_sync_runs` (issue #528, PR #546): a hard process death
  mid-run (OOM kill, container crash, power loss) never reaches the run's finalize
  step, leaving the row stuck at `running` forever, since the retention prune
  deliberately never touches a `running` row. `reap_stale_running_runs` flips a
  `running` row past `LDAP_SYNC_RUN_STALE_SECONDS` (default `7200`) to `failed`
  with `error="run did not finalize (process died mid-run)"`, via a compare-and-swap
  `UPDATE ... WHERE status = 'running'` so a run that finalizes between the cutoff
  computation and the update keeps its real outcome. It runs at the start of every
  sync run, inside the run-serialization slot (never before it, so a call about to
  raise `SyncBusyError` can never reach a live run's row), covering both the
  interval loop and sync-now. The threshold is read through the new
  `effective_ldap_sync_run_stale_seconds()`, which clamps to the larger of 60
  seconds and twice the effective sync interval (with a warning, never a boot
  failure), so a tuning value at or below the interval cannot fail a run that is
  merely slow. The admin UI's 30-minute stale-row display (PR #526) is an
  independent, shorter client-side hint; this reaper is what corrects the audit
  record itself.
- LDAP integration suite and Postgres-live sync coverage wired into the gates and
  nightly (issue #572): `tests/integration/test_ldap_auth.py` was gated on
  `HERD_INTEGRATION_LDAP=1`, a variable no Makefile target, CI workflow, or compose
  file ever set, so it (and the sync-admin surface) self-skipped in `make master`,
  `make everything`, PR CI, and nightly alike. PR #582 first closed the
  real-Postgres half of the gap: `services/auth/tests/test_ldap_sync_service_live_pg.py`
  and `services/common/tests/test_advisory_lock_live_pg.py` exercise `_SyncSlot`'s
  cross-replica advisory-lock branch and the underlying `session_try_lock`/
  `session_unlock`/`xact_lock` SQL against a real server for the first time (every
  prior test ran on SQLite, which no-ops that whole code path). This delivery closes
  the rest: a new Makefile phase, `_gate-ldap-stack-tests`, runs after `test-e2e` in
  `master`, `everything`, and the nightly workflow, connects the checked-in
  `infra/ldap-test` server onto the ephemeral stack's compose network, recreates
  ONLY the stack's auth service (`--no-deps --force-recreate`) in LDAP mode, runs
  `test_ldap_auth.py` plus the new `tests/integration/test_ldap_sync_admin.py`
  (mapping create, sync-now, run polling, group-membership reconcile against a real
  directory, and a concurrent-sync-now race proving the loser gets the in-process
  busy 409), then always restores auth to local mode before any later phase (seeding,
  load tests) runs. `test_ldap_sync_admin.py` works around the resulting
  chicken-and-egg problem (in LDAP mode `authenticate_user` consults ONLY the
  directory, so the stack's seeded local superadmin cannot log in and there is no API
  path to an admin token) by promoting a JIT-provisioned directory user directly in
  Postgres via `docker compose exec postgres psql`, then re-logging in. A sibling
  `_gate-pg-live-tests` phase runs the two PR #582 files hard-required
  (`HERD_TEST_PG_REQUIRED=1`) against the gate stack's own Postgres, since they need
  no LDAP mode. Both new targets are deliberately free of `$(MAKE)` references in
  their own recipes (unlike the pre-existing `_gate-ldap-tests`), since a recipe line
  containing one executes for real even under `make -n`, and these mix in real
  docker network/compose mutation.
  Two follow-up fixes landed the same day from a live-gate rerun. First, a
  `COMPOSE_PROJECT_NAME` leak: `LDAP_COMPOSE` had no `-p`, and that env var (which
  `_gate-ldap-stack-tests` inherits from the gate targets) outranks a compose file's
  own `name:` in project resolution, so every bare `$(LDAP_COMPOSE)` call, including
  `down -v --remove-orphans`, silently targeted the GATE project instead of
  `herd-ldap-test`; a `down --remove-orphans` there reads every real gate container as
  an orphan of a project whose compose file defines one service, and removes them
  all, turning a routine LDAP-test-server teardown into a full gate-stack teardown.
  Fixed by pinning `LDAP_COMPOSE` with `-p herd-ldap-test` (the one project-name
  source no environment variable can override), dropping `--remove-orphans` from
  `_gate-ldap-stack-tests`' own `down` calls as a second layer, and adding two
  pre-flight guards: the target compose project must already have a running `auth`
  container, and its `config.json` must not have been promoted above the environment
  (no `config.bootstrapped` marker) by a prior config-UI save, since either would
  otherwise fail confusingly deep into the phase instead of failing fast with a clear
  message. Second, a seeded-stack collision: `tests/integration/test_ldap_auth.py`
  and `test_ldap_sync_admin.py` defaulted to the `user1..user25`/`herd-eng` fixtures,
  which collide by username with a `make seed`-seeded stack's local `user1..user1000`
  rows (JIT-provisioning refuses with `username_collision`), and separately are
  invisible to `ldap_sync_service`'s reconciler, which only ever touches LDAP-sourced
  users, so a mapped `herd-eng` group's membership assertion would silently lose its
  seeded members too. `infra/ldap-test/ldif/70-seed-integration.ldif` adds dedicated
  `ldapit-admin` and `ldapit-eng1..3` identities (uids that can never match the seed
  script's `user[0-9]+`/`admin[0-9]+` patterns) plus their own `cn=herd-it-eng` group,
  which both integration test files now use instead; the compose healthcheck (probes
  the last entry of the last bootstrap LDIF file to prove the seed is complete) and
  `test_ldap_service_live.py`'s `_seed_is_current` stale-seed guard were both
  retargeted/extended to the new file so an older checkout is still caught.

#### Reconcile and provisioning

- Record-time L2 supersession (issue #479, PR #492): `record_l2_membership_active`
  parks another reservation's stale ACTIVE row on the same (switch, port) as FAILED
  intended-RELEASED, if and only if that reservation's wiring is frozen (frozen
  equals condemned: the freeze commits before any teardown driver call and has one
  production call site). The frozen gate is load-bearing, not an optimization: an
  unfrozen cross-reservation ACTIVE row can be the port's rightful live holder and is
  never touched. This is deliberately not the L1 flip-to-RELEASED path, since
  `add_to_vlan` displaces nothing; settlement there rides the release-direction
  retry channels instead, which then release a superseded row's orphaned fabric
  VLAN allocation (previously only reachable via the apply tail, so a dead
  reservation's VLAN number could strand forever).
- Stale-intent settlement and revalidation (issue #491, PR #493): all three full
  reconciles' RELEASES diff now runs against (ACTIVE union FAILED intended-ACTIVE)
  rows while the BUILDS diff stays ACTIVE-only. The asymmetry is load-bearing:
  widening builds would permanently exempt a failed-but-still-intended pair from
  rebuild. Stale rows are direction-flipped by park helpers that never downgrade a
  concurrently-ACTIVE row, and ride the existing remove/release path; L2
  nil/dangling-allocation rows flip RELEASED with no driver call. Both retry
  channels now revalidate BUILD-direction rows against cabling's current intended
  set before driving; intent gone parks the row release-direction; a fetch failure
  fails closed (nothing driven, reported `still_failed`, not a 503). Pinned-reason
  zombie rows (unresolvable or not-a-simple-chain) stay excluded from the widening;
  their recovery remains a fork re-save.
- `cancel_reservation` and `release_reservation` adopt
  `_release_exclusive_devices_best_effort` (issue #599 part 1, PR #604) instead of
  reimplementing it inline, closing three gaps: a per-device warning log on a
  fetch failure during the release loop (a new optional `context_label` names the
  caller's operation, defaulting to "provision failure" for the two pre-existing
  callers so their behavior is unchanged); an optional `user_id` on the
  retry-exhausted error log; and the retry-exhausted message text parameterized
  by `context_label` so cancel and release keep their existing wording exactly.

#### Cabling and inventory

- Network element objects, phase 1 of 3, cabling backend only (ADR 0012, refs
  issue #22; frontend and docs land in later phases). A network element is a
  non-device canvas node (`networkElementNode`) many device ports can attach
  to with a many-to-one edge, modeling a shared VLAN segment, subnet, external
  cloud, or patch-panel trunk without a full mesh of point-to-point links or a
  new registry table (canvas-native and topology-local by decision).
  `node_to_element_map` (`fork_save_service.py`, beside `node_to_device_map`)
  maps React Flow node ids to element ids for nodes of type
  `networkElementNode`, keyed off `data.element.id` with a fall back to the
  node id. `classify_element_edge` (`fork_save_service.py`, beside
  `node_to_element_map`) is the one shared, pure edge-classification helper
  built on top of it: given one edge plus both maps it returns `"attachment"`
  (a device-to-element edge with a non-empty device-side port name),
  `"element_to_element"`, `"element_edge_no_port"` (a missing or empty
  device-side port name), or `None` (not an element edge, or an element
  endpoint paired with an unresolvable other side). `_run_topology_validation`
  (`services/cabling/app/routes/topologies.py`) and `resolve_canvas_wiring`
  both call this one classifier and act on its result identically. In
  `_run_topology_validation`, checked before the existing BFS pass:
  `"attachment"` is VALID with no BFS and never enters the pathfind batch,
  since an element is not a physical thing the cabling graph could contain a
  path to; `"element_to_element"` reports the new reason `element_to_element`;
  `"element_edge_no_port"` reports the new reason `element_edge_no_port`; `None`
  falls through to the existing `missing_device` handling, unchanged.
  `InvalidEdge`'s docstring (`schemas/topology.py`) now enumerates all four
  reasons; `reason` stays a plain `str`, no schema enum change. In
  `resolve_canvas_wiring`, only `"attachment"` is counted in the returned
  `element_attachments_skipped`: an element edge the validator would reject
  (`"element_to_element"` or `"element_edge_no_port"`) falls through to the
  same silent skip a genuinely broken non-element edge takes, uncounted, so
  the count only ever reflects edges the validator would actually accept.
  None of the three element classifications contribute a `WireSpec`. The
  result threads through a new `CanvasWiringResolution` return type, and the
  count from there through `ForkSaveResult` to the new additive
  `ForkSaveResponse.element_attachments_skipped:
  int = 0` field (`schemas/fork.py`), returned by `POST
  /internal/forks/{reservation_id}/save`; fork-on-activation snapshotting
  (`fork_service.py`'s `_snapshot_connections`) uses the same resolver and
  ignores the count. `tests/contract/snapshots/cabling.json` is regenerated
  (additive: one new `ForkSaveResponse` property, `required` unchanged) and
  `services/cabling/tests/` gains unit coverage for every classification rule
  (both endpoint orderings), the resolver's skip-and-count behavior on a
  mixed canvas, the response defaults, and (follow-on) that the resolver
  does NOT count an `element_to_element` or `element_edge_no_port` edge while
  still counting a genuine `attachment`, pinning the exact validator-agreement
  claim above.
- Bulk connection creation (PR #537): `POST /connections/bulk` (admin-only) creates
  up to 200 connections per call (the cap mirrors inventory's
  `BulkPortCreate.instances`), returning a per-row created/rejected
  `ConnectionBulkReport` so one bad pair does not sink the batch. It shares
  `_extract_bearer_token` and the per-row validation with the single-create path,
  and differs from it in one deliberate way: the device-group boundary check fails
  closed, so an unverifiable device (inventory unreachable) aborts the whole batch
  with 503 and creates nothing, rather than the single path's fail-open, mirroring
  the fail-closed fork-fetch rule from the reconcile epic.

#### Security

- Secrets reverse guard (issue #456, PR #486): `delete_secret` consults inventory's
  by-secret internal reverse lookup (`GET /hypervisors/by-secret/{id}/internal`) and
  refuses with 409 `secret_in_use` (hypervisor ids and names) while any hypervisor
  references the secret, 503 fail-closed when inventory is unreachable, no force
  flag. Secrets' `inventory_guard` module docstring records a claims-registry
  migration path if a second consumer of secrets ever appears.

#### Frontend

- Inventory page Next-click revert (PR #662, nightly run 33300868733): the
  search-debounce effect armed a 300ms `setSkip(0)` timer on every mount, so a
  Next click inside that window was silently reverted to page 1. The effect now
  returns early when the input already matches the applied search; a
  fake-timer vitest pins the race and the seeded e2e pass exercises it live.

- Multi-port wiring dialog (issue #517, PR #530): drawing a line between two device
  nodes on the topology canvas opens `WiringDialog.tsx` (port columns via
  react-window, drag or click-to-connect, per-line L1/L2/L3, "Connect 1:1 in order"
  over the visible filtered ports); the old single-pair modal survives as
  `QuickConnectPopover.tsx` behind a toolbar toggle. Confirming emits one enriched
  edge per line through a shared `addEnrichedEdges` append path. Many same-pair
  edges render as one `BundledEdge` with a count badge, a render-only projection
  over N distinct stored edges (member selection, per-member delete, and
  read-gating all fan out to the underlying edges). Issue #531 (both halves now
  closed, see below) originally noted that cabling's fork-save resolver collapsed N
  same-pair edges to one path on every provisioning path; the dialog showed a
  provisioning notice when N > 1 until the ports half shipped.
- Admin multi-connect dialog (PR #538): `MultiConnectDialog.tsx` brings the same
  staged-lines interaction to the admin Connections page, committing through
  `POST /connections/bulk`, and is the default create surface on
  `ConnectionsPage.tsx` (old single-pair modal behind a Multi/Single toggle). It
  reuses the shared port-selection primitives with `WiringDialog.tsx` but
  deliberately duplicates the dialog shell (drag-versus-click arbitration, SVG line
  geometry, staged-line CRUD) rather than abstracting it, since `WiringDialog` had
  just stabilized after several review rounds; tracked as issue #539 with an
  explicit note not to do that refactor until a third consumer or a double bug fix
  forces the issue. Partial-success handling in `bulkStaging.ts` keeps a rejected
  line staged with the server's reason, and keeps a line with no matching response
  row rather than assuming it was created.
- Dynamic-resources UX completion (issues #472, #473, PRs #487, #488, closing the
  #445 report): a collapsible dynamic-templates section in the Equipment Browser,
  draggable ephemeral canvas placeholders with an editable instance count that
  expand to the pinned per-template wire shape at reserve time, reserve-from-topology
  prefill of the dynamic block, dynamic-aware Copy on the Templates page, and an
  explicit orphaned-secret indicator on the Hypervisors page.
- Dead-code removal (issue #489, PR #490): the unused
  `components/topology-editor/TopologyEditor.tsx` was deleted;
  `pages/TopologyEditorPage.tsx` is and remains the live editor.
- `AdminGuard` route wrapper (issue #527, PR #547; issue #548, PR #550): the eleven
  separate per-page admin guards under `pages/admin/` plus `ReportingPage`'s own
  guard are replaced by one exported `AdminGuard`, at the time in `App.tsx`
  (moved to `components/guards.tsx` by issue #551, see below), wrapping a
  pathless parent route: all 14 admin-gated routes (`/reporting` plus the 13 `/admin/*`
  routes) now redirect a non-admin to `/topology` through the single component.
  Behavior is unchanged; this is a structural consolidation, not a feature or
  permission change.
- Issue #531 closed, both halves (PR #545 ports, this PR layer): the ports half made
  cabling's `resolve_canvas_wiring` resolve each canvas edge against its own
  `data.source_port_name`/`data.target_port_name` instead of only the device pair, so
  N same-pair lines from `WiringDialog.tsx` provision as N distinct wires; edges with
  no port names keep the old device-pair behavior. The layer half was decided as
  canvas-annotation-only rather than implemented: ADR 0009 option C already commits
  execution to deriving L2 VLAN membership and L3 route adjacency from the resolved
  L1 path hops, and `_fetch_fork_intended_wires` filters fork rows to `layer == "L1"`,
  so carrying the dialog's per-line layer into `WireSpec.layer` would drop those rows
  from every reconcile; no backend or provisioning code changed. `WiringDialog.tsx`
  gained a hover tooltip on both layer controls and a short note above the confirm
  button stating that the layer is recorded on the canvas and provisioning derives
  L2/L3 from the resolved path; `TOPOLOGY_EDITOR.md`, the manual, `FEATURES.md`, and
  `PLANNED_FEATURES.md` were corrected to stop describing it as a pending gap.
- Route table extracted for testability (issue #551, PR #560): `AuthGuard`, `GuestGuard`, and
  `AdminGuard` moved out of `App.tsx` into `components/guards.tsx`, and the
  `<Routes>` children moved into an exported `appRouteElements` constant in the new
  `routes.tsx`, leaving `App.tsx` as just `BrowserRouter` plus `Toaster` plus
  `ErrorBoundary` plus `<Routes>{appRouteElements}</Routes>`. A new
  `test/routes.test.tsx` runs `createRoutesFromElements` over `appRouteElements`
  with no rendering involved, walks the resulting tree via a subtree search (so a
  future wrapper around a guard does not false-red the test), and pins: the exact
  set of 14 paths (`/reporting` plus the 13 `/admin/*` routes) that carry an
  `AdminGuard` ancestor; that every one of those paths also sits under `AuthGuard`;
  that the route table has no duplicate paths; and that the `AdminGuard` group
  still renders an `Outlet` for its children. This closes the gap a PR #550
  reviewer found by mutation testing: before this change, moving a route out of
  the `AdminGuard` group left the full vitest suite green. `AdminGuard.test.tsx`
  still pins the guard's own redirect/render behavior; this test only pins route
  membership and structure.
- Consolidated admin-role check (issue #561, PR #607): the inline
  `role === "admin" || role === "superadmin"` predicate, copied across ten call
  sites, is replaced by one `isAdminRole` helper in `frontend/src/lib/roles.ts`.
  Behavior is unchanged; this is a structural consolidation.
- Loopback 1:1 pairing fix (issue #585, PR #603): `MultiConnectDialog`'s "Connect
  1:1 in order" button paired `freeSource[i]` with `freeTarget[i]` positionally,
  so picking the same device on both sides made every index a self-pair
  (`freeSource[i].id === freeTarget[i].id`) and the button staged nothing. A
  same-device pick now intersects the two independently filtered free-port lists
  by port id, in source-column order, and pairs adjacently: `(p1, p2), (p3, p4),
  ...`, with an odd leftover left unpaired and a port visible in only one
  column's filter excluded entirely. Different-device pairing is unchanged. A
  same-device pick with fewer than two pairable ports now reports "Need at least
  two free ports to pair", distinct from the different-device wording.
- Inventory page-size selector (issue #599 part 2, PR #605): `Pagination` gains
  optional `pageSizeOptions`/`onPageSizeChange` props that render a labelled
  Rows-per-page select and keep the bar visible even when the result set fits on
  one page; other `Pagination` consumers are unaffected. `InventoryPage` wires
  the selector (25/50/100/200, within the inventory list endpoint's `le=500` cap)
  to `preferencesStore`, so the chosen page size persists across sessions;
  `preferencesStore` drops the unused `getSavedFilter`.
- Fork version preview, diff, and restore (issue #622, ADR 0006 addendum): the
  fork history panel's read-only version list gains per-version Preview (a
  read-only render of that snapshot on the canvas, ghosted the same way the
  parent-topology preview is, with a "Previewing version N" banner and Exit
  control; editing, the wiring dialog, and Commit all lock while it is up), Diff
  (against another version or the current draft; `lib/forkDiff.ts` is the pure
  client-side set-difference, keying an edge on (source, target,
  source_port_name, target_port_name) rather than its own id so a redrawn
  identical wire never reads as churn; added/removed devices and wires list in
  the panel and added/removed edges color-highlight on the canvas), and Restore,
  rendered ACTIVE-only mirroring the Wiring tab's Retry button. Restore is
  restore-TO-DRAFT, never restore-and-reconcile, and appends no fork_versions
  row itself: it copies the version's canvas onto the fork's draft
  (`ReservationFork.draft_restored_from_id` tracks the pending, unsaved
  restore, surfaced as an amber "Draft restored from version N (unsaved)"
  chip), and nothing is wired until you run Commit to reservation, which is
  what appends the version carrying the `restored_from_id` marker. New API:
  `useForkVersion`/`useRestoreForkVersion` in `api/reservations.ts`, both
  proxying the reservations-service endpoints under
  `/reservations/{id}/fork/versions/{version_id}`. The preview/diff/restore
  canvas state lives in one hook, `hooks/useForkVersionPreview.ts`, so
  `TopologyEditorPage.tsx` only wires its result to the ReactFlow props and
  `ForkHistoryPanel.tsx` rather than growing further inline. Closes the epic's
  last `Partial` entry in `PLANNED_FEATURES.md`.
- Network element objects, frontend (issue #22, ADR 0012 phase 2; backend
  validation and fork-save handling land as a separate phase 1): a new canvas
  node kind, `networkElementNode`, models a non-device reachability hub (a
  shared VLAN segment, a subnet, an external cloud, or a patch-panel trunk)
  that many device ports can attach to without a device-to-device mesh.
  `NetworkElementNodeData` (`types/topology.types.ts`) carries a
  client-minted UUID element id, `element_type` (the closed four-value
  vocabulary), an editable `label`, and free-form `attrs`. Unlike a dynamic
  placeholder, an element node is the OPPOSITE of ephemeral: it PERSISTS into
  `canvas_data`, so `persistableCanvas` in `TopologyEditorPage.tsx` keeps it
  (and its edges) while still stripping placeholders, the one deliberate
  asymmetry in the six `isDynamicPlaceholder`-adjacent call sites the new
  `isNetworkElement` predicate needed its own decision at. The Equipment
  Browser gains an unconditional (not fetched, so never absent) "Network
  elements" collapsible section with four drag cards using the
  `application/herd-network-element` MIME; dropping one onto the canvas mints
  a fresh element id and multiple elements of the same type are allowed,
  unlike the one-placeholder-per-template rule. Drawing a line from a device
  to an element opens a new `ElementAttachDialog.tsx` (a single device-side
  `PortColumn` plus a static element target card, not `WiringDialog`, whose
  props assume a device on both sides) with multi-select: Confirm creates one
  attachment edge per selected port in a single `addEnrichedEdges` call, each
  carrying `source_port_name` and no target port, since the element side has
  no ports. `topologyStore.ts`'s `addEnrichedEdge`/`addEnrichedEdges` now
  normalize direction so the device always lands as the edge's `source` and
  the element as `target`, regardless of which side a connection was drawn
  from. Element-to-element connections are refused in both
  `isValidConnection` and `handleConnect` with the toast "Network elements
  cannot be linked to each other". N attachments to one element bundle into
  one `BundledEdge` for free: `groupEdgesForRender`'s pair key is node ids,
  not device ids, so no code change was needed there. `NetworkElementNode.tsx`
  renders dashed neutral gray with a per-type icon and an inline
  double-click-to-rename label, deliberately distinct from
  `DynamicPlaceholderNode.tsx`'s dashed purple so the two ephemeral-looking
  node kinds are never confused, since only one of them survives a save. The
  minimap colors an element node neutral gray. No provisioning of any kind in
  this phase; the frontend renders whatever `InvalidEdge.reason` string the
  backend validator returns and does not otherwise depend on the backend
  phase.

#### Developer platform and CI

- CI flake fix (PR #532): a test handed fakes out in a fixed order while
  `asyncio.gather` interleaved two tasks nondeterministically; fixed by making the
  test order-independent. It had bitten two unrelated PRs (a frontend-only
  dependabot bump and the wiring-dialog PR #530) before the cause was understood.
- Frontend build fix (PR #536): `tsc` was emitting `vite.config.js` into the
  frontend source tree; the build config no longer does that.
- E2E regression and fix (PR #541): PR #538 made the multi-connect dialog the
  default admin Connections create surface, which silently broke a Playwright test
  that assumed the old single-pair modal (e2e is not part of per-PR CI, so this only
  surfaced in the nightly run); the fix flips that test to Single mode before
  proceeding, and is the worked example behind the rule that changing a default UI
  surface requires grepping `tests/e2e/` for every test that opens it.
- Bulk-connection e2e coverage (issue #540, PR #542): Playwright coverage for the
  bulk connection create flow.
- Dead-code cleanup from a QA sweep (PR #600): removed backend and frontend code
  superseded by ADR 0009's L3/L2 reconcile ledgers and otherwise unreferenced,
  each item independently verified before removal. Backend: execution's
  `route_service.py` lost `assign_routes`, `get_pinned_routes`, `release_routes`,
  `release_routes_for_device`, and `is_route_active` (superseded by
  `record_route_active`/`record_route_failed`/`release_route_membership`, which
  `nats_consumer.py` actually calls); `vlan_service.py` lost `release_vlan` and
  `get_vlan_assignments` (superseded by `l2_membership_service.release_l2_membership`
  and the last-free `delete_vlan` allocation-coupling path); auth's unused
  `AccessTokenResponse` schema, user-profile's unused `auth_service_url` setting
  (it validates JWTs locally and never calls auth over HTTP), config's unused
  `GROUPS` constant, and cabling's dead `require_admin` re-export in
  `routes/templates.py` were all removed; a copy-paste logging bug in acl's
  grant-create log line (`body.resource_type` passed twice instead of a distinct
  4th field) was fixed and pinned with a regression test; a dead `bearer_token`
  parameter was dropped from cabling's `find_blocking_reservations`. Frontend:
  deleted the never-imported `DeviceDetailModal` component (device detail renders
  as a full page via `DevicePage.tsx`, not a modal) and its test, the zero-caller
  `useMultiDeviceConnections` hook, the unreferenced `AssistantRequest` type in
  `types/ai.types.ts`, and the unused `class-variance-authority` dependency.
  Duplication-reduction follow-ups from the same sweep are tracked separately in
  issues #595 to #599 and were deliberately left out of this PR's scope.
- Duplication-reduction batch from the #600 sweep's follow-ups, four independent
  extractions into `herd_common`:
  - Shared count-then-page pagination (issue #597, PR #612): `herd_common/pagination.py`'s
    `paginate(db, stmt, *, skip, limit)` runs a `func.count()` over the caller's
    statement (ORDER BY dropped for the count only) plus offset/limit on the
    caller's own statement, with ordering left entirely to the caller. Adopted by
    six list endpoints (acl `list_grants`; auth `get_all_users`,
    `get_all_groups`, `list_mappings`, `list_sync_runs`; notifications
    `list_for_user`); no query parameter or response shape changed at any of
    them. Notifications keeps its existing `offset` parameter and `le=200` cap by
    maintainer decision, and keeps its total decoupled from the `unread_only`
    filter as a separate statement, both pre-existing, tested behaviors.
  - Six third-copy extractions in one batch (issue #595, PR #614):
    `herd_common/database.py`'s `make_database(database_url)` factory, adopted by
    all 11 DB-backed services' `app/database.py`; `herd_common/cors.py`'s
    `add_cors_middleware(app, cors_origins)`, replacing the identical six-line
    `CORSMiddleware` block duplicated across 11 of 12 services (the config
    service's bootstrap-UI `allow_origins=["*"]` exception is untouched);
    `herd_common/auth.py`'s `caller_id(payload)`, adopted by notifications and
    user-profile; execution's `services/_uuid_utils.py` `as_uuid()`, replacing
    three duplicate copies; inventory's `services/manage_guard.py` for
    `_is_admin`/`_user_can_manage_device` (kept inventory-local since they bind
    this service's own settings); and cabling's `routes/forks.py` `_to_delta`
    helper, replacing the same seven-field `WireSpec` mapping written out three
    times. Pure extraction, zero behavior change.
  - Cabling UUID serializer collapse (issue #596, PR #610):
    `services/cabling/app/schemas/_types.py` defines `UUIDStr`,
    `OptionalUUIDStr`, and `UUIDStrList` `Annotated` aliases, replacing 27
    hand-written `@field_serializer` methods across six files. Verified
    offline that the OpenAPI contract is byte-for-byte unchanged.
  - Cached internal-service client (issue #598, PR #615):
    `herd_common/internal_client.py`'s `call_service(base_url, method, path, *,
    auth, ...)` is the client-side transport for one HERD service calling a
    sibling, selecting `InternalTokenAuth` or `ForwardedAuth`; and
    `herd_common/ttl_cache.py`'s `TTLCache`/`SingletonTTLCache` preserve the
    check-lock-re-check-fetch-store sequence three notifications clients had
    hand-rolled, with an injectable clock. Migrated: reservations'
    `_cabling_fork_call`/`_execution_wiring_call`; notifications'
    `ContactClient`/`PreferencesClient`/`AdminListClient`; inventory's
    `_fetch_user_group_ids`/`_fetch_user_group_names`; and `herd_common/acl.py`'s
    `user_has_grant`/`_owns_active_reservation`. Timeouts, error-mapping, and
    fail-closed semantics are unchanged at every call site.
- Lock-faithful service images (issue #593): every service Dockerfile installs
  third-party dependencies from the workspace `uv.lock` instead of resolving
  fresh at build time. Each Dockerfile now runs
  `uv export --frozen --package <svc> --no-hashes --no-emit-workspace
  --no-default-groups -o requirements.txt` and installs that file verbatim,
  then registers `services/common` and the service package themselves with
  `--no-deps` editable installs (no dependency resolution happens on that
  second pass). The build context is unchanged (repo root for every backend
  service); each Dockerfile now also copies the root `pyproject.toml` and
  `uv.lock` into a dedicated `/lock` layer, ordered before the service source
  so the dependency layer still caches across code-only edits. This closes
  the gap that let a freshly built image resolve `anthropic` 1.0.0 while
  `uv.lock` pinned 0.96.0 (the incident this issue tracks): a Dependabot PR
  that bumps a `pyproject.toml` specifier without regenerating the lock now
  fails both `uv lock --check` and every affected image build. A new CI step
  in the `backend` job builds the ai-orchestrator image and diffs its
  installed packages against the same `uv export --frozen` output via
  `scripts/check_image_matches_lock.py`, tolerating only the two editable
  workspace packages and pip's own bootstrap package.
- JetStream durability for `make prod`, with retention caps (issue #620): base
  `docker-compose.yml`'s `nats` service now mounts a `nats-data` volume at
  `/data` and runs `-js -sd /data -m 8222`, so stream contents survive a
  container recreate; `docker-compose.override.yml` replaces `command` with an
  ephemeral store dir, so `make up` and the gate stack keep starting every
  stream empty (test isolation; a volume there would let stale events survive
  a wiped Postgres and NAK into the DLQ). `herd_common/jetstream.py`'s
  `ensure_stream(js, *, name, subjects, max_age_seconds)` is a shared
  add-or-update helper: it tries `add_stream` first and falls back to
  `update_stream` only on the JetStream "stream name already in use with a
  different configuration" error (`err_code` 10058), since plain `add_stream`
  against an existing stream with a different config raises instead of
  returning it, which would otherwise break boot on an upgraded-in-place
  stack. Adopted by the three stream-owning lifespans (reservations'
  `HERD_RESERVATIONS`, execution's `HERD_HEALTH` and `HERD_DLQ`) with a new
  `NATS_STREAM_MAX_AGE_SECONDS` setting (default 7 days, 0 disables the cap)
  on both services. The integration-test NATS helpers
  (`tests/integration/_nats_helpers.py`, `test_health_alerting_flow.py`,
  `test_dlq_and_idempotency.py`, `test_failed_teardown.py`) stopped
  re-declaring streams they don't own via `add_stream` and instead confirm
  existence with `stream_info`, so they no longer race a configured `max_age`
  into an in-use error. `docs/OPERATIONS.md` documents the ephemeral-vs-durable
  split and `docs/ENV_VARS.md` documents the new knob.
- Reused-stack dedup fix for raw-published integration events (issue #611):
  `test_health_alerting_flow.py`'s `_bad_news_event` and `_recovery_event` now
  stamp a fresh payload `event_id`, and `_publish_health_event` sets the
  matching `Nats-Msg-Id` header, mirroring what the outbox producer always
  does. The dev/test NATS container has no volume, so a container recreate
  resets stream sequences to 1 while Postgres still holds notification rows
  keyed under the same `<stream>:<sequence>` dedupe key from an earlier run;
  the next run's inserts then collided on the unique constraint and were
  silently dropped as redeliveries. Stamping `event_id` gives every run its
  own dedupe key regardless of stream sequence.
- Shared auth test harness and generic `Paginated[T]` base (issue #511):
  six auth test files (`test_api_tokens.py`, `test_internal.py`,
  `test_groups.py`, `test_ldap_sync.py`, `test_auth.py`,
  `test_routers_direct.py`) each carried a byte-identical in-memory SQLite
  engine, sessionmaker, `get_db` override, and `setup_db` fixture, plus
  drifted mock-user builder helpers. New `services/auth/tests/_harness.py`
  holds the importable engine/sessionmaker/`mock_user` builder; `conftest.py`
  gains the shared autouse `setup_db` fixture and a `make_client` factory.
  Files that build a private engine by design (the LDAP-sync
  service/reaper/loop suites, the `*_unit.py` files, `test_auth_ldap.py`,
  `test_ldap_service*.py`) are untouched. Separately, `PaginatedUserResponse`,
  `PaginatedGroupResponse`, `PaginatedMappingResponse`, and
  `PaginatedSyncRunResponse` now subclass a new generic
  `app/schemas/pagination.py::Paginated[T]` instead of repeating the same four
  fields (`items`, `total`, `skip`, `limit`); subclassing rather than using the
  bare generic as a `response_model` keeps the OpenAPI component names and
  field order unchanged, so `tests/contract/snapshots/auth.json` needed no
  edit. `Paginated` is deliberately auth-local; promotion to `herd_common`
  waits for a second service wanting the same shape. Full auth suite: 442
  passed, 43 skipped.
- Shared reservations test harness (issue #628, PR #630, the same split as
  #511): six reservations test files (`test_fork_endpoints.py`,
  `test_fork_version_endpoints.py`, `test_wiring_proxy_endpoints.py`,
  `test_rbac_denial.py`, `test_coverage_gaps.py`, `test_reservations.py`) each
  carried a byte-identical in-memory SQLite engine, sessionmaker, `get_db`
  override, and bearer-scheme override. New
  `services/reservations/tests/_harness.py` holds the importable
  `TEST_DATABASE_URL`/`engine`/`TestSessionLocal`/`override_get_db`/
  `override_bearer`; `conftest.py` gains the shared autouse `setup_db` fixture
  and a `make_client(payload)` factory. Each file keeps its own small,
  differently-shaped client helper; only the copied block moved. Eight files
  that bind their session to `app.database`'s own engine at import time
  (`test_expiration.py`, `test_expiry_reminder.py`, `test_dynamic_requests.py`,
  `test_fork_archive_reconcile.py`, `test_fork_backstop_giveup.py`,
  `test_pending_fork_prune.py`, `test_wiring_changed_staging.py`, and
  `test_reservation_service_unit.py`, which patches
  `app.tasks.expiration.AsyncSessionLocal` directly) are untouched by design,
  mirroring auth's LDAP-sync exception; `test_fleet_report.py` mixes a
  fixture-scoped engine with a route-engine block and was left unmigrated
  rather than half-migrated. Full reservations suite: 519 passed, unchanged
  before and after.
- Seeded e2e phase, so a silently-skipping e2e test cannot hide behind a green
  gate (issue #629): the `everything` recipe's existing `test-e2e` pass runs
  before the gate stack is seeded, so every test gated on an available device
  (the fork Playwright tests' `_pw_create_reserved_topology`, `pw_two_devices_with_ports`,
  `transient_reservation`, ...) always skips there by design, and nothing in the
  gate or in nightly.yml re-ran that suite once the stack was actually seeded. New
  Make target `test-e2e-seeded` runs the identical `test-e2e` body (factored into a
  shared `_test-e2e-run` helper so the two cannot drift) with
  `HERD_E2E_REQUIRE_NO_SKIP=1`; `tests/e2e/conftest.py` gates a `pytest_sessionfinish`
  hook on that env var, collects every skipped test (including setup-phase skips
  from a fixture's own `pytest.skip()`) via `pytest_runtest_logreport`, and fails
  the run if any remain, printing each skipped node id and its reason. `everything`
  now runs `test-e2e-seeded` right after seeding the gate stack and before the
  load-test tail, so `EVERYTHING_LOAD=0`/`everything-noload` still gets the seeded
  e2e pass and only the load test itself is skipped; `master` is unchanged (no
  seed phase to run the seeded pass against). `nightly.yml` runs the same target
  right after its "Seed stack for load test" step. The unseeded `test-e2e` pass
  is kept as-is: it exercises the empty-stack UI paths deliberately, this is
  additive. `format_skip_block`, the pure formatting function behind the report
  block, is pinned directly in `tests/unit/test_e2e_seed_gate.py`.
  Follow-up from review: a new `seeded_skip_ok(reason)` marker (registered in
  `pyproject.toml`'s `markers` list, since `filterwarnings = ["error"]` would
  otherwise turn the unregistered-marker warning into a collection error)
  exempts a skip that is expected even on a seeded stack: applied to both
  `test_ldap_login.py` tests (the gate stack runs `AUTH_METHOD=local`; LDAP-mode
  coverage lives in the integration phase, not e2e), and to the two
  deliberately-manual placeholders, `test_ai_feature_gate.py::test_use_ai_button_toggles_with_key_change`
  and `test_config_playwright.py::test_config_save_and_restart_gated`. An exempt
  skip is never counted toward the failing total but is still printed, via the
  new `format_exempt_block` (also pinned in `tests/unit/test_e2e_seed_gate.py`),
  under its own `exempt (seeded_skip_ok):` heading so the log keeps showing it.
  Separately, `test_add_device_ui.py::test_create_device_via_form` had a real
  race on a seeded stack: it read the template `<select>`'s options before the
  templates query had populated them, so it skipped ("no device templates
  seeded") even when the stack actually had 28 templates seeded; it now waits
  (bounded, falling through to the existing skip on a genuine timeout) for at
  least one value-bearing option before checking. The sibling
  `test_template_select_has_placeholder` was checked for the same race and does
  not have it: its placeholder `<option>` is unconditional in
  `CreateDeviceForm.tsx`, rendered outside the templates map, so it needed no
  change. A second review pass added three more fixes: the `seeded_skip_ok`
  marker now also covers the five AI-gated tests (`test_ai_generate_dialog.py`'s
  three `ai_topology`-dependent tests, `test_ai_chat_multi_turn.py`'s chat test,
  and `test_tier2_playwright.py::test_assistant_stream_token_by_token`), since
  neither the gate nor `nightly.yml` configures an AI provider and that skip is
  an environmental gate no seed can satisfy; the new `WebDriverWait` in
  `test_add_device_ui.py::test_create_device_via_form` now ignores
  `StaleElementReferenceException`/`NoSuchElementException` while polling the
  template `<select>`, since a mid-poll re-render could raise one and was not
  caught by the existing `TimeoutException` handler; and the sessionfinish log
  no longer prints the `HERD_E2E_REQUIRE_NO_SKIP=1: 0 test(s) skipped` header
  when every skip is exempt, via the new pure `format_sessionfinish_report`
  helper (also pinned in `tests/unit/test_e2e_seed_gate.py`).

#### Documentation

- ADR 0012, network element objects (issue #22, design only, no code):
  `docs/design/0012-network-element-objects.md` records three decisions taken
  2026-08-29. Storage is canvas-native and topology-local, a new
  `networkElementNode` kind inside `topologies.canvas_data` (and so inside fork
  canvases and version snapshots for free) with no tables and no migration,
  rejecting the registry-plus-attachments shape the issue body proposed because
  reservation-time edits would mutate global rows, forks would need their own
  element story, and element delete would have to sweep every topology's
  canvas. Provisioning is out for v1: an element is a reachability hub only,
  attachments are declarative `layerEdge` rows the validator accepts without a
  BFS, and cabling's fork-save resolver skips them explicitly with a new
  additive `element_attachments_skipped` count, so the invariant is that an
  element edge never becomes a hop and ADR 0009's derivations need no element
  rule. The anchored-VLAN variant is recorded as phase 2 with its hook named
  (synthetic device-to-anchor-switch hops at fork save, since a chain endpoint
  never receives a driver call). AI generation of elements is deferred to a
  follow-up issue. `PLANNED_FEATURES.md` links the ADR from the network element
  objects bullet, which stays `Planned`.
- Network element objects, phase 3 of 3, docs plus live Playwright e2e (ADR
  0012, refs issue #22; closes out the epic phase 1, PR #634, and phase 2 both
  merged first). `TOPOLOGY_EDITOR.md` gains a "Network elements" section
  beside "Dynamic placeholders" covering the palette, the attach dialog, and
  the persist-versus-strip contrast between the two dashed node kinds (gray
  elements persist, purple placeholders never do); `USER_GUIDE.md`'s topology
  editor summary and the published manual
  (`docs/manual/user-topology.html`, a new "Network elements" section, and
  `docs/manual/user-reservations.html`'s equipment-browser note that
  elements never add to a reservation's device count) get the same coverage;
  `BULK_IMPORT_EXPORT.md` documents the CSV-does-not-carry-attachments
  limitation next to the existing isolated-node caveat; `FEATURES.md` gains
  the shipped capability and `PLANNED_FEATURES.md`'s bullet flips from
  `Planned` to `Shipped` with the three-phase delivery summary. This entry's
  ADR doc also gets one amendment: the "Canvas shape" call-site list for
  `isDynamicPlaceholder` missed a seventh site discovered during phase 2
  review, `handleAIProposal`'s device-id set, which phase 2 fixed with a
  positive `isDeviceNode` predicate (`frontend/src/lib/canvasNodes.ts`) and
  the extracted `collectCanvasDeviceIds` helper rather than a negated
  placeholder/element pair, since a negated pair silently stops being
  exhaustive the moment a future node kind is added.
  `tests/e2e/test_network_elements_playwright.py` (new) covers the two
  acceptance paths the ADR's "Testing" e2e level names: dropping a
  `vlan_segment` element from the Equipment Browser (a native DragEvent
  dispatch, since the card is a plain draggable div rather than a React Flow
  node) and attaching two ports through `ElementAttachDialog` via a real
  device-node-handle-to-element-node-handle drag (the same mouse
  move/down/move/move/up technique `test_wiring_dialog_playwright.py` uses
  for device-to-device wiring, since `NetworkElementNode` exposes exactly one
  target handle), then saving, reloading, and reading back through `GET
  /cabling/topologies/{id}` that the element node and both attachment edges
  persisted with `source_port_name` set and through `POST
  .../validate` that the canvas reports `valid: true`; and a reservation
  created against an element-carrying topology reaching `ACTIVE`, with the
  fork's `GET /reservations/{id}/fork` canvas carrying the element node and
  its `POST .../fork/save` response reporting `element_attachments_skipped:
  1` with zero released/built rows, confirmed by a `wiring-status` read-back
  showing no wiring at all for the attachment. Run live, twice, against a
  Playwright-driven Vite dev server proxied at the seeded gate stack (issue
  #629's device-availability seeding trap applies here too: run explicitly
  against a seeded stack, since both `make everything` and `nightly.yml` run
  e2e before `make seed`), plus every other `*_playwright.py` file that opens
  the canvas or the Equipment Browser
  (`test_wiring_dialog_playwright.py`, `test_connections_bulk_playwright.py`,
  `test_fork_live_edit.py -k _pw`, `test_tier2_playwright.py`), all green.

#### Test coverage

- Coverage batch (2026-08-30, PRs #650 through #660): backend workspace line
  coverage stood at 94.76% before this batch (secrets 80.8%, integration
  82.3%, the two lowest services); frontend stood at 73.75% lines with seven
  pages at 0%. After the batch: backend 97.07% lines with every service at 94% or above (secrets 98.3%, integration 97.3%), frontend 89.7% lines with 1,210 tests (was 879), and no file added since v0.2.0 below 85%.
  - Backend: secrets routers to 100% (PR #650); acl's grants router plus a
    root-level unit test for `scripts/check_image_matches_lock.py` (PR #651);
    integration's `nats_consumer` and `webhooks` to 100% (PR #653); cabling's
    forks routes and `fork_save_service` to 100% (PR #654); auth's
    `ldap_sync`, admin, and tokens routers to 100% (PR #656); execution's
    `nats_consumer` 90% to 95%, `wiring_retry_service` and
    `l1_assignment_service` to 100% (PR #659).
  - Frontend: admin group, device-group, users, and add-device pages 0% to
    95-100% (PR #652); the topology editor page 60% to 96%, `AppLayout` to
    100%, plus the reporting and reservations pages, alongside a new vitest
    coverage config that switches to include-based reporting so an untested
    file surfaces at 0% instead of being omitted from the report (PR #658);
    topology and template pages plus `FieldRow`, a 32% lane average to 96%
    (PR #660); the inventory, connections, drivers, recipes, and grants pages
    (PR #655).
  - Most of the backend router gaps were not missing behavior but a
    coverage.py artifact: post-await lines under-attributed behind httpx's
    `ASGITransport`, closed with direct handler-call tests in the existing
    `test_routers_direct.py` convention rather than new HTTP-layer tests.
    A minority were genuine gaps and got real behavior tests instead:
    cabling's prune paths, execution's race branches, and `AppLayout`'s
    dropdown-close behavior.

## [0.2.0] - 2026-08-03

- Completed the connection-driven reconcile epic (ADR 0009): initial provisioning, fork saves, and terminal teardown all flow through one full-reconcile pipeline over the three wiring ledgers, retiring the legacy device-set resolvers; device-set changes wire and release only through fork saves.
- Layered wiring-status surface: every wiring row is tagged l1/l2/l3 (L2 rows carry the resolved fabric VLAN, L3 rows a route count), the reservation Wiring tab renders all three layers with six-outcome retry counting, and the external `/api/v1` facade gains a read-only wiring-status passthrough.
- HERD-owned VLAN definition lifecycle: `create_vlan` on a fabric allocation's first built membership over a transit-inclusive switch scope, `delete_vlan` on last-free with a reuse-race supersession guard; driver `create_vlan` is required to be idempotent.
- Reliability hardening: unreadable fork intent defers convergence instead of tearing down live wiring; terminal teardown freezes wiring first, with commit-time frozen re-checks in all three ledgers; device removal releases wiring from the saved intended set, never the draft canvas, with a durable retry marker; NATS consumers wait for schema readiness during upgrades; startup never runs create_all on a migration-managed schema.
- Security: bulk topology import updates enforce the creator-or-admin gate per row, and visible-device lookups are self-or-admin.
- Frontend: ACL grants management UI, and dynamic-template authoring with hypervisor registration.
- Developer platform: Playwright effect-assertion e2e suite, the validation-gate stack isolated in its own compose project, and a shared HerdBaseSettings config base class.

### Delivery detail

#### Reconcile and provisioning

- ADR 0009 phases 6 through 8, completing the epic (issue #416, CLOSED): phase 6
  (PR #443) moved terminal-transition release onto the three ledgers' shared apply
  path (the teardown pass is deliberately not frozen-gated); phase 7 (PR #447)
  unified initial provisioning onto the connection-driven full-reconcile
  (activation creates the fork, then stages a delta-less `wiring_changed` event
  atomically with the ledger advance, since staging inside the ACTIVE-flip
  transaction races the outbox relay against the fork POST) and retired the
  device-set resolvers, the three `_execute_*` paths, and the event-action maps
  (net -3718 lines), with an expiration-sweep backstop for a reservation whose fork
  creation never landed; phase 8 (PR #449) layered the wiring-status surface by
  l1/l2/l3, extended the Wiring tab to all three layers, and added the read-only
  wiring-status passthrough on the external `/api/v1` facade (relayed verbatim as a
  deliberate exception to the facade's v1-freeze rule; manual retry stays internal
  only). The phase 7 review's hardening nits (issue #448, PR #453) and a
  terminal-teardown warn-and-skip for a missing `reservation_id` (issue #455,
  PR #458) landed as immediate follow-ups.
- Sweep batch (issues #459 through #463, plus the #442 decision; PRs #474, #476,
  #477, #478, #480): HERD-owned VLAN definition lifecycle, coupling `create_vlan` to
  the fabric allocation in the shared allocation-transition step so the fork-save
  reconcile, terminal teardown, and both retry channels share one implementation
  (fires per (allocation, switch) still pending over a transit-inclusive scope,
  since an undefined VLAN on a transit switch forwards nothing; a create failure
  parks that switch's dependent membership builds as FAILED intended-ACTIVE rows);
  `delete_vlan` runs on last-free per switch, is supersession-skipped when the
  number was re-allocated, and a delete failure is log-and-continue; fail-closed
  fork fetch, `_fetch_fork_intended_wires` raising on any non-genuine-404 non-200 so
  unreadable intent defers convergence instead of full-reconciling against an empty
  set and tearing down live wiring (the sibling `_fetch_*` helpers deliberately keep
  the non-200-means-absent idiom, since their misread degrades one connection, not
  the whole desired set); freeze-first terminal teardown, landing and committing the
  freeze before any ledger snapshot or teardown driver call, with all three ledgers
  re-checking frozen at record time; PATCH-remove reworked to prune the fork's
  saved intended set by edge incidence (through-hops serving a remaining saved edge
  survive, far hops of a pruned edge release) instead of round-tripping the draft
  canvas through a save, with a durable `pending_fork_prune_device_ids` retry marker
  (migration 0014); a NATS consumer schema gate
  (`herd_common.consumer_schema_gate.start_consumer_when_schema_ready`) that defers
  behind a background poll on the migration-managed path with model tables missing,
  instead of draining events into the DLQ during an upgrade window.

#### Security

- Authz tightening: bulk topology import updates enforce the creator-or-admin gate
  per row (issue #464, PR #475), and inventory's
  `GET /device-groups/visible-devices` is self-or-admin (issue #465, PR #474).

#### Frontend

- ACL grants management UI (issue #397, PR #450).
- Dynamic-template authoring and hypervisor registration (issue #398, PR #454):
  `HypervisorsPage.tsx` plus the dynamic fields in `TemplateEditorPage.tsx`, giving
  every backend feature of the dynamic-resources epic a frontend path.

#### Developer platform and CI

- Migration lifecycle fix (issue #419, PR #452):
  `herd_common.schema_init.create_all_and_stamp` now skips `metadata.create_all`
  entirely once a schema carries an `alembic_version` stamp, closing the
  create-all-vs-migration collision hazard at the source; a per-migration
  `has_table` guard is no longer required for new migrations.
- HerdBaseSettings (issue #384, PR #451): all 11 service Settings models now extend
  a shared `herd_common.base_settings.HerdBaseSettings` base, removing duplicated
  per-service settings boilerplate.
- Gate-project isolation (PR #446): the master/everything ephemeral validation
  stack runs in its own compose project, so gate runs no longer destroy the dev
  stack's volumes or seed data.
- Effect-assertion e2e suite (issue #388, PR #444): 19 tests asserting the
  backend-observable effect via API read-back after each UI action, not just UI
  acknowledgment, and restoring mutated state to baseline; also replaced the flaky
  Selenium `test_inventory_expanded` nightly case (issue #335) with a Playwright
  port.

## [0.1.0] - 2026-07-27

- AI topology generation with human-in-the-loop ghost-node review, feature-gated behind an Anthropic or OpenAI-compatible LLM provider.
- Reservation lifecycle with editable per-reservation topology forks, release-before-build reconcile, port-claim conflict detection, and immutable as-built records at teardown.
- Automatic L1/L2/L3 infrastructure provisioning on reservation events, with a connection-driven fork-save reconcile at all three layers backed by per-item ledgers, direction-aware auto and manual retry, and per-connection L1 Wiring-tab status.
- Driver sandbox for admin-uploaded packages under POSIX rlimit caps, with driver-published JSON Schema config vocabularies and per-command execution transcripts.
- Encrypted-at-rest secrets service with AES-GCM envelope encryption, online key rotation, and ACL-gated reveal.
- Dynamic hypervisor-backed resources that materialize instances through recipe drivers with an idempotent instance ledger and a provisioning timeout backstop.
- Local and LDAP/Active Directory authentication with three-role RBAC, device-group visibility, and resource-level ACL grants.
- External versioned `/api/v1` integration facade with admin-minted API tokens and HMAC-signed, at-least-once outbound webhooks.
- Zero-database first-startup config UI, utilization reporting with CSV export, multi-channel notifications, bulk import/export, topology versioning, BFS pathfinding, and fleet-scale device health polling.

### Delivery detail

#### Reconcile and provisioning

- Editable reservation topology forks (ADR 0006, issue #25 P3a, PRs #349, #353,
  #359 through #361): cabling owns `reservation_fork`/`fork_connections`/
  `fork_versions` (keyed by a bare `reservation_id`, no cross-schema FK) behind an
  idempotent internal fork-create call; reservations forwards the user-facing fork
  surface and archives forks from all terminal transitions; live-edit mode edits the
  fork instead of the parent topology, with debounced draft autosave, a
  released/built toast, a port-conflict dialog, and a read-only as-built render for
  ended reservations.
- Connection-driven L1 reconcile (ADR 0007, issue #345 P3b, PRs #363 through #365,
  #367, #368, #371, #372): fork saves stage a `reservation.wiring_changed` event
  anchored to a `fork_wiring_ledger` row advanced atomically with the outbox
  enqueue; execution applies events in fork-version order via
  `reservation_wiring_state.last_applied_fork_version`, chain-walking hops grouped
  by a new nullable `edge_key` column (migration 0008, the canvas edge id,
  deliberately kept outside connection identity) rather than pairing positionally; a
  Wiring tab surfaces per-connection status with manual retry for parked rows,
  backed by a batch-capped auto-retry channel.
- Driver-result-keyed success (issue #370, PR #387): the shared
  `driver_result_failed` helper judges success from the driver's own result payload
  rather than the sandbox transport flag (a healthy transport only means the child
  process did not crash), and every `run_driver_action` call site (health polls,
  device-check, manual/internal execute, configure, retry) now gates through it.
- Never-downgrade-an-ACTIVE-row discipline (issue #412, PR #414):
  `record_l1_failed` never overwrites an ACTIVE assignment row with a failure, since
  a failure recorded against a pair a concurrent writer has since proven connected
  is stale by definition.
- ADR 0009 phases 1 through 5 (issue #416, in delivery): phases 1-2 (PR #418) gated
  every L2/L3 driver call site through `driver_result_failed` and corrected the
  legacy device-set L1 resolver to pair per switch (exactly two reserved-adjacent
  ports pair unambiguously; any other count fails safe with a warning, since the
  device-set graph carries no edge intent); phase 3 (PR #424) added
  intended-direction wiring ledgers (migrations 0016 through 0018) with
  direction-scoped retry and freeze (terminal reservations may retry
  release-direction rows; the manual-retry proxy 409s only PENDING/
  PENDING_PROVISION; the UI Retry button is ACTIVE-only), the six-valued retry
  outcome vocabulary (reconnected/released/superseded/still_failed/
  not_retryable/frozen), and a cross-reservation supersession guard for no-driver-
  call releases; phase 4 (PR #437) made L2 VLAN membership fork-driven on
  `wiring_changed`, always a full reconcile derived from recorded hops with trunk
  hops excluded, split from the unchanged per-fabric VLAN-number allocation (first
  built membership allocates, last released frees); phase 5 (PR #438) made L3 route
  adjacency fork-driven the same way, with release adjacency-aware (routes stay
  while any intended hop lands on the switch) and the issue #20 pin lifecycle left
  untouched; L3 supersession is deliberately absent, since pins are per-reservation
  and non-exclusive.
- L3 driver contract (issue #20, PR #251): Layer 3 Switch promoted to a fully
  invoked connection type.
- Dynamic hypervisor-backed resources (ADR 0004, issue #32, PRs #264, #271, #272,
  #281): a hypervisor registry and dynamic template type, recipe drivers
  (`create_instance`/`destroy_instance`) run through the execution sandbox, an
  idempotent `dynamic_instances` ledger keyed by a stable per-request `request_id`,
  gated reservation activation via a `provision-result` callback, and a
  compare-and-swap expiration-sweep timeout backstop that moves a stuck
  PENDING_PROVISION reservation to FAILED without racing a late callback. The same
  backstop reverts a stranded physical-only reservation back to PENDING instead of
  failing it (issue #318, PR #350), since `reservation.created` stages atomically
  with ACTIVE, so a stranded row provably triggered no provisioning.
- Fleet-scale health polling (issue #24, PR #306): the health scheduler claims at
  most `HEALTH_POLL_BATCH_SIZE` due rows per tick and runs at most
  `HEALTH_POLL_MAX_CONCURRENCY` polls at once; devices carry a persisted in_use/idle
  poll tier flipped by reservation lifecycle events; `EXECUTION_POLLER_ONLY` runs a
  replica as poller-only with no API routers.
- Transactional outbox (issue #21, PR #237): the event producer writes an outbox
  row in the same DB transaction as the state change, and a background relay
  publishes it with a `Nats-Msg-Id` header for broker-side dedup plus a payload
  `event_id` for consumer-side dedup, so no request-path code publishes a
  state-change event to NATS directly.
- Off-loop driver execution (issue #317, PR #320): the execution consumer runs
  driver calls via `asyncio.to_thread` with a per-message `in_progress` ack
  heartbeat, so a slow provisioning call does not trigger ack-timeout redelivery.
- Driver-published config schemas (issue #23): a driver's optional
  `config_schema()` classmethod is extracted in the sandbox and cached per-SHA256;
  the `configure` boundary validates against the published schema first and falls
  back to inventory's connection-type registry only on a `PublishedSchemaError`, so
  a driver can accept a config vocabulary the registry's `additionalProperties:
  false` schema would otherwise reject.

#### Cabling and inventory

- Bulk import name-match semantics (issue #336, PR #348): topology import matches
  existing topologies by name and updates them in place rather than duplicating; a
  topology update that would rewire wiring held by another user's active
  reservation rejects per-row.
- Batch read endpoints (issues #249, #250, PR #302): inventory's
  `POST /devices/batch` and cabling's `POST /pathfind/batch` replace per-id GET
  fan-out from the topology editor.
- Cross-service referential guards (2026-07-19): inventory's admin device DELETE
  refuses with 409 `device_in_use` while a non-terminal reservation holds the
  device, via a fail-closed reverse lookup into reservations (issue #391, PR #407);
  cabling's `POST /connections` 422s when a referenced device does not exist, since
  inventory's `GET /device-groups/device/{id}` 404s for a missing device while
  still returning 200 `[]` for a real-but-ungrouped one, with the guard's fail-open
  path reserved for transport/5xx outages (issue #392, PR #406).
- Config-version restore active-reservation guard (issue #337, PR #352).

#### Security

- Constant-time internal-token verification (issue #311, PR #351):
  `internal_auth.internal_token_matches` replaces `==` comparisons for
  `X-Internal-Token` checks.

#### Developer platform and CI

- Playwright e2e adoption (PR #400, 2026-07-19): new e2e tests use Playwright's
  Python sync API via `pw_browser`/`pw_page` fixtures, running host-side against
  `E2E_HOST_BASE_URL` rather than the Selenium container; the Selenium suite is
  kept and ported opportunistically.
- Config precedence (PR #374, 2026-07-18): every service resolves settings through
  `herd_common.config_loader.herd_settings_sources`, with a `config.json` saved
  through the config UI outranking container env vars, and a first-boot
  auto-bootstrap file (marked with a sibling `config.bootstrapped` file) ranking
  below env until the first real UI save.
- Save-and-restart scoped to the current compose project (issue #373, PR #383): the
  config UI's restart action self-inspects for `com.docker.compose.project` and
  fails closed, so other compose stacks on the host are never bounced.
