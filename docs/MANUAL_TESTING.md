# Manual test plan

Test cases that are deliberately NOT automated. Each entry explains why
automation is excluded, so a future contributor does not waste effort
re-attempting it (or knows exactly what changed if the reason expires).
Run the applicable cases before a release or after changes to the named
areas; record results in the release notes or the relevant issue.

Conventions: each case lists preconditions, steps, and expected results.
"Per release" cases gate a release; "on change" cases run only when their
named area changes. The automated suites (unit, contract, integration,
e2e, load; see the Makefile targets) are the baseline and are not repeated
here.

## M1. FRR live-config apply (real router path)

- Why manual: needs the external network-simulator lab; netmiko over real
  SSH; CI has no hardware. The dry-run half IS automated.
- Cadence: on change to drivers/frr_mgmt/ or the execution configure path.
- Preconditions: lab reachable; `scripts/seed_frr_demo.sh` run with
  SEED_FRR=1; the two lab routers up.
- Steps: from DevicesPage, open the FRR router; apply a config version
  adding a static route (live, not dry-run); open the device again and
  fetch status.
- Expected: execution run recorded SUCCESS with a per-command transcript;
  `vtysh -c "show ip route"` on the router shows the route; removing the
  route via a second apply removes it live.

## M2. AI topology generate with a real model

- Why manual: output is nondeterministic; quality judgment (sensible
  wiring, plausible port choices) is a human call. The pipeline mechanics
  (gating, ghost-node review, commit) ARE automated with the AI configured
  gate skipped when no provider is present.
- Cadence: on change to services/ai-orchestrator/ prompts or tools; before
  a demo.
- Preconditions: vLLM (or hosted key) configured; `/api/ai/status` reports
  configured.
- Steps: request a 3-device topology in natural language with one
  constraint (e.g. "two servers through an L1 switch"); review the ghost
  proposal; commit it.
- Expected: proposal respects inventory (no invented devices), constraint
  satisfied, commit creates topology + reservation; a second, deliberately
  impossible request (equipment not in inventory) is refused with a
  useful explanation rather than hallucinated hardware.

## M3. Recipe drafting loop quality (AI recipe authoring)

- Why manual: same nondeterminism as M2; the validation loop is automated
  but "is the generated driver sensible" is not machine-checkable.
- Cadence: on change to the recipe-authoring prompts or validator.
- Preconditions: AI_RECIPE_AUTHORING_ENABLED=true, provider configured,
  admin user.
- Steps: draft a recipe for a simple fictional hypervisor from the
  DriversPage panel; read the generated driver.py; approve and upload.
- Expected: validation report green; generated code uses only stdlib
  imports; upload lands in the driver list; a draft that failed validation
  cannot be uploaded.

## M4. Config Save and Restart, production-like scope

- Why manual: the restart-scope behavior (issue #373, PR #383) is about
  OTHER compose projects on the host, and CI runs no second project.
  The single-project restart is covered by unit tests with a fake docker
  client.
- Cadence: on change to services/config/ restart logic.
- Preconditions: HERD up via `make prod`; a second unrelated compose
  project running on the same host.
- Steps: change a schema key in the config UI; Save and Restart; watch
  `docker ps` timestamps for both projects.
- Expected: only HERD services restart; the unrelated project's
  containers keep their uptime; the changed key is live afterward
  (verify via the relevant service's behavior or /api/config).

## M5. LDAP login edge cases against a real directory

- Why manual: the happy path is covered by `make test-auth-ldap` against
  the checked-in OpenLDAP container (`infra/ldap-test/`) and StartTLS
  ordering by the mocked unit suite; referrals, nested groups, TLS against
  a real certificate chain, and password-policy lockouts depend on
  directory-server behavior that the plain-LDAP seeded container does not
  exercise.
- Cadence: on change to auth LDAP code or before enabling a new customer
  directory.
- Preconditions: AUTH_METHOD=ldap against a real lab directory (the
  checked-in container has no nested groups, lockout policy, or TLS); a
  user in a nested group; a user near lockout.
- Steps: log in as the nested-group user; log in with a wrong password
  repeatedly to trip the directory's lockout; log in during a simulated
  referral.
- Expected: the nested-group user logs in and is JIT-provisioned; directory
  group membership mirrors into the matching HERD group only if an admin
  has mapped that directory group at `/admin/ldap-sync` (ADR 0011, issue
  #38), either via sync-now or the background interval loop, and an
  unmapped group still needs manual HERD group assignment; a
  directory-locked account fails with the generic auth error (no lockout
  detail leaked); referrals either work or fail closed with a clean 503,
  never a hang.

## M6. Reservation expiration at wall-clock scale

- Why manual: dev/test pins EXPIRATION_INTERVAL_SECONDS=5 so integration
  tests fit the harness cap; production uses minutes. Timer arithmetic is
  unit-tested; this case observes the real cadence once.
- Cadence: on change to the expiration sweep or its intervals.
- Preconditions: `make prod` stack (no override pin); a reservation with
  end_time a few minutes out.
- Steps: let the reservation expire naturally; watch it transition and
  the fork archive; check notifications.
- Expected: transition happens within one production sweep interval of
  end_time; fork archived; notification delivered; no duplicate events
  (check the outbox/consumer logs for single delivery).

## M7. Browser matrix and canvas ergonomics

- Why manual: e2e runs Chromium only; rendering, zoom/resize reflow, and
  drag "feel" are visual judgments.
- Cadence: per release.
- Preconditions: seeded stack; Firefox and a Chromium-family browser at
  100%, 150%, and browser-zoomed-out views; one small-laptop window size.
- Steps: exercise the topology editor (drag devices, draw an edge, open
  the minimap), the reservation detail modal (all tabs), and the
  Reservations calendar in each browser/zoom combination.
- Expected: no clipped controls or unreachable buttons; canvas drag
  tracks the pointer without offset drift; modals fit the viewport;
  floating panels remain draggable and on-screen.

## M8. Toast timing and stacking under rapid actions

- Why manual: assertions on transient overlapping toasts are inherently
  flaky in WebDriver (the e2e suite asserts single toasts only).
- Cadence: per release, brief.
- Steps: perform several quick mutations in a row (e.g. save preferences,
  cancel a reservation, trigger a validation error) and watch the toast
  region.
- Expected: toasts stack without overlap or orphaned "ghost" toasts; each
  dismisses on its own timer; an error toast is not hidden behind a
  success toast.

## M9. Health-poll tier flip observation

- Why manual: the in_use/idle interval difference (issue #24) is minutes
  at production settings; integration covers the tier-flip logic, not the
  real cadence.
- Cadence: on change to the health scheduler.
- Preconditions: `make prod` stack; a device with a working driver; poll
  intervals at production defaults.
- Steps: watch execution logs for poll timestamps of one device; reserve
  it (tier flips to in_use); later release it (tier flips back).
- Expected: observed poll spacing matches the configured interval for
  each tier, and the flip happens on the reservation lifecycle event, not
  on a poll boundary.

## Explicitly automated instead (do not add here)

The following were considered for this list and rejected because the
automated suites cover them: bulk import/export round-trips (integration),
driver upload/delete (integration + e2e), port-conflict 409s
(integration; two-user UI flow tracked as a proposed e2e case), DLQ and
redelivery behavior (integration with mock drivers), config precedence
ladder (unit + integration).
