# Testing Gaps

Tracking doc for test coverage not yet implemented. Shipped work lives under
the usual service test directories; this file is the backlog.

Last audit: 2026-05-04. When you close a gap, delete the entry. When a new one
surfaces, add it with a severity tag (CRITICAL, HIGH, MEDIUM, LOW) and a
concrete target file path.

## Contract and drift

- **[MEDIUM] Generated TypeScript types.** No pipeline generates `frontend/src/types/*`
  from the backend OpenAPI. Manual drift between Pydantic models and TS interfaces
  is caught only at runtime. Consider `openapi-typescript` against each service.
  Note: `tests/contract/test_openapi_schema.py` already pins backend shape against
  committed snapshots, so unintended backend-side drift surfaces in code review;
  this gap is about the missing forward-generation step into the frontend.

## Security and cross-cutting

- **[MEDIUM] TLS/Traefik cert chain verification.** Zero tests touch the
  traefik config at `infra/traefik/` or verify that `https://localhost`
  serves the expected cert chain. Target: a small integration check that
  fetches `/api/auth/healthz` with `verify=True` against the committed CA.
- **[MEDIUM] Alembic up/down tests.** Migrations run implicitly through the
  integration stack setup; no explicit test verifies `alembic upgrade head`
  from empty and `alembic downgrade -1` against every revision per service.
  Target: `services/<svc>/tests/test_migrations.py` per service that owns a schema.
- **[MEDIUM] DB enum rejection on Postgres.** `services/inventory/tests/test_storage_constraints.py`
  covers uniqueness, FK, and cascade on SQLite; Postgres-native enum rejection
  of out-of-range values is not exercised. Target: gate a Postgres fixture in
  the inventory integration flow and add `test_invalid_enum_rejected_by_postgres`.

## NATS and background workers

- **[MEDIUM] Execution consumer: inventory/cabling 5xx during fetch.** Covered
  in principle by the existing transient-error NAK test, but no concrete test
  drives an HTTP 5xx from `_fetch_device` / `_fetch_connections_for_device`
  through to NAK and then DLQ after max-deliver. Target: extend
  `services/execution/tests/test_nats_consumer.py`.

## AI orchestrator

- **[MEDIUM] committer.py rollback edge cases.** `test_commit.py` covers the
  canvas-save-fail and reservation-fail rollback paths. Not covered: rollback
  when the rollback DELETE itself fails (what does the user see?), and
  rollback when the /execute admin-required path is hit on a partial commit.
- **[LOW] extractor.py malformed-output resilience.** `test_extractor.py` exists
  but has no "LLM returned a tool_use with missing required keys" test.

## Frontend: pages and components

**13 pages have zero Vitest coverage.** Only `DriversPage` is tested.

- [HIGH] Page smoke tests (render, key interactions):
  - `frontend/src/pages/LoginPage.tsx`
  - `frontend/src/pages/RegisterPage.tsx`
  - `frontend/src/pages/ReservationsPage.tsx`
  - `frontend/src/pages/ReservationCalendarPage.tsx`
  - `frontend/src/pages/SettingsPage.tsx`
  - `frontend/src/pages/TopologyEditorPage.tsx`
  - `frontend/src/pages/ConfigPage.tsx`
- [MEDIUM] Remaining pages: Dashboard, Device, Inventory, Templates, TemplateEditor, Reporting, Topology.

**21 components have zero coverage.** Priorities:

- [HIGH] `TopologyEditor`, `AIDialog`, `AIProposalBar`, `AICommitDialog` (complex
  logic paths, state machines).
- [MEDIUM] `CreateReservationModal`, `ReservationDetailModal`, `EditDevicesModal`,
  `DeviceDetailModal`, `PortsSection`.
- [LOW] `AppLayout`, `FloatingPanel`, `FieldRow` (lower-complexity presentation).

## Frontend: API clients (8 still untested)

Tests live under `frontend/src/test/api/`. Currently covered after this round:
`client`, `ai`, `notifications`, `reporting`, `auth`, `reservations`, `inventory`,
`config`, `userProfile`. Remaining:

- [MEDIUM] `acl.ts`, `admin.ts`, `connections.ts`, `deviceGroups.ts`,
  `drivers.ts`, `groups.ts`, `ports.ts`, `templates.ts`, `topologies.ts`.

## Frontend: stores and hooks

- [MEDIUM] `frontend/src/stores/configStore.ts` is the only untested store.
- `frontend/src/hooks/` does not exist; no gap.

## E2E

`tests/e2e/` has 30 files and 116 tests. Remaining UI gaps:

- [HIGH] AI Generate generate-to-commit flow. `tests/e2e/test_ai_generate_dialog.py`
  covers opening the dialog, the empty-prompt disabled state, and escape-to-close,
  but submitting a prompt, accepting the proposal, and committing is a deferred
  E2E gap until the LLM call can be stubbed at the network layer (the integration
  test `tests/integration/test_ai_status.py::test_ai_generate_succeeds_when_key_present`
  exercises the live LLM path).
- [HIGH] Device-config edit -> schedule -> result UI flow. Backend (inventory +
  execution + scheduler) is unit-tested; the user-visible journey of editing
  a config, scheduling an apply, and watching the job status update is not
  (`tests/e2e/test_device_config_apply.py` covers the device-detail page, the
  config section, and the apply-jobs panel rendering, but the schedule submit is
  a deferred E2E gap).
- [MEDIUM] LDAP login E2E (depends on stack configured for LDAP + live directory).
- [MEDIUM] Pathfind UI: "Find Path" button, result rendering, dedup display.
- [MEDIUM] Device detail modal edit workflows (not the passive view already tested).

## Load tests

`tests/load/locustfile.py` covers auth login, reservations, inventory list/detail,
templates list, and ACL check. Missing coverage (~56% of API surface):

- [MEDIUM] `topology_*` endpoints (read and write).
- [MEDIUM] Drivers: list, upload, download.
- [LOW] Reporting, pathfinding, AI, notifications, config, user-profile.

## Notes

- All items are actionable; each has a target file or clearly-identified
  subject under test. Severity reflects impact of regression, not effort to
  implement.
- Before picking up an item, rerun the relevant service's test command
  (`make test-<svc>`) to make sure the baseline is green.
