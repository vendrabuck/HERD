# Testing Gaps

Tracking doc for test coverage not yet implemented. Shipped work lives under
the usual service test directories; this file is the backlog.

Last audit: 2026-05-04, refreshed 2026-08-16. When you close a gap, delete the
entry. When a new one surfaces, add it with a severity tag (CRITICAL, HIGH,
MEDIUM, LOW) and a concrete target file path.

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
  drives an HTTP 5xx from `_fetch_device` / `_fetch_fork_intended_wires`
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

**20 page test files exist today** (`ConfigPage`, `ConnectionsPage`,
`DeviceGroupDetailPage`, `DevicePage`, `DriversPage`, `GrantsPage`, `HypervisorsPage`,
`InventoryPage`, `LdapSyncPage`, `LoginPage`, `RegisterPage`, `ReportingPage`,
`ReservationsPage`, `SettingsPage`, `TemplateEditorPage`, `TemplatesPage`, plus four
TopologyEditorPage suites: `TopologyEditorForkMode`, `TopologyEditorDynamicPlaceholders`,
`TopologyEditorNetworkElements`, and `TopologyEditorWiring`).

- [HIGH] Page smoke tests (render, key interactions):
  - `frontend/src/pages/ReservationCalendarPage.tsx`
  - `frontend/src/pages/TopologyEditorPage.tsx` beyond the fork-mode,
    dynamic-placeholder, network-element, and wiring-dialog suites: the base editor
    flows (device drop, edge draw, parent save) still lack direct page-level coverage.
- [MEDIUM] Remaining pages: `TopologyPage`, `TopologyTemplatesPage`, and the
  admin-only `AddDevicePage`, `DeviceGroupsPage`, `GroupDetailPage`, `GroupsPage`, and
  `UsersPage`.

**Component coverage priorities** (`CreateReservationModal`, `ReservationDetailModal`,
`AIDialog`, `AIProposalBar`, `AICommitDialog`, `EditDevicesModal`,
and `PortsSection` now all carry targeted suites; the dead `TopologyEditor` component
was deleted in issue #489, and the dead `DeviceDetailModal` component was deleted in
PR #600; device detail is rendered by `DevicePage.tsx` as a full page, not a modal):

- [LOW] `AppLayout`, `FloatingPanel`, `FieldRow` (lower-complexity presentation, still
  untested).

## Frontend: API clients (1 still untested)

Tests live under `frontend/src/test/api/`. Every client under `frontend/src/api/` is
covered except:

- [MEDIUM] `recipes.ts`.

## Frontend: stores and hooks

- All four stores under `frontend/src/stores/` (`authStore`, `configStore`,
  `preferencesStore`, `topologyStore`) now carry tests; no gap.
- `frontend/src/hooks/` does not exist; no gap.

## E2E

`tests/e2e/` has 52 files and 163 tests (123 Selenium, 40 Playwright). Remaining UI gaps:

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
- [MEDIUM] Pathfind UI: "Find Path" button, result rendering, dedup display.
- [MEDIUM] Device detail modal edit workflows (not the passive view already tested).

## Load tests

`tests/load/locustfile.py` covers auth login, reservations, inventory list/detail,
templates list, ACL check, bulk export (devices/templates/topologies, JSON and CSV,
plus a dry-run import), and notifications (unread-count, list, preferences write).
Missing coverage:

- [MEDIUM] Topology write endpoints (create, update, canvas save, versions) beyond
  the bulk export path.
- [MEDIUM] Drivers: list, upload, download.
- [LOW] Reporting, pathfinding, AI, config.

## Notes

- All items are actionable; each has a target file or clearly-identified
  subject under test. Severity reflects impact of regression, not effort to
  implement.
- Before picking up an item, rerun the relevant service's test command
  (`make test-<svc>`) to make sure the baseline is green.
