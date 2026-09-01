# Testing Gaps

Tracking doc for test coverage not yet implemented. Shipped work lives under
the usual service test directories; this file is the backlog.

Last audit: 2026-05-04, refreshed 2026-09-01. When you close a gap, delete the
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

**33 page test files exist today**, covering every page under `frontend/src/pages/`
except `ReservationCalendarPage`. The 2026-08-30 coverage batch (PRs #650 to #660)
added direct suites for the seven pages this register used to list as untested
(`AddDevicePage`, `DeviceGroupsPage`, `GroupDetailPage`, `GroupsPage`, `UsersPage`,
`TopologyPage`, `TopologyTemplatesPage`), plus three more `TopologyEditorPage`
suites (`TopologyEditorPage.HistoryAndSave`, `TopologyEditorPage.ElementAttachAndDrop`,
`TopologyEditorPage.AIProposal`) covering the base editor flows this register used
to call out as missing: device drop, element attach, AI-proposal edge mapping,
version-history preview/restore, save-as-template, and pathfind-status reconcile.
`TopologyEditorPage.tsx` itself is now at 96.4% lines.

- [HIGH] `frontend/src/pages/ReservationCalendarPage.tsx` (3.8% lines, no test
  file). Calendar rendering, month navigation, and reservation-click handling are
  all untested.

Measured frontend coverage on main is 89.7% lines (1,211 tests, 129 test files).
The files below 85% lines, all predating v0.2.0, grouped by area with a note on
what a test would still need to cover:

- [MEDIUM] `frontend/src/pages/ConfigPage.tsx` (30.6%): has a suite covering
  initial render, but the save-and-restart flow (compose-project self-check plus
  restart confirmation) is untested.
- [LOW] `frontend/src/App.tsx` (0%): no test imports it; router wiring,
  `ErrorBoundary`, and the `Toaster` mount are exercised only indirectly through
  page tests that render individual routed components.
- [MEDIUM] Device-config panels: `frontend/src/components/device-config/ApplyJobsPanel.tsx`
  (6.7%, exercised only indirectly through `DeviceConfigSection.test.tsx`) and
  `frontend/src/components/device-config/DeviceConfigSection.tsx` (49.4%, has its
  own suite). The schedule-apply submit and job-status-polling branches are the
  deferred piece, the same UI-journey gap the E2E section below already tracks.
- [MEDIUM] AI assistant tabs: `frontend/src/components/reservations/AIAssistantTab.tsx`
  (77.8%, has a suite) and `frontend/src/components/reservations/AIAssistantTabLegacy.tsx`
  (68.4%, no suite of its own, the default render path since
  `VITE_AI_CHAT_ENABLED` defaults false, exercised only indirectly through
  `AIAssistantTab.test.tsx`). Streaming-error and multi-turn branches are the
  untested remainder.
- [LOW] Topology editor and canvas dialogs: `frontend/src/components/topology-editor/AIDialog.tsx`
  (52.6%, has a suite), `frontend/src/components/topology-editor/VersionDiffDialog.tsx`
  (63.6%, has a suite), `frontend/src/components/topology-editor/RestoreConfirmDialog.tsx`
  (77.8%, no suite of its own, exercised only through the `TopologyEditorPage`
  suites that mount it), and `frontend/src/components/ui/FloatingPanel.tsx`
  (40.6%, same: no direct suite).
- [LOW] `frontend/src/components/devices/PortsSection.tsx` (68.4%) and
  `frontend/src/components/devices/DynamicFieldRenderer.tsx` (83.3%): both have a
  targeted suite; validation and edge-case branches are the remainder.
- [MEDIUM] `frontend/src/components/admin/UserManagementTable.tsx` (0%):
  `UsersPage.test.tsx` mocks it out entirely rather than rendering it, so its
  fetch, sort, and promote/demote actions have no test at all.
- [LOW] `frontend/src/components/NotificationBell.tsx` (60.5%) and
  `frontend/src/components/ui/BulkImportExport.tsx` (83.3%): both have a
  targeted suite; dropdown and export-format branches are the remainder.

A file with no test importing it (`App.tsx`, and effectively
`UserManagementTable.tsx`) now shows 0% rather than being omitted from the report:
PR #658 switched the vitest coverage config to include-based reporting, so an
untested file surfaces at 0% instead of staying invisible.

`CreateReservationModal`, `ReservationDetailModal`, `AIProposalBar`,
`AICommitDialog`, `EditDevicesModal`, `AppLayout`, and `FieldRow` all carry
targeted suites above the 85% line-coverage threshold; no gap. The dead `TopologyEditor`
component was deleted in issue #489, and the dead `DeviceDetailModal` component
was deleted in PR #600; device detail is rendered by `DevicePage.tsx` as a full
page, not a modal.

## Frontend: API clients

Tests live under `frontend/src/test/api/`; every client under `frontend/src/api/`
now has a test file (the 2026-08-30 batch added `recipes.ts`, now 100%). Four
clients have a suite but stay below 85% lines on error and edge-case branches:

- [MEDIUM] `api/deviceConfig.ts` (71.4%), `api/notifications.ts` (72.4%).
- [LOW] `api/config.ts` (80.0%), `api/reporting.ts` (82.6%).

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
