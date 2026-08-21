# Changelog

## [Unreleased]

### Delivery detail

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

#### Cabling and inventory

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
