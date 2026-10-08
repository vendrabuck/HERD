# Device configuration specification

| | |
|---|---|
| Area prefix | `CFG` (used in rule identifiers, for example `CFG-VER-1`) |
| Verified at | commit `cd3eaeed` (`v0.6.0-215-gcd3eaeed`), 2026-10-08 |
| Owning services | inventory (`services/inventory/`: config versions, apply jobs, the apply scheduler, the config-schema proxy); execution (`services/execution/`: driver actions, execution runs and their command transcripts, driver loading and the driver cache, the driver sandbox, the package validator); `services/common/herd_common/` (`device_config.py`, the manage-or-owner checks in `acl.py`); the device page's configuration section in `frontend/` |
| Other services involved | acl (explicit `manage` grants), reservations (active-reservation ownership, reservation status, the restore guard's by-device lookup), ai-orchestrator (calls `POST /execute` and `POST /internal/validate-package`; its assistant tools write versions and schedule dry runs), cabling and execution's wiring consumer (read the latest config version of a Layer 3 switch) |
| Design records | [ADR 0002](../design/0002-driver-published-config-schemas.md), [ADR 0005](../design/0005-ai-recipe-authoring.md), [ADR 0014](../design/0014-first-class-layer-3-routing.md) |
| Related guides | [DRIVERS.md](../DRIVERS.md) (connection-type contracts, dry-run support, the sandbox), [ROLES.md](../ROLES.md) (reservation-owner widening for device-config writes, the execution service), [USER_GUIDE.md](../USER_GUIDE.md), [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md), [ENV_VARS.md](../ENV_VARS.md) |

All inventory paths below are the inventory service's own paths; through the gateway they
are prefixed with `/api/inventory`. All execution paths are prefixed with
`/api/execution`.

This document depends on five others. The device and driver package records, driver
upload, and the device visibility helper itself are in `inventory.md` (rules INV-VIS-1
to INV-VIS-9, INV-DRV-1 to INV-DRV-18). How execution's wiring consumer drives switches,
judges a driver's result, classifies a driver load failure, and builds a driver context
is in `provisioning-and-wiring.md` (rules WIRE-DRIVER-1 to WIRE-DRIVER-7); how it reads
a Layer 3 switch's latest config version is WIRE-L3-4 and WIRE-L3-16 there, and how
cabling validates routing intent against it is TOPO-L3-7 and TOPO-L3-11 in
`topology.md`. The recipe result rules the package validator shares with the dynamic
consumer are DYN-RESULT-1 to DYN-RESULT-3 in `dynamic-resources.md`. The assistant tools
that write versions and schedule dry runs (AI-WRITE-2, AI-WRITE-3), the AI commit's
config push (AI-COMMIT-15 to AI-COMMIT-17), and the recipe-drafting loop that calls the
validator (AI-RECIPE-8) are in `ai-features.md`. The acl service's own check routes are
IAM-ACL-7 to IAM-ACL-12 in `identity-and-access.md`.

## 1. Purpose

A lab device's configuration is kept as a numbered history of snapshots that anyone who
can see the device can read and compare, and that the people who manage the device can
add to, roll back, and push to the device now or at a scheduled time, optionally as a
dry run whose commands are captured for review first. Behind that sits execution's
ability to run any driver method in a separate, resource-limited process and record what
happened. This area does not decide when a reservation's wiring is built
(`provisioning-and-wiring.md`), does not poll device health
(`operations-and-observability.md`), and does not store driver packages
(`inventory.md`).

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). Every user-facing route here reads the
role from the JWT claim alone. Rules beyond role (device visibility, the manage grant,
reservation ownership) are numbered rules in section 8.

| Actor | May | May not |
|---|---|---|
| Unauthenticated caller | Nothing (401) | Anything |
| User | Read the config versions, diffs, and apply jobs of a device visible to them; read any driver's config schema; with a `manage` grant on the device or an active reservation of it, create and restore versions, apply now, schedule, and confirm a dry run; cancel their own pending jobs; with a `manage` grant, run `configure` through `POST /execute`; list the runs of a reservation they own and read those runs' command transcripts | See a hidden device's history; run any driver action other than `configure`; read a single run's detail; retry a run |
| Admin | Everything on every device; run any driver action; list, read, and retry any run | Apply to a device whose driver contract has no `configure` (CFG-GATE-2, CFG-GATE-4) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Read a device's latest config version and its apply-job summary; read a driver's published schema; run `configure` through `POST /execute/internal`; validate a package | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Config version | One numbered snapshot of a device's configuration: the config object, the connection type it was validated against, a free-text description, the author, an optional `restored_from_id`, and the id of the last run that applied it | inventory | `device_config_versions` (`DeviceConfigVersion` in `services/inventory/app/models/device_config_version.py`); `last_apply_run_id` is a bare execution id, no foreign key |
| Current config pointer | `devices.current_config_version_id`, the version an immediate apply last applied with success | inventory | `devices` |
| Config schema registry | HERD's own JSON Schema for the `configure` input of three connection types | common | `CONFIG_SCHEMAS` in `services/common/herd_common/device_config.py` |
| Published config schema | A schema a driver returns from its `config_schema()` classmethod, preferred over the registry | execution (captured), inventory (proxied) | `driver_cache.config_schema_json` (`DriverCache` in `services/execution/app/models/driver_cache.py`) |
| Apply job | A scheduled push of one version to its device, optionally a dry run and optionally tied to a reservation | inventory | `device_config_apply_jobs` (`DeviceConfigApplyJob` in `services/inventory/app/models/device_config_apply_job.py`); `reservation_id` and `run_id` are bare ids of other services |
| Execution run | One driver method call: device, driver and its SHA256, action, acting user, optional reservation, input, output, error, timing, status | execution | `execution_runs` (`ExecutionRun`); `device_id`, `driver_id`, `reservation_id`, `user_id` are bare ids |
| Command transcript | The per-command rows a driver records during one run | execution | `execution_command_log` (`ExecutionCommand`), cascading with its run |
| Driver cache | The extracted package, its metadata, and its published schema, one row per driver, valid for one SHA256 | execution | `driver_cache` plus the directory under `DRIVER_CACHE_PATH` |
| Driver metadata | `driver_metadata.json` capability flags; `supports_dry_run` matters here | execution | `driver_cache.metadata_json` |
| Validation report | The verdict on an unapproved recipe package; nothing is stored | execution | the response only |

## 4. State model

Two lifecycles live here: the apply job's and the execution run's. A config version has
no status.

**Statuses.**

Apply job:

- `pending`: waiting for its time, or re-queued by the stale sweep.
- `running`: claimed by a scheduler and being fired.
- `success`: execution answered a run whose status is `SUCCESS`.
- `failed`: anything else went wrong.
- `skipped`: the job's reservation was not active, or its creator no longer had authority.
- `cancelled`: cancelled by its creator or an admin while pending.

Execution run (as written by `run_driver_action`; the wiring and dynamic-instance flows
write their own runs, `provisioning-and-wiring.md` and `dynamic-resources.md`):

- `PENDING`: the row exists; the driver is not loaded yet.
- `RUNNING`: the sandbox call has started.
- `SUCCESS`, `FAILED`, `TIMEOUT`: the outcome.

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | job `pending` | `POST /devices/{id}/config-versions/{vid}/schedule`; `POST /apply-jobs/{id}/confirm` (a new row) | CFG-JOB-1 to CFG-JOB-7; CFG-JOB-11 | nothing | CFG-STATE-1 |
| job `pending` | job `running` | the apply scheduler (`fire_job`) | conditional update on `pending` | nothing | CFG-STATE-2 |
| job `pending` | job `cancelled` | `DELETE /apply-jobs/{id}` | creator or admin; read as `pending` | nothing | CFG-STATE-3, CFG-STATE-4 |
| job `running` | job `pending` | the stale sweep | `fired_at` null and `scheduled_for` over 300 seconds ago | nothing | CFG-STATE-5 |
| job `running` | job `skipped` | the apply scheduler | reservation not active, or creator not authorized | nothing | CFG-STATE-6 |
| job `running` | job `success` or `failed` | the apply scheduler | the execute outcome | nothing | CFG-STATE-7 |
| any job status | job `failed` | the scheduler loop after `fire_job` raised | none (by id) | nothing | CFG-STATE-8 |
| (none) | run `PENDING` | `run_driver_action` | the action gate passed | nothing | CFG-RUNSTATE-1 |
| run `PENDING` | run `FAILED` | `run_driver_action` | driver load failed, or `configure` input refused | nothing | CFG-RUNSTATE-2 |
| run `PENDING` | run `RUNNING` | `run_driver_action` | none | nothing | CFG-RUNSTATE-3 |
| run `RUNNING` | run `FAILED` | `run_driver_action` | dry run refused | nothing | CFG-RUNSTATE-3 |
| run `RUNNING` | run `SUCCESS`, `FAILED`, or `TIMEOUT` | `run_driver_action` | the sandbox result | nothing | CFG-RUNSTATE-4 |

**Concurrency.** Only the scheduler's claim is a compare-and-swap. `_due_jobs` also
selects with `FOR UPDATE SKIP LOCKED` on Postgres, but the lock is released at the first
commit inside `fire_job`, so the claim is what keeps two schedulers off one job. Every
other job write and every run write reads the row and overwrites it.

**Rules.**

- **CFG-STATE-1.** A job is created `pending` by a schedule, and by a confirm, which
  writes a new row and leaves its source job unchanged. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`, `confirm_dry_run_apply`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_schedule_apply_job`); `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_promotes_dry_run_to_real_apply`, `test_confirm_leaves_source_dry_run_intact`)
- **CFG-STATE-2.** The scheduler claims a due job with an update guarded on
  `status = 'pending'`; when it changes no row, another scheduler won and this one fires
  nothing. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`, `_due_jobs`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_bails_when_already_claimed`, `test_due_jobs_returns_only_pending_and_due`)
- **CFG-STATE-3.** A cancel sets a job read as `pending` to `cancelled`; any other status
  is refused (CFG-JOB-10). \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`cancel_apply_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_cancel_pending_job`, `test_cancel_already_cancelled_job`)
- **CFG-STATE-4.** The cancel write carries no status guard, so a cancel that commits
  after the scheduler's claim answers 204 while the job still fires, and the job ends
  with whichever of the two writes committed last. Known gap, see #1088. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`cancel_apply_job`) \
  Pinned by: none (#1088)
- **CFG-STATE-5.** Each scheduler tick first returns to `pending` every `running` job
  whose `fired_at` is null and whose `scheduled_for` is more than
  `STALE_RUNNING_AFTER_SECONDS` (300) in the past. The age is measured from
  `scheduled_for`, not from the claim, so a job claimed late (a backlog or an outage) can
  be re-queued while another scheduler is still firing it. Known gap, see #1089. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_resweep_stale_running`, `STALE_RUNNING_AFTER_SECONDS`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_resweep_stale_running_requeues`, `test_resweep_leaves_fresh_running_alone`, `test_resweep_leaves_terminal_jobs_alone`)
- **CFG-STATE-6.** A claimed job becomes `skipped`, with its reason in `error` and
  `fired_at` set, when its reservation is not active (CFG-SCHED-5) or its creator fails
  the fire-time authority check (CFG-SCHED-6). \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_skipped_when_reservation_not_active`, `test_fire_job_skips_when_creator_no_longer_authorized`)
- **CFG-STATE-7.** Otherwise a claimed job becomes `failed` (version gone, driver cannot
  configure, or any non-success execute outcome) or `success`, with `run_id`, `error`,
  and `fired_at` written together. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`, `_post_internal_execute`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_success`, `test_fire_job_failed_when_execution_returns_500`, `test_fire_job_fails_when_version_was_deleted`, `test_fire_job_fails_when_driver_cannot_configure`)
- **CFG-STATE-8.** When `fire_job` raises, the loop marks that job `failed` with
  `scheduler crash` from a fresh session, by job id and with no status guard, and moves
  on to the next job; a failure of that write is only logged. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`run_scheduler_loop`, `_mark_failed_in_fresh_session`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_run_scheduler_loop_recovers_when_fire_job_crashes`, `test_mark_failed_in_fresh_session_records_failure`, `test_mark_failed_in_fresh_session_swallows_session_error`)
- **CFG-RUNSTATE-1.** `run_driver_action` writes the run row as `PENDING` only after the
  action gate (CFG-GATE-4) passed, and before it loads the driver. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `create_execution_run`) \
  Pinned by: `services/execution/tests/test_configure_capability_gate.py` (`test_run_driver_action_refuses_before_create_execution_run`); `services/execution/tests/test_execution_crud.py` (`test_create_execution_run`)
- **CFG-RUNSTATE-2.** A driver load failure (CFG-EXEC-9) or a refused `configure` input
  (CFG-EXEC-10) moves the run from `PENDING` straight to `FAILED`. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `update_execution_run`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_driver_package_error_records_failed`); `services/execution/tests/test_router_endpoints.py` (`test_manual_execute_validates_configure_kwargs`, `test_run_driver_action_driver_load_failure`)
- **CFG-RUNSTATE-3.** The run is set `RUNNING` with `started_at` before the sandbox
  call; a dry run the sandbox refuses (CFG-DRY-1) ends `FAILED` with
  `dry-run refused: <reason>` and `completed_at`. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_dry_run_refused_records_failed`)
- **CFG-RUNSTATE-4.** When the child exited cleanly the run is `SUCCESS`, or `FAILED`
  when the driver's own result reports failure (the rule of WIRE-DRIVER-3 in
  `provisioning-and-wiring.md`), with the output stored either way; when the child did
  not, the run is `TIMEOUT` if the sandbox error contains `timed out`, else `FAILED`. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `driver_result_failed`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_run_driver_action_success`, `test_run_driver_action_execution_timeout`, `test_run_driver_action_execution_failure`); `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_driver_result_failure_records_failed`, `test_run_driver_action_bare_data_output_stays_success`, `test_run_driver_action_falsy_success_value_records_failed`)
- **CFG-RUNSTATE-5.** An exception that escapes `run_driver_action` after the row is
  written (any load error other than `DriverPackageError`, `ValueError`, or
  `RuntimeError`, for example a database error) leaves the run `PENDING` or `RUNNING`
  for good; nothing sweeps such rows. Known gap, see #1097. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: none (#1097)

## 5. API surface

Inventory routes:

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| GET | `/devices/{id}/config-versions` | any signed-in user (non-admins: visible devices only) | 200 | CFG-AUTH-1, CFG-AUTH-5, CFG-VER-6 |
| GET | `/devices/{id}/config-versions/diff?from&to` | any signed-in user (non-admins: visible devices only) | 200 | CFG-AUTH-1, CFG-AUTH-5, CFG-VER-8 |
| GET | `/devices/{id}/config-versions/{vid}` | any signed-in user (non-admins: visible devices only) | 200 | CFG-AUTH-1, CFG-AUTH-5, CFG-VER-7 |
| POST | `/devices/{id}/config-versions` | admin, or a user with `manage` or an active reservation of the device | 201 | CFG-AUTH-3, CFG-AUTH-8, CFG-VER-1 to CFG-VER-5, CFG-GATE-3 |
| POST | `/devices/{id}/config-versions/{vid}/restore` | as create | 201 | CFG-AUTH-3, CFG-AUTH-8, CFG-VER-4, CFG-VER-9 to CFG-VER-13 |
| POST | `/devices/{id}/config-versions/{vid}/apply` | as create | 200 | CFG-AUTH-3, CFG-AUTH-8, CFG-GATE-2, CFG-APPLY-1 to CFG-APPLY-6 |
| POST | `/devices/{id}/config-versions/{vid}/schedule` | as create | 201 | CFG-AUTH-3, CFG-AUTH-8, CFG-GATE-2, CFG-JOB-1 to CFG-JOB-7 |
| GET | `/devices/{id}/apply-jobs` | any signed-in user (non-admins: visible devices only) | 200 | CFG-AUTH-5, CFG-JOB-8 |
| GET | `/apply-jobs/{id}` | any signed-in user (non-admins: jobs of visible devices only) | 200 | CFG-AUTH-5, CFG-JOB-9 |
| POST | `/apply-jobs/{id}/confirm` | as create, on the job's device | 201 | CFG-AUTH-3, CFG-JOB-11 to CFG-JOB-13 |
| DELETE | `/apply-jobs/{id}` | the job's creator, or an admin | 204 | CFG-STATE-3, CFG-JOB-10 |
| GET | `/drivers/{id}/config-schema` | any signed-in user | 200 | CFG-SCHEMA-9 |

Execution routes:

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| POST | `/execute` | admin (any action); a user with a `manage` grant (`configure` only) | 201 | CFG-EXEC-1 to CFG-EXEC-5, CFG-EXEC-7 to CFG-EXEC-13, CFG-GATE-4 |
| GET | `/runs` | admin; a user, with the `reservation_id` of a reservation they own | 200 | CFG-RUN-1 to CFG-RUN-3 |
| GET | `/runs/{id}` | admin | 200 | CFG-RUN-4 |
| GET | `/runs/{id}/commands` | admin; a user who owns the run's reservation | 200 | CFG-TX-5 |
| POST | `/runs/{id}/retry` | admin | 200 | CFG-RUN-5, CFG-RUN-6 |

`GET /runs` takes `device_id`, `reservation_id`, `status`, `created_after`,
`created_before`, `skip` (default 0), and `limit` (1 to 500, default 50) and answers
`{items, total, skip, limit}`. The config-version and apply-job lists take `skip` and
`limit` with the same bounds. A run carries `id`, `device_id`, `driver_id`,
`driver_sha256`, `action`, `status`, `reservation_id`, `user_id`, `input_params`,
`output` (a JSON string), `error`, `port_a`, `port_b`, `started_at`, `completed_at`,
`duration_ms`, and `created_at`. A config version carries `id`, `device_id`,
`version_number`, `connection_type`, `description`, `created_by`, `author_name`,
`created_at`, `restored_from_id`, and `last_apply_run_id`, plus `config` on the detail
and on a create or restore answer.

## 6. Events

None. Inventory has no NATS connection, and the driver-action routes publish nothing.

## 7. Internal API

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| GET | `/devices/{id}/config-versions/latest/internal` (inventory) | `X-Internal-Token` | execution's wiring consumer, cabling's routing-intent validation | the highest-numbered version, `config` included | CFG-VER-14 |
| GET | `/devices/{id}/apply-jobs/internal` (inventory) | `X-Internal-Token` | ai-orchestrator (purpose classification) | `{count, names}` | CFG-JOB-14 |
| GET | `/drivers/{id}/config-schema?sha256&filename&connection_type` (execution) | `X-Internal-Token` | inventory (the schema proxy and config validation) | `{driver_id, sha256, has_schema, schema, source}` | CFG-SCHEMA-10 |
| POST | `/execute/internal` (execution) | `X-Internal-Token` | inventory's apply scheduler | the run | CFG-EXEC-6, CFG-GATE-4 |
| POST | `/internal/validate-package` (execution) | `X-Internal-Token` | ai-orchestrator (recipe drafting) | the validation report | CFG-VAL-1 to CFG-VAL-9 |

Execution's `POST /device-check` (an on-demand login, status, and logout of one device)
is specified in `operations-and-observability.md`, rules OPS-HEALTH-6 to OPS-HEALTH-10.

The inventory routes require the `X-Internal-Token` header: a missing header is a 422
and a wrong one 403 `Invalid internal token`. The execution routes answer 500
`Internal API token not configured` when execution has no token and 403
`Invalid internal token` when the header is missing or wrong (CFG-EXEC-6).

## 8. Features

### 8.1 Who may read and change a device's configuration

**What it does.** Anyone who can see a device can read its configuration history.
Changing it takes an admin, a user an admin granted `manage` on the device, or the owner
of an active reservation that holds the device.

**Surfaces.** The inventory routes of section 5; `services/inventory/app/services/manage_guard.py`
and `user_has_manage_or_owns_active_reservation` in
`services/common/herd_common/acl.py`. The visibility helper is INV-VIS-6 in
`inventory.md`.

**Rules.**

- **CFG-AUTH-1.** Every inventory route of this area answers 401 to a request with no
  bearer token or one that does not verify. \
  Enforced in: `services/inventory/app/dependencies/auth.py` (`get_current_user_payload`) \
  Pinned by: `services/inventory/tests/test_device_configs_rbac.py` (`test_anonymous_denied`)
- **CFG-AUTH-2.** A role claim of `admin` or `superadmin` passes every write check of
  this area without asking acl or reservations. \
  Enforced in: `services/inventory/app/services/manage_guard.py` (`_is_admin`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_create_config_version_happy_path`, `test_schedule_apply_job`); `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_promotes_dry_run_to_real_apply`)
- **CFG-AUTH-3.** Any other caller passes create, restore, apply, schedule, and confirm
  only when it holds an explicit `manage` grant on the device (acl `POST /check` with
  the caller's own token) or owns an `ACTIVE` reservation that holds the device
  (reservations `GET /internal/active?user_id&device_id` with the internal token);
  otherwise 403
  `manage permission required on this device (or active reservation ownership)`. \
  Enforced in: `services/inventory/app/services/manage_guard.py` (`_user_can_manage_device`); `services/common/herd_common/acl.py` (`user_has_manage_or_owns_active_reservation`, `_explicit_acl_manage`, `_owns_active_reservation`) \
  Pinned by: `services/inventory/tests/test_device_configs_rbac.py` (`test_non_admin_create_version_denied_without_acl_grant`, `test_non_admin_create_version_succeeds_with_acl_grant`, `test_non_admin_restore_denied_without_acl_grant`, `test_non_admin_apply_denied_without_acl_grant`, `test_non_admin_schedule_denied_without_acl_grant`, `test_non_admin_schedule_succeeds_with_acl_grant`); `services/inventory/tests/test_apply_jobs_reservation_owner.py` (`test_reservation_owner_can_schedule_without_explicit_grant`, `test_reservation_owner_can_create_config_version`, `test_non_owner_without_grant_still_rejected`); `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_non_admin_without_grant_rejected`, `test_confirm_non_admin_owner_allowed`); `services/common/tests/test_acl.py` (`test_explicit_grant_returns_true`, `test_no_explicit_grant_falls_through_to_reservation_check`, `test_no_grant_no_reservation_returns_false`)
- **CFG-AUTH-4.** The check fails closed. With no bearer token, or when acl is
  unreachable, answers non-200, or answers non-JSON, the grant counts as absent and the
  reservation check still runs; that check answers no when no internal token is
  configured or reservations is unreachable, non-200, or non-JSON. A 200 whose JSON body
  is not an object raises instead of answering no. Known gap, see #1096. \
  Enforced in: `services/common/herd_common/acl.py` (`user_has_grant`, `_owns_active_reservation`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_no_bearer_token_skips_acl_check_and_tries_reservations`, `test_acl_service_unreachable_still_tries_reservations`, `test_acl_5xx_falls_through_to_reservations`, `test_malformed_acl_response_falls_through_to_reservations`, `test_reservations_service_unreachable_returns_false`, `test_reservations_non_200_returns_false`, `test_malformed_reservation_response_returns_false`, `test_no_internal_token_skips_reservation_lookup`)
- **CFG-AUTH-5.** The version list, detail, and diff, the device's apply-job list, and the
  apply-job read are gated by device visibility, not by `manage`: a non-admin outside the
  device's groups gets the same 404 as an unknown id (`Device not found`, or
  `Apply job not found` on the job read, judged on the job's own device), an
  unanswerable visibility lookup is 503, and admins are not filtered. The device's
  existence is checked first. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`list_config_versions`, `diff_config_versions`, `get_config_version`); `services/inventory/app/routers/apply_jobs.py` (`list_apply_jobs`, `get_apply_job`); `services/inventory/app/services/device_visibility.py` (`check_device_read_visibility`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_list_config_versions_non_admin_denied_when_not_visible`, `test_list_config_versions_non_admin_allowed_when_visible`, `test_list_config_versions_admin_unfiltered`, `test_get_config_version_non_admin_denied_when_not_visible`, `test_diff_config_versions_non_admin_denied_when_not_visible`, `test_config_version_reads_404_detail_matches_device_read`); `services/inventory/tests/test_device_read_visibility_gate.py` (`test_hidden_device_404_matches_unknown_id_404`, `test_non_admin_inside_groups_sees_same_body_as_admin`, `test_visibility_unavailable_fails_closed_503`, `test_admin_never_consults_visibility`)
- **CFG-AUTH-6.** The write routes do not check visibility: a `manage` grant or an active
  reservation is enough, whatever the caller's device groups. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`create_config_version`, `restore_config_version`, `apply_config_version`); `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`, `confirm_dry_run_apply`) \
  Pinned by: none (#1100)
- **CFG-AUTH-7.** The fire-time check of a scheduled job asks the same two questions
  with no user token: acl `POST /internal/check` and reservations `GET /internal/active`,
  both with the internal token and a 5 second timeout; any failure on either answers no. \
  Enforced in: `services/common/herd_common/acl.py` (`user_has_manage_or_owns_active_reservation_internal`, `user_has_manage_internal`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_manage_or_reservation_internal_true_on_explicit_manage`, `test_manage_or_reservation_internal_true_via_reservation_fallback`, `test_manage_or_reservation_internal_false_when_both_deny`, `test_manage_or_reservation_internal_closed_when_both_unreachable`, `test_manage_internal_no_token_returns_false_without_calling`)
- **CFG-AUTH-8.** The write routes answer an unknown device with 404 `Device not found`
  and an unknown version, or one of another device, with 404 `Config version not found`.
  Create looks up the device, and apply and schedule the device and the version, before
  the authorization check; restore checks authorization between the two lookups. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`_load_device`, `_load_version`, `create_config_version`, `restore_config_version`, `apply_config_version`); `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_create_config_for_unknown_device`, `test_restore_not_found`, `test_schedule_apply_job_unknown_version`); `services/inventory/tests/test_router_edge_cases.py` (`test_schedule_unknown_device_404`)

**Out of scope.** The acl grant model and its check routes (`identity-and-access.md`);
how reservations answers `GET /internal/active` (`reservations.md`). The assistant's
write tools reach these routes with the user's own token, so the same rules apply to
them (`ai-features.md`, AI-WRITE-2, AI-WRITE-3).

### 8.2 Per-device config versioning

**What it does.** A device keeps a numbered history of configuration snapshots. A user
can add one, read any of them, compare two as a text diff, and restore an old one, which
adds a new snapshot carrying the old configuration.

**Surfaces.** User interface `frontend/src/components/device-config/DeviceConfigSection.tsx`
on the device page (8.12); the version routes of section 5 and the latest-version
internal route of section 7.

**Rules.**

- **CFG-VER-1.** A create stores the request's `config` and `description`, the
  connection type of the device's driver, `created_by` (the token's `sub`), and
  `author_name` (the token's `username`, empty when absent), numbers the version one
  more than the device's highest (1 for the first), and answers 201 with the version and
  its config. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`create_config_version`, `_next_version_number`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_create_config_version_happy_path`, `test_latest_internal_returns_highest_version`)
- **CFG-VER-2.** A device whose template has no driver, or whose driver has no
  connection type, cannot take a version: 422
  `Device has no driver-defined connection_type; cannot validate config`. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`_connection_type_for`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_create_config_version_without_driver_connection_type_422`)
- **CFG-VER-3.** The config is validated as 8.3 says, and a refusal is a 422 whose
  `detail` is the validator's message. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`_validate_config_for_device`, `create_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_create_config_version_validates`, `test_create_config_version_unsupported_connection_type`, `test_layer2_switch_vlan_assignments_validated`)
- **CFG-VER-4.** Creating or restoring a version never moves the device's current
  config pointer; only an immediate apply does (CFG-APPLY-4). \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`create_config_version`, `restore_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_create_version_does_not_flip_current_pointer`, `test_restore_does_not_flip_current_pointer`)
- **CFG-VER-5.** The next number is read as the current maximum plus one with no lock.
  The unique index on device and version number exists only on a schema built by
  inventory migration 0013; the model does not declare it, so a schema built fresh by
  `create_all` has none. Two concurrent writes for one device therefore either store
  the same number (fresh schema) or one fails with an unhandled 500 (migrated schema).
  Known gap, see #1095. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`_next_version_number`); `services/inventory/app/models/device_config_version.py` (`DeviceConfigVersion`); `services/inventory/migrations/versions/0013_device_config_versions.py` (`ix_device_config_versions_device_version`) \
  Pinned by: none (#1095)
- **CFG-VER-6.** The list answers the device's versions newest number first, without
  their `config`, as `{items, total, skip, limit}`. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`list_config_versions`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_versions_list_paginated`)
- **CFG-VER-7.** The detail answers one version with its `config`; a version id that is
  unknown or belongs to another device is 404 `Config version not found`. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`get_config_version`, `_load_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_get_config_version_detail`, `test_get_config_version_not_found`)
- **CFG-VER-8.** The diff needs both `from` and `to`, each a version of this device
  (404 otherwise), and answers `{version_a, version_b, diff}`, a unified diff of the two
  configs written as JSON with sorted keys and two-space indent, labelled `v<N>`; two
  identical configs give an empty string. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`diff_config_versions`); `services/inventory/app/services/config_diff.py` (`render_unified_diff`, `_canonicalize`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_diff_config_versions`, `test_diff_with_same_versions`)
- **CFG-VER-9.** A restore writes a new version that copies the source's `config` and
  `connection_type` and records `restored_from_id`, and answers 201. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`restore_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_restore_creates_new_version`); `tests/e2e/test_flows_effects_playwright.py` (`test_device_config_version_cycle`)
- **CFG-VER-10.** A restore without a `description` gets `Restored from v<N>`, N being
  the source's number. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`restore_config_version`) \
  Pinned by: none (#1100)
- **CFG-VER-11.** A restore validates the stored config again before writing: against
  the device's current published schema when there is one, else the registry entry for
  the source version's connection type. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`restore_config_version`, `_validate_config_for_device`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_restore_re_validates_against_published_schema`)
- **CFG-VER-12.** A restore is refused with 409
  `{"message": "Device has active reservations; restore blocked", "reservations": [{id, status, end_time}]}`
  while a reservation of another user in `PENDING`, `PENDING_PROVISION`, or `ACTIVE`
  holds the device (reservations `GET /internal/by-device/{id}`); the caller's own
  reservations neither block nor appear in the list. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`restore_config_version`); `services/inventory/app/services/reservation_guard.py` (`find_blocking_reservations_for_device`) \
  Pinned by: `services/inventory/tests/test_device_config_restore_reservation_guard.py` (`test_restore_blocked_by_active_reservation_of_another_user`, `test_restore_allowed_when_caller_owns_the_blocking_reservation`, `test_restore_blocked_mixed_owned_and_unowned_reservations_lists_only_others`, `test_restore_proceeds_and_calls_the_guard_when_no_blocking_reservations`, `test_find_blocking_reservations_filters_non_blocking_statuses`)
- **CFG-VER-13.** The restore guard fails closed with 503: a transport error is
  `reservations service unreachable while checking active reservations`, a non-200
  `reservations service returned an error while checking active reservations`, and a
  body that is not a JSON list of objects
  `reservations service returned an unparseable body while checking active reservations`. \
  Enforced in: `services/inventory/app/services/reservation_guard.py` (`find_blocking_reservations_for_device`) \
  Pinned by: `services/inventory/tests/test_device_config_restore_reservation_guard.py` (`test_restore_blocked_by_reservation_upstream_unreachable_503`, `test_find_blocking_reservations_transport_error_raises_503`, `test_find_blocking_reservations_non_200_raises_503`, `test_find_blocking_reservations_unparseable_body_raises_503`)
- **CFG-VER-14.** The internal latest-version read answers the device's highest
  version number with its config, which is not necessarily the applied one; an unknown
  device is 404 `Device not found` and a device with no versions 404
  `No config versions for device`. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`get_latest_config_version_internal`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_latest_internal_returns_highest_version`, `test_latest_internal_bad_token`, `test_latest_internal_missing_token`, `test_latest_internal_no_versions_404`, `test_latest_internal_unknown_device_404`)
- **CFG-VER-15.** No route deletes or edits a version; deleting the device deletes its
  versions and its apply jobs through the foreign keys. \
  Enforced in: `services/inventory/app/models/device_config_version.py` (`DeviceConfigVersion`); `services/inventory/app/models/device_config_apply_job.py` (`DeviceConfigApplyJob`) \
  Pinned by: none (#1100)

**Out of scope.** What a Layer 3 switch's latest version means for routing (ADR 0014;
`provisioning-and-wiring.md` WIRE-L3-4, `topology.md` TOPO-L3-7). The device delete
guard (`inventory.md`, INV-DEL-1 to INV-DEL-9), which shares the restore guard's lookup
(INV-DEL-6).

### 8.3 Config schemas: the registry and driver-published schemas

**What it does.** Every configuration HERD stores or pushes is checked first. A driver
may describe the configuration it accepts; when it does not, HERD's own schema for the
device's kind of driver applies.

**Surfaces.** `services/common/herd_common/device_config.py`;
`services/inventory/app/services/published_schema.py`; the schema extraction in
`services/execution/app/services/driver_loader.py`; `GET /drivers/{id}/config-schema` on
both services.

**Rules.**

- **CFG-SCHEMA-1.** The registry holds a schema for `Management` (`vlan` 1 to 4094,
  `ip`, `hostname`, `description`), `Layer 2 Switch` (`vlan_assignments` mapping names
  to VLANs 1 to 4094, `description`), and `Layer 3 Switch` (`interfaces`,
  `virtual_routers`, `routes`, `description`), each refusing unknown keys. \
  Enforced in: `services/common/herd_common/device_config.py` (`CONFIG_SCHEMAS`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_registry_covers_the_three_known_connection_types`, `test_every_schema_rejects_additional_properties`, `test_management_valid_config_passes`, `test_management_vlan_out_of_range_fails`, `test_layer2_valid_vlan_assignments_pass`, `test_layer3_full_valid_config_passes`, `test_layer3_interface_accepts_kind_and_port`, `test_layer3_route_requires_destination_and_interface`)
- **CFG-SCHEMA-2.** An empty or missing config passes without any check, on every
  connection type and against either kind of schema. \
  Enforced in: `services/common/herd_common/device_config.py` (`validate_device_config`, `validate_device_config_with_schema`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_empty_config_is_always_a_noop`, `test_with_published_schema_empty_is_noop`)
- **CFG-SCHEMA-3.** Against the registry, a non-empty config with no connection type,
  or with one the registry does not hold (`Layer 1 Switch`, `Hypervisor`, any other
  string), is refused. \
  Enforced in: `services/common/herd_common/device_config.py` (`validate_device_config`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_missing_connection_type_with_config_is_rejected`, `test_unknown_connection_type_with_config_is_rejected`)
- **CFG-SCHEMA-4.** A refusal reads `device '<name>': config failed schema validation: <message>`
  (`device:` without a name), the message being the schema library's best match. \
  Enforced in: `services/common/herd_common/device_config.py` (`validate_device_config`, `validate_device_config_with_schema`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_role_prefixes_error_message_when_provided`, `test_no_role_uses_bare_device_prefix`, `test_with_published_schema_rejects_with_role_prefix`)
- **CFG-SCHEMA-5.** A published schema is used only after cleaning: it must be a JSON
  object, any `$ref` that is not a local `#` pointer rejects it whole, every `$id`,
  `$anchor`, `$dynamicAnchor`, `$dynamicRef`, and `$schema` key is removed, and the
  result must be a valid draft 2020-12 schema. A rejected schema raises
  `PublishedSchemaError`, and every caller then validates against the registry instead. \
  Enforced in: `services/common/herd_common/device_config.py` (`_sanitize_published_schema`, `_is_local_ref`); `services/inventory/app/routers/device_configs.py` (`_validate_config_for_device`); `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_published_schema_with_remote_ref_is_rejected`, `test_published_schema_local_ref_is_allowed`, `test_published_schema_id_is_stripped`, `test_published_schema_invalid_schema_is_rejected`); `services/inventory/tests/test_device_configs.py` (`test_create_unsafe_published_schema_falls_back_to_registry`); `services/execution/tests/test_execution_service_edges.py` (`test_configure_unsafe_published_schema_falls_back_to_registry`)
- **CFG-SCHEMA-6.** A usable published schema replaces the registry entirely: it can
  accept a config the registry refuses and refuse one the registry accepts. \
  Enforced in: `services/common/herd_common/device_config.py` (`validate_device_config_with_schema`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_with_published_schema_overrides_registry`); `services/inventory/tests/test_device_configs.py` (`test_create_uses_published_schema_accept`, `test_create_published_schema_overrides_registry_reject`); `services/execution/tests/test_execution_service_edges.py` (`test_configure_accepts_commands_when_driver_publishes_schema`)
- **CFG-SCHEMA-7.** Inventory reads a driver's published schema from execution's
  internal config-schema route (CFG-SCHEMA-10) with the driver's SHA256, file name, and
  connection type and a 10 second timeout, and fails open: a transport error, a non-200,
  a non-JSON body, `has_schema` false, or a non-object `schema` all mean no published
  schema, and the registry applies. A 200 whose JSON body is not an object raises
  instead. Known gap, see #1096. \
  Enforced in: `services/inventory/app/services/published_schema.py` (`_fetch_published_schema`, `published_schema_for_device`) \
  Pinned by: `services/inventory/tests/test_published_schema.py` (`test_valid_200_parses_and_returns_schema`, `test_200_malformed_body_falls_back_to_none`, `test_200_has_schema_false_falls_back_to_none`, `test_non_200_falls_back_to_none_with_warning`, `test_transport_error_falls_back_to_none`, `test_published_schema_for_device_returns_none_when_no_driver`); `services/inventory/tests/test_device_configs.py` (`test_create_falls_back_to_registry_when_no_published_schema`, `test_create_fails_open_to_registry_when_execution_unreachable`)
- **CFG-SCHEMA-8.** Inventory keeps each answer in process for 30 seconds, keyed by
  driver id and SHA256, so replacing a driver's file never serves the old schema. \
  Enforced in: `services/inventory/app/services/published_schema.py` (`_fetch_published_schema`, `_MEMO_TTL_SECONDS`) \
  Pinned by: `services/inventory/tests/test_published_schema.py` (`test_memo_hit_within_ttl_skips_second_http_call`, `test_memo_is_keyed_per_driver_sha256`, `test_invalidate_memo_forces_a_fresh_fetch`)
- **CFG-SCHEMA-9.** `GET /drivers/{id}/config-schema` (inventory, any signed-in user)
  answers `{driver_id, connection_type, schema, source}`: the published schema as the
  driver returned it with `source` `driver`, else the registry entry with `source`
  `registry`, else `schema` null with `source` `none`; an unknown driver is 404
  `Driver package not found`. \
  Enforced in: `services/inventory/app/routers/drivers.py` (`get_driver_config_schema_proxy`); `services/inventory/app/services/published_schema.py` (`published_schema_for_driver`) \
  Pinned by: `services/inventory/tests/test_drivers.py` (`test_config_schema_proxy_returns_published`, `test_config_schema_proxy_falls_back_to_registry`, `test_config_schema_proxy_unknown_driver_404`)
- **CFG-SCHEMA-10.** Execution's internal config-schema route loads the driver (a cache
  hit for a known SHA256) and answers the cached schema with `has_schema` true and
  `source` `driver`, or `has_schema` false, `schema` null, `source` `none`; a load that
  fails with `DriverPackageError`, `ValueError`, or `RuntimeError` answers the same
  `has_schema` false with 200. \
  Enforced in: `services/execution/app/routers/executions.py` (`get_driver_config_schema_endpoint`) \
  Pinned by: `services/execution/tests/test_config_schema_endpoint.py` (`test_config_schema_requires_internal_token`, `test_config_schema_returns_published_schema`, `test_config_schema_no_published_schema`, `test_config_schema_fail_open_on_load_error`)
- **CFG-SCHEMA-11.** At load, execution reads `Driver.config_schema()` in the sandbox
  through the `__config_schema__` action, on the class without instantiating it and
  under the status timeout; no such method, a raise, a timeout, or a non-object answer
  stores no schema, and the load still succeeds. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`extract_config_schema_json`, `load_driver`); `services/execution/app/services/driver_sandbox.py` (`extract_config_schema`); `services/execution/app/services/_runner.py` (`main`) \
  Pinned by: `services/execution/tests/test_config_schema_extraction.py` (`test_extract_valid_schema_from_fixture`, `test_extract_no_schema_from_fixture`, `test_extract_schema_returning_non_dict`, `test_extract_schema_that_raises_is_failed_run`, `test_extract_does_not_instantiate_credential_dependent_init`, `test_extract_schema_timeout_surfaces_as_failed_run`); `services/execution/tests/test_driver_loader_load.py` (`test_load_driver_persists_published_config_schema`, `test_load_driver_no_schema_leaves_column_null`, `test_load_driver_broken_config_schema_loads_and_stores_null`)
- **CFG-SCHEMA-12.** Execution validates a `configure` call's keyword arguments after
  the driver load, so the schema of the SHA256 being run applies, with the same
  fallback to the registry. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`); `services/execution/app/services/driver_loader.py` (`get_driver_config_schema`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_configure_validation_runs_after_load_driver`, `test_configure_registry_fallback_when_no_published_schema`)

**Out of scope.** The assistant's schema tool (`ai-features.md`, AI-TOOL-7) and the AI
commit's own config check (AI-COMMIT-3), both built on the registry here. What the
Layer 3 fields mean for routing intent (ADR 0014, `topology.md`).

### 8.4 Applying a version now

**What it does.** A user who may change a device's configuration can push one of its
versions to the device immediately and sees whether the push worked.

**Surfaces.** The Apply dialog of the configuration section (8.12);
`POST /devices/{id}/config-versions/{vid}/apply`.

**Rules.**

- **CFG-APPLY-1.** After the checks of CFG-AUTH-3 and CFG-GATE-2, inventory calls
  execution `POST /execute` with the caller's own `Authorization` header and a 30 second
  timeout, sending `device_id`, `action` `configure`, `user_id` (the caller), and the
  version's config as `method_kwargs`; it sends no `dry_run` and no `reservation_id`. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_apply_calls_execution_with_method_kwargs`)
- **CFG-APPLY-2.** Every execution outcome answers 200
  `{version_id, run_id, status, error}`: a transport error is `failed` with
  `execution service unreachable: <exception text>`, and an execution status of 400 or
  more is `failed` with `<status> <detail>`, the detail taken from the JSON body or the
  raw text. Known gap, see #1093. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_apply_handles_execution_transport_error`, `test_apply_handles_non_json_error_body`); `services/inventory/tests/test_device_configs.py` (`test_apply_surfaces_403_verbatim`)
- **CFG-APPLY-3.** For a 2xx answer, `status` is the run's status in lower case, and
  `success` when the body has no status or is not JSON; `error` is the run's. A
  scheduled job counts the same answers as `failed` (CFG-SCHED-9). Known gap, see
  #1094. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_apply_handles_non_json_success_body`); `services/inventory/tests/test_device_configs.py` (`test_apply_success_flips_current_pointer`)
- **CFG-APPLY-4.** When the answer names a run, the version's `last_apply_run_id` is set
  (null when the id is not a UUID) and, only when the status is `success`, the device's
  current config pointer moves to the version; both are committed before the answer. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_apply_success_flips_current_pointer`, `test_apply_failure_does_not_flip_current_pointer`); `services/inventory/tests/test_device_configs_rbac.py` (`test_apply_with_malformed_run_id_returns_200_and_persists_pointer`)
- **CFG-APPLY-5.** Execution admits a non-admin's `configure` only with an explicit
  `manage` grant (CFG-EXEC-1), so the owner of an active reservation who passes
  inventory's check without a grant always gets 200 `failed` with
  `403 Admin access or device manage grant required`. Known gap, see #1092. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`, `_user_has_acl_manage`); `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_execute_non_admin_configure_without_grant_forbidden`); `services/inventory/tests/test_device_configs.py` (`test_apply_surfaces_403_verbatim`)
- **CFG-APPLY-6.** The device's current config pointer is written by an immediate apply
  only and is returned by no route. Known gap, see #1094. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`); `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: none (#1094)

**Out of scope.** What execution does with the call (8.8); the AI commit's own push to
`POST /execute` (`ai-features.md`, AI-COMMIT-15).

### 8.5 The configure capability gate

**What it does.** A configuration can be pushed only to a device whose driver kind
includes a configure step. Versions can still be stored for any device, because a switch
version records intent that other features read.

**Surfaces.** `_assert_driver_can_configure` in inventory, `_assert_action_permitted` in
execution, the fire-time check in the apply scheduler.

**Rules.**

- **CFG-GATE-1.** Only `Management` drivers can configure: `CONFIGURE_CONNECTION_TYPES`
  is exactly the set of connection types whose required driver methods include
  `configure`. \
  Enforced in: `services/common/herd_common/device_config.py` (`CONFIGURE_CONNECTION_TYPES`, `connection_type_supports_configure`); `services/execution/app/services/driver_loader.py` (`REQUIRED_METHODS`) \
  Pinned by: `services/common/tests/test_device_config.py` (`test_configure_connection_types_is_management_only`, `test_connection_type_supports_configure_true_for_management`, `test_connection_type_supports_configure_false_for_other_known_types`, `test_connection_type_supports_configure_false_for_none`, `test_connection_type_supports_configure_false_for_unknown_string`); `services/execution/tests/test_configure_capability_parity.py` (`test_configure_connection_types_matches_required_methods_contract`, `test_configure_connection_types_matches_required_methods_is_management_only`)
- **CFG-GATE-2.** Inventory's apply and schedule, after the 404s and the 403 and before
  any execution call or job row, refuse a device whose template's driver cannot
  configure with 409
  `{"error": "driver_cannot_configure", "connection_type", "driver", "message"}`; a
  device with no driver passes this gate. \
  Enforced in: `services/inventory/app/services/manage_guard.py` (`_assert_driver_can_configure`); `services/inventory/app/services/published_schema.py` (`driver_for_device`) \
  Pinned by: `services/inventory/tests/test_configure_capability_gate.py` (`test_schedule_layer3_driver_409_structured_detail_and_no_job_created`, `test_apply_layer3_driver_409_and_execution_not_called`, `test_schedule_non_manager_gets_403_not_409`, `test_apply_non_manager_gets_403_not_409`, `test_schedule_management_driver_still_succeeds`, `test_apply_management_driver_still_succeeds`, `test_schedule_no_driver_resolvable_pins_todays_behavior`, `test_apply_no_driver_resolvable_pins_todays_behavior`)
- **CFG-GATE-3.** Versions are created, read, and restored on every connection type the
  registry or a published schema covers; the gate applies to pushes only. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`create_config_version`) \
  Pinned by: `services/inventory/tests/test_configure_capability_gate.py` (`test_create_config_version_on_layer3_device_still_201`)
- **CFG-GATE-4.** Execution, before writing any run row, refuses every action on a
  device with no driver with 409 `{"error": "device_has_no_driver", "message"}`, and a
  `configure` on a driver that cannot configure with the 409 `driver_cannot_configure`
  shape of CFG-GATE-2 (`driver` being the device's driver name), on `POST /execute` and
  `POST /execute/internal` alike. \
  Enforced in: `services/execution/app/services/execution_service.py` (`_assert_action_permitted`, `run_driver_action`) \
  Pinned by: `services/execution/tests/test_configure_capability_gate.py` (`test_execute_admin_configure_by_connection_type`, `test_internal_execute_configure_by_connection_type`, `test_execute_non_admin_configure_by_connection_type`, `test_execute_admin_no_driver_refused_for_configure`, `test_execute_admin_no_driver_refused_for_non_configure_action`, `test_internal_execute_no_driver_refused`, `test_execute_non_admin_no_driver_refused`, `test_assert_action_permitted_allows_management_configure`, `test_assert_action_permitted_raises_409_for_every_non_configurable_type`, `test_assert_action_permitted_ignores_non_configure_actions`, `test_assert_action_permitted_raises_409_for_no_driver_regardless_of_action`, `test_run_driver_action_refuses_before_create_execution_run`); `tests/integration/test_execution_configure_gate.py` (`test_ai_commit_apply_configs_refuses_l3_device_via_execution_gate`)
- **CFG-GATE-5.** The scheduler checks the device's current driver again when it fires
  a job and marks the job `failed` with `DRIVER_CANNOT_CONFIGURE_ERROR` when it cannot
  configure; a device or driver it cannot resolve passes. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`, `DRIVER_CANNOT_CONFIGURE_ERROR`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_fails_when_driver_cannot_configure`, `test_fire_job_proceeds_when_driver_can_configure`, `test_fire_job_unresolvable_device_is_left_to_existing_behavior`)

**Out of scope.** Pairing a layer driver with a Management driver on one box
([DRIVERS.md](../DRIVERS.md), "Apply versus config versions").

### 8.6 Scheduled config apply

**What it does.** A user can schedule a version to be pushed at a later time, optionally
only while one of their reservations is active, and optionally as a dry run whose
captured commands they review before confirming a real push. A pending job can be
cancelled.

**Surfaces.** The Apply dialog's time field and the Scheduled applies panel (8.12); the
review dialog `frontend/src/components/reservations/AIApplyConfirmModal.tsx` (8.12); the
apply-job routes of sections 5 and 7.

**Rules.**

- **CFG-JOB-1.** `scheduled_for` must be later than now, a time without a zone being
  read as UTC; otherwise 422 `scheduled_for must be in the future`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_schedule_apply_job_rejects_past_timestamp`, `test_schedule_apply_job_rejects_exactly_now`); `services/inventory/tests/test_router_edge_cases.py` (`test_schedule_accepts_naive_future_timestamp`)
- **CFG-JOB-2.** `scheduled_for` must be at most `APPLY_JOB_MAX_HORIZON_DAYS` (30) days
  ahead; otherwise 422 `scheduled_for must be within <N> days from now`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: `services/inventory/tests/test_apply_jobs_reservation_owner.py` (`test_scheduled_for_beyond_horizon_returns_422`, `test_scheduled_for_just_within_horizon_returns_201`)
- **CFG-JOB-3.** The two time checks run before the device and version lookups, so a
  bad time on an unknown device is a 422. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: none (#1100)
- **CFG-JOB-4.** A `reservation_id`, when given, must answer reservations
  `GET /internal/{id}` with 200 and `is_active` true, and the caller must own an active
  reservation holding the device (`GET /internal/active`), both with the internal token
  and 5 seconds; a 404, an inactive reservation, or no ownership is 422
  `RESERVATION_MISMATCH_ERROR`, and anything else (no token, transport error, another
  status, non-JSON) is 503 `reservations service unreachable`. Admins are checked too. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`_validate_reservation_for_job`, `RESERVATION_MISMATCH_ERROR`) \
  Pinned by: `services/inventory/tests/test_apply_jobs_reservation_owner.py` (`test_foreign_reservation_id_returns_422_and_writes_no_row`, `test_reservation_id_inactive_returns_422_and_writes_no_row`, `test_reservation_id_active_but_not_owned_by_caller_returns_422`, `test_reservation_id_valid_and_owned_schedules_successfully`, `test_reservation_id_validation_fails_closed_when_unreachable`)
- **CFG-JOB-5.** The two lookups do not prove that the named reservation itself holds
  the device or belongs to the caller: a caller with one qualifying reservation can
  name another active one. By decision; see the `_validate_reservation_for_job`
  docstring. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`_validate_reservation_for_job`) \
  Pinned by: none
- **CFG-JOB-6.** A dry-run job needs the device's driver to declare `supports_dry_run`;
  otherwise 422
  `this driver does not advertise dry-run support; refuse to fire a dry-run that would hit the wire`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: none (#1100)
- **CFG-JOB-7.** A schedule stores the device, the version, `scheduled_for` as sent,
  the `reservation_id`, `dry_run`, status `pending`, the caller as `created_by`, and the
  token's `username` as `author_name`, and answers 201 with the job. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_schedule_apply_job`)
- **CFG-JOB-8.** A device's job list is ordered by `scheduled_for`, latest first; an
  unknown device is 404 `Device not found`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`list_apply_jobs`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_list_apply_jobs`); `services/inventory/tests/test_router_edge_cases.py` (`test_list_apply_jobs_unknown_device_404`)
- **CFG-JOB-9.** A single job read answers the job, or 404 `Apply job not found`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`get_apply_job`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_get_apply_job_returns_job`, `test_get_apply_job_not_found_404`)
- **CFG-JOB-10.** Only the job's creator or an admin may cancel (403
  `Not authorized to cancel this job`), only a `pending` job (409
  `Job is '<status>', not cancellable`); an unknown job is 404 `Apply job not found`. The
  cancel does not check device visibility. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`cancel_apply_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_cancel_pending_job`, `test_cancel_other_users_job_forbidden`, `test_cancel_already_cancelled_job`); `services/inventory/tests/test_device_configs_rbac.py` (`test_admin_can_cancel_other_users_pending_job`); `services/inventory/tests/test_router_edge_cases.py` (`test_cancel_unknown_job_404`)
- **CFG-JOB-11.** A confirm needs a source job that is a dry run (409
  `Source job is not a dry-run; nothing to promote`) and `success` (409
  `Source dry-run is '<status>'; only successful dry-runs can be promoted`); these are
  checked before the caller's authority (CFG-AUTH-3) on the job's device, and an unknown
  job is 404 `Apply job not found`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`confirm_dry_run_apply`) \
  Pinned by: `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_404_when_job_missing`, `test_confirm_409_when_source_is_not_dry_run`, `test_confirm_409_when_dry_run_pending`, `test_confirm_409_when_dry_run_failed`)
- **CFG-JOB-12.** A confirm writes a new `pending` real job (not a dry run) for the same
  device and version, due 10 seconds from now, carrying the source's `reservation_id`
  and the confirming user as `created_by`, answers 201 with it, leaves the source
  unchanged, and logs `apply_job_promoted_from_dry_run`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`confirm_dry_run_apply`) \
  Pinned by: `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_promotes_dry_run_to_real_apply`, `test_confirm_leaves_source_dry_run_intact`, `test_confirm_non_admin_owner_allowed`)
- **CFG-JOB-13.** A confirm repeats none of the schedule-time checks (horizon,
  reservation, driver gate, dry-run support) and does not check device visibility; the
  scheduler's fire-time checks still apply to the new job. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`confirm_dry_run_apply`) \
  Pinned by: none (#1100)
- **CFG-JOB-14.** The internal summary answers `count`, every job ever scheduled for
  the device, and `names`, the distinct non-empty `description` values of their
  versions, at most `APPLY_JOBS_SUMMARY_NAME_CAP` (20), never a config; an unknown
  device answers `{"count": 0, "names": []}`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`get_apply_jobs_summary_internal`, `APPLY_JOBS_SUMMARY_NAME_CAP`) \
  Pinned by: `services/inventory/tests/test_apply_jobs_internal_summary.py` (`test_summary_returns_count_and_deduplicated_names`, `test_summary_omits_null_descriptions_but_still_counts_them`, `test_summary_never_leaks_config_contents`, `test_summary_empty_for_unknown_device`, `test_summary_requires_internal_token`)

**Out of scope.** The assistant's `schedule_config_apply` tool, which always schedules a
dry run tagged with its reservation (`ai-features.md`, AI-WRITE-3), and how the
classifier uses the summary (`ai-features.md`).

### 8.7 The apply scheduler

**What it does.** A background task in inventory fires due jobs against execution,
re-checking at fire time that the job may still run.

**Surfaces.** `run_scheduler_loop` in
`services/inventory/app/services/apply_scheduler.py`, started by inventory's
`services/inventory/app/main.py`; interval `APPLY_SCHEDULER_INTERVAL_SECONDS`.

**Rules.**

- **CFG-SCHED-1.** The scheduler runs inside every inventory process whose
  `APPLY_SCHEDULER_ENABLED` is true (the default), ticking every
  `APPLY_SCHEDULER_INTERVAL_SECONDS` (30). \
  Enforced in: `services/inventory/app/main.py` (`run_scheduler_loop`); `services/inventory/app/services/apply_scheduler.py` (`run_scheduler_loop`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_run_scheduler_loop_fires_due_jobs`)
- **CFG-SCHED-2.** Each tick runs the stale sweep (CFG-STATE-5), then takes at most 10
  `pending` jobs whose time has come, oldest `scheduled_for` first, and fires them one
  after another. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`run_scheduler_loop`, `_due_jobs`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_due_jobs_returns_only_pending_and_due`, `test_run_scheduler_loop_fires_due_jobs`)
- **CFG-SCHED-3.** A tick that fails waits twice as long as the last, up to ten
  intervals or 300 seconds, whichever is larger; a good tick resets the wait. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`run_scheduler_loop`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_run_scheduler_loop_backs_off_on_db_failure_then_recovers`)
- **CFG-SCHED-4.** After its claim a job is checked in this order: its reservation is
  active (when it names one), its creator is still authorized, its version exists, its
  driver can configure (CFG-GATE-5); then it is fired. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_proceeds_when_reservation_active`, `test_fire_job_skips_when_reservation_active_but_creator_unauthorized`)
- **CFG-SCHED-5.** The reservation check reads reservations `GET /internal/{id}` with the
  internal token and 5 seconds and fires only on 200 with `is_active` true; no token, a
  transport error, another status, or non-JSON skips the job with
  `reservation not currently active`. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_reservation_active`, `fire_job`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_skipped_when_reservation_not_active`, `test_reservation_gate_hits_internal_url`, `test_reservation_gate_closed_default_when_token_missing`, `test_reservation_gate_closed_default_on_403`, `test_reservation_active_http_error_returns_false`, `test_reservation_active_malformed_json_returns_false`)
- **CFG-SCHED-6.** The creator check (CFG-AUTH-7) runs for every job, with or without a
  reservation, and a no skips the job with `CREATOR_UNAUTHORIZED_ERROR`. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_creator_still_authorized`, `fire_job`, `CREATOR_UNAUTHORIZED_ERROR`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_skips_when_creator_no_longer_authorized`, `test_fire_job_skips_when_authority_check_unreachable`, `test_fire_job_positive_control_authorized_creator_fires`)
- **CFG-SCHED-7.** A job whose version no longer exists fails with
  `config version no longer exists`. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_fails_when_version_was_deleted`)
- **CFG-SCHED-8.** A job is fired with execution `POST /execute/internal`, the internal
  token, and 30 seconds, sending `device_id`, `action` `configure`, `user_id` (the job's
  creator), the version's config as `method_kwargs`, and `dry_run`; it sends no
  `reservation_id`, so the run carries none, and a non-admin reservation owner cannot
  read its transcript (CFG-TX-5). Known gap, see #1090. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_post_internal_execute`) \
  Pinned by: none (#1090)
- **CFG-SCHED-9.** The job is `success` only for a 2xx JSON answer whose `status` is
  `SUCCESS` in any case. Otherwise it is `failed` with `execution unreachable: <text>` (a
  transport error), `<status> <detail>` (400 or more),
  `execution returned malformed JSON`, or the run's `error`, else
  `execution returned non-success status` (a missing or null status included). A run id
  that is not a UUID is stored as null. Known gap, see #1093. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_post_internal_execute`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_post_internal_execute_http_error`, `test_post_internal_execute_error_body_not_json`, `test_post_internal_execute_success_body_not_json`, `test_post_internal_execute_malformed_run_id_degrades_to_none`, `test_post_internal_execute_non_success_status`, `test_post_internal_execute_missing_status_records_failed`, `test_post_internal_execute_null_status_records_failed`)
- **CFG-SCHED-10.** A successful job sets its version's `last_apply_run_id` but does not
  move the device's current config pointer. Known gap, see #1094. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: none (#1094)

**Out of scope.** Catching up missed fire times: a job fires once, at the first tick at
or after its time.

### 8.8 Running a driver action

**What it does.** An admin can run any method of a device's driver; a user with a
`manage` grant can push a configuration; inventory's scheduler pushes scheduled ones.
Every call is recorded as an execution run.

**Surfaces.** `POST /execute`, `POST /execute/internal`, and `run_driver_action` in
`services/execution/app/services/execution_service.py`. How the driver context is built
is WIRE-DRIVER-6 in `provisioning-and-wiring.md`.

**Rules.**

- **CFG-EXEC-1.** `POST /execute` lets an admin run any action. Any other caller may run
  only `configure` (403 `Admin access required` otherwise) and only with an explicit
  `manage` grant on the device, asked of acl `POST /check` with the caller's own token
  and 5 seconds; no token, a transport error, a non-200, non-JSON, or no grant is 403
  `Admin access or device manage grant required`. Owning a reservation does not count. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`, `_user_has_acl_manage`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_execute_non_admin_status_action_forbidden`, `test_execute_non_admin_configure_without_grant_forbidden`, `test_execute_non_admin_configure_with_grant_succeeds`); `services/execution/tests/test_router_direct.py` (`test_acl_manage_false_without_authorization`, `test_acl_manage_false_on_httpx_error`, `test_acl_manage_false_on_non_200`, `test_acl_manage_false_on_malformed_json`, `test_acl_manage_true_when_allowed`, `test_acl_manage_false_when_not_allowed`)
- **CFG-EXEC-2.** On `POST /execute` the run is attributed to the token's `sub`; the
  body's required `user_id` is ignored. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_manual_execute_overrides_user_id_with_jwt_subject`)
- **CFG-EXEC-3.** The body's `reservation_id`, `port_a`, `port_b`, `method_kwargs`, and
  `dry_run` are used as sent; the `reservation_id` is not checked against the device or
  the caller. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`) \
  Pinned by: none (#1100)
- **CFG-EXEC-4.** The device and its template are read through inventory's internal
  routes with the internal token and 10 seconds; a 404 is 404 `Device <id> not found` or
  `Template <id> not found`, and any other failure is 503
  `Failed to fetch device: <exception text>` (or template). Known gap, see #1093. \
  Enforced in: `services/execution/app/services/execution_service.py` (`fetch_device`, `fetch_template`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_fetch_device_returns_payload`, `test_fetch_device_404_raises_404`, `test_fetch_device_other_error_raises_503`, `test_fetch_template_404_raises_404`, `test_fetch_template_other_error_raises_503`)
- **CFG-EXEC-5.** Once the gate (CFG-GATE-4) passes, a call answers 201 with the run
  whatever its outcome; only a refused `configure` input (CFG-EXEC-10) changes the HTTP
  status. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`, `internal_execute`) \
  Pinned by: `services/execution/tests/test_api_endpoints.py` (`test_execute_success`, `test_execute_driver_failure`, `test_execute_driver_load_failure`, `test_execute_timeout`)
- **CFG-EXEC-6.** `POST /execute/internal` needs the internal token (500
  `Internal API token not configured` when execution has none, 403
  `Invalid internal token` when the header is missing or wrong), runs only `configure`
  (422 `internal execute is restricted to action='configure'; got '<action>'`), and
  attributes the run to the body's `user_id`. \
  Enforced in: `services/execution/app/routers/executions.py` (`internal_execute`, `_require_internal_token`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_internal_execute_uses_internal_token`, `test_internal_execute_rejects_non_configure_action`, `test_require_internal_token_rejects_missing`, `test_require_internal_token_errors_when_not_configured`)
- **CFG-EXEC-7.** The run's `input_params` is the driver context with every key of a
  `password`-typed template field replaced by `***REDACTED***`. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `redact_context_for_logging`, `extract_password_keys`) \
  Pinned by: `services/execution/tests/test_api_endpoints.py` (`test_execute_success`); `services/execution/tests/test_execution_service.py` (`test_redact_context`, `test_extract_password_keys`)
- **CFG-EXEC-8.** A non-empty `method_kwargs` is stored in `input_params` under
  `method_kwargs` as sent, a pushed configuration included. \
  Enforced in: `services/execution/app/services/execution_service.py` (`create_execution_run`) \
  Pinned by: none (#1100)
- **CFG-EXEC-9.** A driver load failure ends the run `FAILED` with
  `driver load failed: <ClassName>` (the wrapped cause's class when there is one); the
  exception text goes to the log message only. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`); `services/execution/app/services/driver_loader.py` (`driver_load_failure_text`) \
  Pinned by: `services/execution/tests/test_driver_load_sanitize_run.py` (`test_run_driver_action_download_failure_stores_class_name_only`, `test_run_driver_action_extraction_failure_stores_class_name_only`, `test_run_driver_action_validate_import_failure_stores_class_name_only`); `services/execution/tests/test_api_endpoints.py` (`test_execute_driver_load_failure_never_returns_foreign_text`)
- **CFG-EXEC-10.** A `configure` call's `method_kwargs` is validated as 8.3 says; a
  refusal ends the run `FAILED` with the validator's message and answers 422 with the
  same message. Other actions are not validated. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_manual_execute_validates_configure_kwargs`, `test_internal_execute_validates_configure_kwargs`, `test_manual_execute_skips_validation_for_non_configure_actions`)
- **CFG-EXEC-11.** A driver that raised is recorded as `driver raised <ClassName>`, its
  message logged only; a failure the driver returned keeps the driver's own `error`, or
  `driver reported failure` when it gave none. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `driver_result_failed`); `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_raised_exception_stores_class_name_only`, `test_run_driver_action_raised_exception_logs_full_text_with_run_id`, `test_run_driver_action_returned_failure_keeps_driver_message`, `test_run_driver_action_driver_result_failure_without_error_message`)
- **CFG-EXEC-12.** A failure to store the command transcript is logged and does not
  change the run's outcome. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_command_log_failure_swallowed`)
- **CFG-EXEC-13.** The start-of-run log line carries the context's key names and the
  keyword arguments' key names, never their values. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_start_log_omits_method_kwargs_values`, `test_run_driver_action_start_log_emits_context_keys_not_values`)

**Out of scope.** The wiring consumer's driver calls (`provisioning-and-wiring.md`,
WIRE-DRIVER-1 to WIRE-DRIVER-7) and the recipe calls (`dynamic-resources.md`); device
health checks (`operations-and-observability.md`).

### 8.9 Execution run history

**What it does.** Admins can list, read, and retry every driver run. The owner of a
reservation can list the runs tagged with it.

**Surfaces.** `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/retry`; the assistant's
`list_executions_for_reservation` tool reads the list with the user's token
(`ai-features.md`).

**Rules.**

- **CFG-RUN-1.** An admin may list with any filters. Anyone else must pass a
  `reservation_id` (403 `Admin access required` otherwise) that reservations
  `GET /{id}` answers with 200 for the caller's own token in 5 seconds; any other answer,
  a transport error, or no token is 403 `Reservation not owned by caller`. \
  Enforced in: `services/execution/app/routers/executions.py` (`_authorize_runs_list`, `_user_owns_reservation`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_list_runs_admin_unchanged`, `test_list_runs_non_admin_without_reservation_id_403`, `test_list_runs_non_owner_with_reservation_id_403`, `test_list_runs_owner_with_reservation_id_allowed`, `test_user_owns_reservation_returns_false_without_authorization`, `test_user_owns_reservation_returns_false_on_httpx_error`); `services/execution/tests/test_router_direct.py` (`test_user_owns_reservation_true_on_200`)
- **CFG-RUN-2.** The list filters by exact `device_id`, `reservation_id`, and `status`,
  by `created_after` (inclusive) and `created_before` (exclusive), orders newest first,
  and pages with `skip` and `limit`. \
  Enforced in: `services/execution/app/services/execution_service.py` (`list_execution_runs`) \
  Pinned by: `services/execution/tests/test_execution_crud.py` (`test_list_filter_by_device_id`, `test_list_filter_by_reservation_id`, `test_list_filter_by_status`, `test_list_pagination`, `test_list_combined_filters`); `services/execution/tests/test_execution_service_edges.py` (`test_list_execution_runs_created_after_and_before`)
- **CFG-RUN-3.** A reservation owner's list holds every run tagged with that
  reservation, whatever device it ran on, the wiring consumer's switch runs
  (WIRE-DRIVER-5) included, each with its `input_params`. \
  Enforced in: `services/execution/app/routers/executions.py` (`list_runs`); `services/execution/app/services/execution_service.py` (`list_execution_runs`) \
  Pinned by: none (#1100)
- **CFG-RUN-4.** `GET /runs/{id}` is for admins (403
  `Admin or superadmin role required`); an unknown run is 404 `Execution run not found`. \
  Enforced in: `services/execution/app/routers/executions.py` (`get_run`) \
  Pinned by: `services/execution/tests/test_router_direct.py` (`test_get_run_404_when_missing`, `test_get_run_returns_persisted`); `services/execution/tests/test_api_endpoints.py` (`test_get_run_detail`)
- **CFG-RUN-5.** An admin's retry of a `FAILED` or `TIMEOUT` run runs the same action
  again as a new run, with the original run's user, reservation, ports, and
  `method_kwargs`, and answers 200 with the new run; any other status is 400
  `Only failed or timed-out runs can be retried`, an unknown run 404. \
  Enforced in: `services/execution/app/routers/executions.py` (`retry_run`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_retry_rejects_successful_run`, `test_retry_failed_run_rebuilds_and_runs`); `services/execution/tests/test_api_endpoints.py` (`test_retry_failed_run`, `test_retry_success_run_rejected`); `services/execution/tests/test_router_direct.py` (`test_retry_run_404_when_missing`)
- **CFG-RUN-6.** A run does not record whether it was a dry run and a retry passes no
  `dry_run`, so retrying a failed or timed-out dry run pushes the configuration for
  real. Known gap, see #1091. \
  Enforced in: `services/execution/app/routers/executions.py` (`retry_run`) \
  Pinned by: none (#1091)

**Out of scope.** The wiring status and wiring retry routes, which are not run history
(`provisioning-and-wiring.md`).

### 8.10 Per-command execution transcripts

**What it does.** A driver can record each command it sends and the device's answer,
real or simulated; the rows are stored with the run and shown in the dry-run review.

**Surfaces.** `record_command` in `services/execution/app/services/driver_transcript.py`,
which drivers import; `GET /runs/{id}/commands`.

**Rules.**

- **CFG-TX-1.** `record_command` appends one JSON line
  `{command, response, duration_ms, exit_status}` (`exit_status` `ok` by default) to the
  file named by `HERD_TRANSCRIPT_PATH`; with the variable unset it does nothing, and a
  write error is swallowed. \
  Enforced in: `services/execution/app/services/driver_transcript.py` (`record_command`) \
  Pinned by: `services/execution/tests/test_driver_transcript.py` (`test_record_command_noop_when_env_unset`, `test_record_command_writes_one_row`, `test_record_command_appends_multiple_rows`, `test_record_command_defaults`, `test_record_command_swallows_write_errors`)
- **CFG-TX-2.** The sandbox gives each call a fresh transcript file, reads it after the
  child ends (on a timeout too), skips lines that are not JSON, and deletes the file. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`, `_read_transcript`) \
  Pinned by: `services/execution/tests/test_command_log.py` (`test_sandbox_captures_transcript`, `test_sandbox_empty_transcript_when_driver_silent`, `test_sandbox_cleans_up_transcript_file`); `services/execution/tests/test_driver_sandbox_edges.py` (`test_read_transcript_parses_and_skips_malformed`, `test_read_transcript_missing_path_returns_empty`, `test_read_transcript_oserror_returns_empty`)
- **CFG-TX-3.** Stored rows skip any entry without a `command`, are numbered from 1 in
  the order recorded, and keep `exit_status` (`ok` when empty) cut to 20 characters. \
  Enforced in: `services/execution/app/services/execution_service.py` (`insert_command_log`) \
  Pinned by: `services/execution/tests/test_command_log.py` (`test_insert_command_log_inserts_rows_in_order`, `test_insert_command_log_skips_rows_missing_command`, `test_insert_command_log_empty_rows_is_noop`); `services/execution/tests/test_execution_service_edges.py` (`test_insert_command_log_all_rows_without_command_returns_zero`)
- **CFG-TX-4.** A run's rows are deleted with the run. \
  Enforced in: `services/execution/app/models/execution_command.py` (`ExecutionCommand`) \
  Pinned by: `services/execution/tests/test_command_log.py` (`test_command_rows_cascade_on_run_delete`)
- **CFG-TX-5.** `GET /runs/{id}/commands` answers the rows in order, an empty list when
  the driver recorded none, and 404 `Execution run not found` for an unknown run. An
  admin may read any run; anyone else only a run whose `reservation_id` passes the
  ownership check of CFG-RUN-1 (403 `Reservation not owned by caller`), and a run with
  no reservation is 403 `Admin access required`. \
  Enforced in: `services/execution/app/routers/executions.py` (`list_run_commands`, `_authorize_run_read`); `services/execution/app/services/execution_service.py` (`list_command_log`) \
  Pinned by: `services/execution/tests/test_command_log_acl.py` (`test_admin_can_read_any_run`, `test_non_admin_owner_can_read`, `test_non_admin_non_owner_rejected`, `test_non_admin_run_without_reservation_rejected`, `test_reservations_service_error_is_closed_by_default`); `services/execution/tests/test_router_direct.py` (`test_list_run_commands_404_when_run_missing`, `test_list_run_commands_admin_returns_empty`); `services/execution/tests/test_command_log.py` (`test_get_run_commands_admin`, `test_get_run_commands_not_found`)

**Out of scope.** The golden-transcript regression tests of the checked-in drivers
(`services/execution/tests/test_golden_transcripts.py`), which pin driver output rather
than this area's behavior.

### 8.11 Dry run

**What it does.** A configuration push can run as a dry run: the driver is told not to
touch the device and records the commands it would have sent. Only drivers that declare
they support this may be asked.

**Surfaces.** `driver_metadata.json` in a driver package; the dry-run check in
`execute_driver_method`; the schedule-time check of CFG-JOB-6.

**Rules.**

- **CFG-DRY-1.** The sandbox refuses a dry run, before starting any process, unless
  the driver's metadata has `supports_dry_run` true. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`, `DryRunRefused`) \
  Pinned by: `services/execution/tests/test_dry_run.py` (`test_sandbox_refuses_dry_run_when_driver_lacks_metadata`, `test_sandbox_refuses_dry_run_when_metadata_explicitly_false`, `test_sandbox_allows_dry_run_when_metadata_true`); `services/execution/tests/test_driver_sandbox_edges.py` (`test_dry_run_refused_without_support`)
- **CFG-DRY-2.** An admitted dry run gives the driver a copy of the context with the
  unprefixed key `dry_run` set to true and leaves the caller's context unchanged; a real
  run is not checked against the metadata. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: `services/execution/tests/test_dry_run.py` (`test_sandbox_does_not_mutate_caller_context_on_dry_run`, `test_sandbox_real_run_skips_metadata_check`); `services/execution/tests/test_driver_sandbox_edges.py` (`test_dry_run_injects_context_flag`)
- **CFG-DRY-3.** The metadata is read from `driver_metadata.json` at load and cached
  with the driver; a missing, unreadable, or non-object file, or no cache row, reads as
  `supports_dry_run` false and `supports_vrf` false. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`read_driver_metadata`, `get_driver_metadata`, `DEFAULT_DRIVER_METADATA`) \
  Pinned by: `services/execution/tests/test_dry_run.py` (`test_read_metadata_returns_default_when_missing`, `test_read_metadata_returns_parsed_dict`, `test_read_metadata_handles_malformed_json`, `test_read_metadata_handles_non_object`, `test_get_metadata_returns_default_when_no_cache_row`, `test_get_metadata_returns_default_when_column_empty`, `test_get_metadata_returns_parsed_when_set`, `test_load_driver_populates_cached_metadata`)
- **CFG-DRY-4.** HERD does not check that a driver which declares dry-run support keeps
  off the wire; the declaration is binding on the driver's author. By decision; see
  [DRIVERS.md](../DRIVERS.md), "Dry-run support". \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: none

**Out of scope.** How a driver honors the flag ([DRIVERS.md](../DRIVERS.md)).

### 8.12 Device configuration in the browser

**What it does.** The device page shows the configuration history with view, compare,
restore, and apply actions and a list of scheduled pushes. A user who asked the
reservation assistant for a change reviews the dry run's captured commands and confirms
or cancels it.

**Surfaces.** `frontend/src/components/device-config/DeviceConfigSection.tsx` and
`frontend/src/components/device-config/ApplyJobsPanel.tsx`, mounted by
`frontend/src/pages/DevicePage.tsx`; `frontend/src/components/reservations/AIApplyConfirmModal.tsx`,
opened from the reservation assistant; clients `frontend/src/api/deviceConfig.ts` and
`frontend/src/api/deviceConfigJobs.ts`.

**Rules.**

- **CFG-UI-1.** The configuration section shows the version count, a table of versions
  with author and time, an empty state, and an error line when the list fails; its
  buttons are shown to every viewer, and the server decides who may use them. \
  Enforced in: `frontend/src/components/device-config/DeviceConfigSection.tsx` (`DeviceConfigSection`, `VersionRow`); `frontend/src/pages/DevicePage.tsx` (`DeviceConfigSection`) \
  Pinned by: `frontend/src/test/components/DeviceConfigSection.test.tsx` (`shows the empty state when the device has no config versions`, `renders an error message when the versions request fails`, `renders a populated version table with author and singular count`)
- **CFG-UI-2.** New version parses the text as JSON in the browser, an empty box being
  `{}`; a parse error is shown in the dialog and nothing is sent, and a server refusal
  shows the server's `detail`. \
  Enforced in: `frontend/src/components/device-config/DeviceConfigSection.tsx` (`DeviceConfigSection`) \
  Pinned by: `frontend/src/test/components/DeviceConfigSection.test.tsx` (`surfaces a JSON parse error in the create modal without calling the API`, `creates a new version with parsed JSON and shows a success toast`); `tests/e2e/test_flows_effects_playwright.py` (`test_device_config_version_cycle`)
- **CFG-UI-3.** Compare is enabled only with exactly two versions ticked; ticking a third
  drops the earlier of the two. \
  Enforced in: `frontend/src/components/device-config/DeviceConfigSection.tsx` (`DeviceConfigSection`) \
  Pinned by: `frontend/src/test/components/DeviceConfigSection.test.tsx` (`keeps Compare disabled until exactly two versions are selected`)
- **CFG-UI-4.** The Apply dialog applies now when its time is blank, showing
  `Apply failed: <error>` for a `failed` answer and `Applied (run <first 8 characters>)`
  otherwise, and schedules at the chosen local time when one is set. A refused request
  shows the generic `Apply request failed` or `Restore failed`, or the server's `detail`
  as given on a schedule, so a structured 409 detail is not shown as text. Known gap,
  see #1098. \
  Enforced in: `frontend/src/components/device-config/DeviceConfigSection.tsx` (`DeviceConfigSection`) \
  Pinned by: none (#1098)
- **CFG-UI-5.** The scheduled-applies panel is hidden when the device has no jobs,
  refreshes every 10 seconds, shows each job's status and error, and offers Cancel only
  on a `pending` job. \
  Enforced in: `frontend/src/components/device-config/ApplyJobsPanel.tsx` (`ApplyJobsPanel`); `frontend/src/api/deviceConfigJobs.ts` (`useApplyJobs`) \
  Pinned by: none (#1100)
- **CFG-UI-6.** The dry-run review polls the job every 2 seconds until it is `success`,
  `failed`, `skipped`, or `cancelled`, then shows the run's transcript with simulated
  rows marked; Confirm is enabled only on `success`, Cancel dry-run only before it, and a
  refused confirm shows the server's `detail`. \
  Enforced in: `frontend/src/components/reservations/AIApplyConfirmModal.tsx` (`AIApplyConfirmModal`); `frontend/src/api/deviceConfigJobs.ts` (`useApplyJob`, `TERMINAL_STATUSES`) \
  Pinned by: `frontend/src/test/components/AIApplyConfirmModal.test.tsx` (`renders the plan text`, `shows the transcript with simulated badges once the dry-run succeeds`, `disables Confirm until the dry-run succeeds`, `Confirm POSTs to the confirm endpoint and closes`, `Cancel button cancels the dry-run job and closes`, `Confirm endpoint 409 (failed dry-run) surfaces detail as toast`)

**Out of scope.** When the assistant opens the review (`ai-features.md`); the rest of
the device page (`inventory.md`).

### 8.13 Driver loading and the driver cache

**What it does.** Before running a driver, execution makes sure it holds an extracted,
checked copy of exactly the package version the device's template names, downloading it
from inventory once per version.

**Surfaces.** `load_driver` in `services/execution/app/services/driver_loader.py`,
called by every driver action, the wiring consumer, the dynamic-instance consumer, and
the internal config-schema route. How a load failure is classified for the wiring rows
is WIRE-DRIVER-7 in `provisioning-and-wiring.md`.

**Rules.**

- **CFG-LOAD-1.** The cache is one row per driver; it is used only when its SHA256
  equals the one the device names and its directory still exists. A different SHA256
  deletes the directory and the row; a missing directory deletes the row. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`get_cached_driver`) \
  Pinned by: `services/execution/tests/test_driver_loader_advanced.py` (`test_cache_miss`, `test_cache_hit`, `test_cache_sha256_mismatch`, `test_cache_path_missing`); `services/execution/tests/test_driver_loader_load.py` (`test_load_driver_cache_hit`)
- **CFG-LOAD-2.** A miss downloads the archive from inventory's internal download route
  with the internal token and 30 seconds; a failure raises a `RuntimeError` naming only
  the cause's class, which callers treat as transient. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`download_driver_package`, `load_driver`) \
  Pinned by: `services/execution/tests/test_driver_loader_load.py` (`test_download_driver_package_success`, `test_download_driver_package_failure`, `test_load_driver_download_failure`, `test_load_driver_download_failure_sanitizes_foreign_text`)
- **CFG-LOAD-3.** The archive is extracted under `DRIVER_CACHE_PATH/<driver id>`: a
  `.zip`, or a `.tar.gz` or `.tgz` through the tar `data` filter; entries cannot land
  outside the directory. Any other name, or a corrupt archive, raises
  `DriverPackageError` naming only the cause's class, and the directory is removed. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`extract_driver_package`, `load_driver`) \
  Pinned by: `services/execution/tests/test_driver_loader.py` (`test_extract_zip`, `test_extract_unsupported_format`); `services/execution/tests/test_driver_loader_advanced.py` (`test_extract_tar_gz`, `test_extract_tgz`); `services/execution/tests/test_driver_loader_security.py` (`test_zip_traversal_does_not_escape_destination`, `test_zip_absolute_path_entry_is_contained`, `test_zip_corrupt_archive_raises`, `test_tar_gz_corrupt_archive_raises`, `test_tar_gz_symlink_escape_is_blocked_by_data_filter`, `test_tar_gz_traversal_blocked_by_data_filter`); `services/execution/tests/test_driver_loader_load.py` (`test_load_driver_extraction_failure`, `test_load_driver_extraction_failure_sanitizes_foreign_text`)
- **CFG-LOAD-4.** The extracted package must hold `driver.py` defining a class `Driver`
  with every method `REQUIRED_METHODS` lists for the template's connection type (an
  unknown connection type fails); `load_driver` checks this by importing `driver.py`. A
  failure raises `DriverPackageError` `Driver validation failed: <reasons>`, an import
  error named by class only, and the directory is removed. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`validate_driver`, `REQUIRED_METHODS`, `load_driver`) \
  Pinned by: `services/execution/tests/test_driver_loader.py` (`test_validate_valid_l1_driver`, `test_validate_missing_driver_py`, `test_validate_missing_driver_class`, `test_validate_missing_methods`, `test_validate_unknown_connection_type`, `test_validate_syntax_error_driver`); `services/execution/tests/test_driver_loader_advanced.py` (`test_validate_management_driver`, `test_validate_l1_driver_against_l2_type`, `test_required_methods_dict_completeness`); `services/execution/tests/test_driver_loader_load.py` (`test_load_driver_validation_failure`, `test_load_driver_validate_import_failure_sanitizes_foreign_text`)
- **CFG-LOAD-5.** A good package's SHA256, directory, metadata, and published schema
  are written to the driver's cache row, updating it in place when a row exists. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`load_driver`) \
  Pinned by: `services/execution/tests/test_driver_loader_load.py` (`test_load_driver_download_and_cache`, `test_load_driver_updates_existing_cache`, `test_load_driver_updates_existing_row_under_concurrent_insert`)
- **CFG-LOAD-6.** When two first loads of one driver run at once, both find no row,
  extract into the same directory, and insert; the second insert breaks the unique
  driver id and its caller fails with an unhandled error (CFG-RUNSTATE-5). Known gap,
  see #1097. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`load_driver`); `services/execution/app/models/driver_cache.py` (`DriverCache`) \
  Pinned by: none (#1097)

**Out of scope.** Driver upload, replacement, and storage (`inventory.md`, INV-DRV-1 to
INV-DRV-18); the method contract of each connection type ([DRIVERS.md](../DRIVERS.md)).

### 8.14 The driver sandbox

**What it does.** Every driver method runs in its own short-lived process with resource
limits and a timeout, so a slow or broken driver cannot take execution down with it.

**Surfaces.** `execute_driver_method` in `services/execution/app/services/driver_sandbox.py`;
the child entry point `services/execution/app/services/_runner.py`;
`services/execution/app/services/_rlimits.py`.

**Rules.**

- **CFG-SBX-1.** Each call starts `python _runner.py <driver dir> <action> <context file>`
  plus, when there are keyword arguments, their JSON as one more argument; the context
  is written to a temporary JSON file that is deleted when the call ends. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`); `services/execution/app/services/_runner.py` (`main`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_execute_login`, `test_execute_context_passed`, `test_execute_connect_ports_via_method_kwargs`); `services/execution/tests/test_runner.py` (`test_runner_login_action`, `test_runner_connect_ports_with_args`, `test_runner_invalid_context_file`)
- **CFG-SBX-2.** The keyword arguments are `method_kwargs` plus `port_a` and `port_b`
  when given and not already present. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_execute_connect_ports_via_method_kwargs`, `test_execute_connect_ports`)
- **CFG-SBX-3.** The child's environment holds `PYTHONPATH` (the driver directory, plus
  `_deps` when present), `PYTHONDONTWRITEBYTECODE`, `PATH`, `HERD_TRANSCRIPT_PATH`, the
  rlimit policy, and every context key upper-cased as a string (booleans in lower case,
  null as empty, lists and objects as JSON) except the keys of `password`-typed fields,
  which reach the driver through the context file only. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`, `context_to_env_vars`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_context_to_env_vars_string`, `test_context_to_env_vars_bool`, `test_context_to_env_vars_none`, `test_context_to_env_vars_dict`, `test_context_to_env_vars_keys_uppercased`, `test_context_to_env_vars_exclude_strips_secrets`, `test_env_vars_visible_in_subprocess`, `test_password_keys_stripped_from_env_but_kept_in_context`); `services/execution/tests/test_sandbox_isolation.py` (`test_no_secret_key_leaks_into_env`)
- **CFG-SBX-4.** The child applies `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_NOFILE`, and
  `RLIMIT_NPROC` from the `DRIVER_RLIMIT_*` settings as both soft and hard limits before
  it imports the driver; 0 leaves a limit off, and a limit that cannot be set is
  skipped. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`_rlimit_pairs`); `services/execution/app/services/_rlimits.py` (`apply_rlimits`); `services/execution/app/services/_runner.py` (`main`) \
  Pinned by: `services/execution/tests/test_driver_sandbox_edges.py` (`test_rlimit_pairs_reads_settings_live`, `test_apply_rlimits_applies_each_positive_limit`, `test_apply_rlimits_skips_unknown_limit_name`, `test_apply_rlimits_swallows_setrlimit_error`, `test_execute_passes_rlimit_policy_to_child_env`); `services/execution/tests/test_sandbox_isolation.py` (`test_memory_limit_kills_driver`, `test_cpu_limit_kills_driver`)
- **CFG-SBX-5.** Unless the caller passes a timeout, `status` gets
  `STATUS_CHECK_TIMEOUT_SECONDS` (10) and every other action `EXECUTION_TIMEOUT_SECONDS`
  (30); a child still running then is killed and the call fails with
  `Execution timed out after <N>s`. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_execute_timeout`, `test_execute_default_timeout_status`, `test_execute_default_timeout_login`)
- **CFG-SBX-6.** The child instantiates `Driver(context)`, calls the action with the
  keyword arguments, and prints the result as JSON; any exception prints one JSON line
  `{exception_class, message}` to stderr and exits 1. \
  Enforced in: `services/execution/app/services/_runner.py` (`main`) \
  Pinned by: `services/execution/tests/test_runner.py` (`test_runner_main_login`, `test_runner_driver_init_exception`, `test_runner_main_guard_prints_error_and_exits_one`)
- **CFG-SBX-7.** Exit 0 succeeds with stdout parsed as JSON, or `{"raw_output": <stdout>}`
  when it is not, and stderr kept apart. A negative exit fails with
  `driver killed by signal <N> (likely a resource limit)`; a positive exit whose last
  non-empty stderr line is the runner's JSON fails with `driver raised <ClassName>`;
  any other exit fails with `driver process exited with status <N>`, the raw output
  kept only for the log. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`, `_parse_driver_exception`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_execute_non_json_stdout`, `test_execute_failing_driver`); `services/execution/tests/test_driver_sandbox_edges.py` (`test_success_with_non_json_stdout_wraps_raw_output`, `test_success_with_stderr_carries_it_separately_and_leaves_error_none`, `test_negative_returncode_reports_signal`, `test_raise_behind_stderr_noise_is_still_sanitized`, `test_parser_reads_only_the_last_line`, `test_unstructured_failure_never_stores_raw_stderr_as_error`)
- **CFG-SBX-8.** A package's `requirements.txt` is installed into `_deps` (60 seconds,
  no resource limits, errors only logged) only when `ALLOW_DRIVER_PIP_INSTALL` is true
  and `_deps` does not exist yet. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_requirements_txt_skipped_when_pip_install_disabled`, `test_requirements_txt_installed_when_pip_install_enabled`, `test_execute_with_existing_deps_dir`); `services/execution/tests/test_driver_sandbox_edges.py` (`test_pip_install_exception_swallowed`)
- **CFG-SBX-9.** The child runs as execution's own user with no namespace, filesystem,
  or network separation; driver packages are trusted code. By decision; see
  [DRIVERS.md](../DRIVERS.md), "Execution sandbox". \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`) \
  Pinned by: none

**Out of scope.** The wiring consumer's in-line retries around the sandbox
(`provisioning-and-wiring.md`, WIRE-DRIVER-2) and the recipe timeout
(`dynamic-resources.md`).

### 8.15 Validating a recipe package

**What it does.** Before an admin sees an AI-drafted Hypervisor recipe, execution checks
the package without installing it: its shape and imports by reading the code, then a
full simulated lifecycle in the sandbox, and answers a report.

**Surfaces.** `POST /internal/validate-package`;
`services/execution/app/services/package_validator.py`. The caller is ai-orchestrator's
drafting loop (`ai-features.md`, AI-RECIPE-8).

**Rules.**

- **CFG-VAL-1.** The route needs the internal token (as CFG-EXEC-6) and accepts only the
  `Hypervisor` connection type (422
  `Only the Hypervisor connection type is supported for package validation`). \
  Enforced in: `services/execution/app/routers/validation.py` (`validate_package_endpoint`); `services/execution/app/services/package_validator.py` (`SUPPORTED_CONNECTION_TYPES`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_route_requires_internal_token`, `test_route_500_when_token_unconfigured`, `test_route_rejects_unsupported_connection_type`); `tests/integration/test_package_validation.py` (`test_validate_package_requires_internal_token`)
- **CFG-VAL-2.** `package_b64` must be strict base64 (422
  `package_b64 is not valid base64`), decode to at most `VALIDATE_PACKAGE_MAX_BYTES`
  (422 `package exceeds the <N> byte validation limit`), and not be empty (422
  `package is empty`). \
  Enforced in: `services/execution/app/services/package_validator.py` (`_decode_package`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_invalid_base64_raises_decode_error`, `test_size_cap_raises_decode_error`, `test_route_rejects_invalid_base64`)
- **CFG-VAL-3.** The package is extracted into a temporary directory that is always
  removed and never cached; an extraction failure answers a failing report with
  `Failed to extract package: <text>`. \
  Enforced in: `services/execution/app/services/package_validator.py` (`validate_package`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_corrupt_archive_reports_extraction_failure`, `test_no_temp_dirs_left_behind`)
- **CFG-VAL-4.** The structural section is checked by parsing, never importing:
  `driver.py` exists, parses, and defines a top-level class `Driver` that defines every
  required Hypervisor method. \
  Enforced in: `services/execution/app/services/package_validator.py` (`_structural_errors`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_missing_driver_py_fails_structural`, `test_missing_required_method_fails_structural`, `test_unparseable_driver_py_fails_structural`, `test_missing_driver_class_fails_structural`)
- **CFG-VAL-5.** The policy section requires `driver_metadata.json` to exist, parse to
  an object, and declare `supports_dry_run` true; forbids `_deps/` and
  `requirements.txt`; allows only absolute imports of the standard library, of modules
  shipped in the package, and of `driver_transcript`; and refuses a credential-like name
  (`password`, `passwd`, `secret`, `token`, `api_key`, `apikey`) assigned a non-empty
  string literal. \
  Enforced in: `services/execution/app/services/package_validator.py` (`_policy_errors`, `_credential_literal_errors`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_missing_metadata_fails_policy`, `test_supports_dry_run_false_fails_policy`, `test_non_stdlib_import_fails_policy`, `test_package_local_import_is_allowed`, `test_deps_dir_and_requirements_fail_policy`, `test_inline_credential_literal_fails_policy`, `test_non_literal_or_empty_credentials_are_allowed`)
- **CFG-VAL-6.** Nothing runs unless both sections pass. Then the published schema is
  read (optional, never decisive) and the dry-run lifecycle runs in the sandbox with
  `dry_run` true and `VALIDATE_DRY_RUN_TIMEOUT_SECONDS` per step: `login`,
  `create_instance`, `status`, `destroy_instance` with the created `instance_ref`,
  `destroy_instance` with none (reported as `KEYED_DESTROY_STEP`), `logout`, against a
  made-up context whose endpoint is under `.invalid` and whose password is a password
  key. \
  Enforced in: `services/execution/app/services/package_validator.py` (`validate_package`, `_run_dry_run_lifecycle`, `_DRY_RUN_STEPS`, `_synthetic_context`, `_extract_schema_section`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_structural_failure_never_executes_the_package`, `test_good_package_is_valid_across_all_sections`, `test_dry_run_threads_instance_ref_from_create_to_destroy`, `test_schema_is_optional_and_does_not_gate_validity`, `test_keyed_destroy_step_runs_with_request_id_in_context`, `test_import_time_failure_surfaces_in_sandbox_not_in_process`, `test_repo_mock_hypervisor_package_validates`)
- **CFG-VAL-7.** The package is `valid` exactly when every lifecycle step passed; the
  instance steps and the session steps are judged by the consumer's own rules
  (`dynamic-resources.md`, DYN-RESULT-3), and `status` passes when the call completed
  and did not return `success` false. \
  Enforced in: `services/execution/app/services/package_validator.py` (`_step_verdict`, `_run_dry_run_lifecycle`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_method_exception_fails_dry_run`, `test_driver_level_failure_verdict_fails_dry_run`, `test_recipe_that_cannot_destroy_without_instance_ref_fails_validation`)
- **CFG-VAL-8.** A step that raised reports `<ClassName>: <message>`, unlike a driver
  run, because the message is the drafting loop's repair signal and the context holds
  nothing real. By decision; see the `_validation_error_text` docstring. \
  Enforced in: `services/execution/app/services/package_validator.py` (`_validation_error_text`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_method_exception_fails_dry_run`)
- **CFG-VAL-9.** A broken package answers 200 with a failing report, never an error
  status; the report is `{valid, structural, policy, schema, dry_run}`, each step of
  `dry_run.methods` carrying its action, verdict, output, error, duration, and
  transcript. \
  Enforced in: `services/execution/app/routers/validation.py` (`validate_package_endpoint`, `ValidationReport`) \
  Pinned by: `services/execution/tests/test_package_validator.py` (`test_route_happy_path_report_shape`, `test_route_returns_red_report_not_error_for_broken_package`); `tests/integration/test_package_validation.py` (`test_mock_hypervisor_package_validates_live`, `test_recipe_that_needs_instance_ref_to_destroy_fails_live`, `test_broken_package_returns_red_report_live`)

**Out of scope.** Uploading a validated draft, which goes through inventory's ordinary
driver upload (`inventory.md`); validating any connection type other than Hypervisor.

## 9. Errors

FastAPI validation errors (422) carry `detail` as a list of `{loc, msg, type}`; every
other error carries `detail` as a string or as the object shown. Inventory's immediate
apply reports execution's refusals inside a 200 body (CFG-APPLY-2), not as its own
status.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 401 | `Not authenticated` or `Could not validate credentials` | no bearer token, or one that does not verify | CFG-AUTH-1 |
| 403 | `manage permission required on this device (or active reservation ownership)` | an inventory write by a caller without `manage` or an active reservation | CFG-AUTH-3 |
| 403 | `Not authorized to cancel this job` | a cancel by someone other than the creator or an admin | CFG-JOB-10 |
| 403 | `Admin access required` | a non-admin `POST /execute` of another action; a non-admin `GET /runs` without `reservation_id`; a transcript of a run with no reservation | CFG-EXEC-1, CFG-RUN-1, CFG-TX-5 |
| 403 | `Admin access or device manage grant required` | a non-admin `configure` without a `manage` grant | CFG-EXEC-1, CFG-APPLY-5 |
| 403 | `Reservation not owned by caller` | a run list or transcript for a reservation the caller does not own | CFG-RUN-1, CFG-TX-5 |
| 403 | `Admin or superadmin role required` | a non-admin run detail or retry | CFG-RUN-4, CFG-RUN-5 |
| 403 | `Invalid internal token` | an internal route with a wrong token | CFG-VER-14, CFG-EXEC-6, CFG-VAL-1 |
| 404 | `Device not found` | an unknown device, or a hidden one for a non-admin read | CFG-AUTH-5, CFG-AUTH-8, CFG-VER-14, CFG-JOB-8 |
| 404 | `Config version not found` | an unknown version, or one of another device | CFG-AUTH-8, CFG-VER-7, CFG-VER-8 |
| 404 | `No config versions for device` | the internal latest read of a device with no versions | CFG-VER-14 |
| 404 | `Apply job not found` | an unknown job, or a job of a hidden device on the read | CFG-AUTH-5, CFG-JOB-9, CFG-JOB-10, CFG-JOB-11 |
| 404 | `Driver package not found` | the schema proxy for an unknown driver | CFG-SCHEMA-9 |
| 404 | `Device <id> not found`, `Template <id> not found` | execution cannot find the device or its template | CFG-EXEC-4 |
| 404 | `Execution run not found` | an unknown run | CFG-RUN-4, CFG-RUN-5, CFG-TX-5 |
| 400 | `Only failed or timed-out runs can be retried` | a retry of a run that is not `FAILED` or `TIMEOUT` | CFG-RUN-5 |
| 409 | `{"error": "driver_cannot_configure", "connection_type", "driver", "message"}` | a push to a device whose driver cannot configure | CFG-GATE-2, CFG-GATE-4 |
| 409 | `{"error": "device_has_no_driver", "message"}` | any execution action on a device with no driver | CFG-GATE-4 |
| 409 | `{"message": "Device has active reservations; restore blocked", "reservations": [...]}` | a restore while another user's reservation holds the device | CFG-VER-12 |
| 409 | `Job is '<status>', not cancellable` | a cancel of a job that is not `pending` | CFG-JOB-10 |
| 409 | `Source job is not a dry-run; nothing to promote`, `Source dry-run is '<status>'; only successful dry-runs can be promoted` | a confirm of the wrong kind of job | CFG-JOB-11 |
| 422 | `Device has no driver-defined connection_type; cannot validate config` | a version for a device without a driver connection type | CFG-VER-2 |
| 422 | `device '<name>': config failed schema validation: <message>` and the other validator messages | a config the schema refuses | CFG-VER-3, CFG-SCHEMA-3, CFG-SCHEMA-4, CFG-EXEC-10 |
| 422 | `scheduled_for must be in the future`, `scheduled_for must be within <N> days from now` | a bad schedule time | CFG-JOB-1, CFG-JOB-2 |
| 422 | `reservation_id must reference an active reservation you own that includes this device` | a schedule naming a reservation that fails CFG-JOB-4 | CFG-JOB-4 |
| 422 | `this driver does not advertise dry-run support; refuse to fire a dry-run that would hit the wire` | a dry-run schedule for a driver without dry-run support | CFG-JOB-6 |
| 422 | `internal execute is restricted to action='configure'; got '<action>'` | `POST /execute/internal` of another action | CFG-EXEC-6 |
| 422 | `Only the Hypervisor connection type is supported for package validation`; `package_b64 is not valid base64`; `package exceeds the <N> byte validation limit`; `package is empty` | a validation request the route refuses | CFG-VAL-1, CFG-VAL-2 |
| 500 | `Internal API token not configured` | an execution internal route when execution has no token | CFG-EXEC-6, CFG-VAL-1 |
| 500 | (unhandled) | concurrent version creates on a migrated schema; concurrent first loads of one driver; a non-object JSON body from an upstream check | CFG-VER-5, CFG-LOAD-6, CFG-AUTH-4 |
| 503 | `reservations service unreachable` | a schedule whose reservation cannot be checked | CFG-JOB-4 |
| 503 | `reservations service unreachable while checking active reservations` and the two sibling details | a restore whose guard cannot be answered | CFG-VER-13 |
| 503 | `Failed to fetch device: <text>`, `Failed to fetch template: <text>` | execution cannot read the device or template | CFG-EXEC-4 |
| 503 | the visibility lookup's own detail (`inventory.md`) | a non-admin read whose visibility lookup fails | CFG-AUTH-5 |

## 10. Interactions with other services

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| inventory to acl | acl | `POST /check` with the caller's token, 5 s | explicit `manage` for a write | fail closed: counts as no grant, the reservation check still runs (CFG-AUTH-4) |
| inventory to acl | acl | `POST /internal/check` (`X-Internal-Token`, 5 s) | fire-time `manage` | fail closed: counts as no grant (CFG-AUTH-7) |
| inventory to reservations | reservations | `GET /internal/active?user_id&device_id` (`X-Internal-Token`, 5 s) | active-reservation ownership, at request and fire time | fail closed: counts as not owner (CFG-AUTH-4, CFG-AUTH-7); at schedule time a non-200 is 503 (CFG-JOB-4) |
| inventory to reservations | reservations | `GET /internal/{id}` (`X-Internal-Token`, 5 s) | a schedule's and a fire's reservation status | schedule: 503 (CFG-JOB-4); fire: the job is skipped (CFG-SCHED-5) |
| inventory to reservations | reservations | `GET /internal/by-device/{id}` (`X-Internal-Token`, 5 s) | the restore guard | fail closed: 503 (CFG-VER-13) |
| inventory to execution | execution | `GET /drivers/{id}/config-schema` (`X-Internal-Token`, 10 s) | the published schema | fail open: the registry applies (CFG-SCHEMA-7) |
| inventory to execution | execution | `POST /execute` with the caller's token, 30 s | immediate apply | 200 with `status` `failed` and the error (CFG-APPLY-2) |
| inventory to execution | execution | `POST /execute/internal` (`X-Internal-Token`, 30 s) | a scheduled job | the job is `failed` (CFG-SCHED-9) |
| execution to acl | acl | `POST /check` with the caller's token, 5 s | a non-admin `configure` | fail closed: 403 (CFG-EXEC-1) |
| execution to reservations | reservations | `GET /{id}` with the caller's token, 5 s | run list and transcript ownership | fail closed: 403 (CFG-RUN-1, CFG-TX-5) |
| execution to inventory | inventory | `GET /devices/{id}/internal`, `GET /templates/{id}/internal` (`X-Internal-Token`, 10 s) | the device and template of an action | 404 is relayed; anything else 503 (CFG-EXEC-4) |
| execution to inventory | inventory | `GET /drivers/{id}/internal-download` (`X-Internal-Token`, 30 s) | the driver archive on a cache miss | the run is `FAILED` with `driver load failed: <ClassName>` (CFG-LOAD-2, CFG-EXEC-9) |
| execution to a driver | the driver package | a sandboxed subprocess | every driver method | the run records the failure (CFG-SBX-5, CFG-SBX-7, CFG-RUNSTATE-4) |

The calls other services make to this area are section 7 and, for ai-orchestrator's
`POST /execute`, `ai-features.md` (AI-COMMIT-15).

## 11. Configuration

| Setting | Default | Effect |
|---|---|---|
| `APPLY_SCHEDULER_ENABLED` (inventory) | `true` | Runs the apply scheduler in this process (CFG-SCHED-1) |
| `APPLY_SCHEDULER_INTERVAL_SECONDS` (inventory) | `30` | Tick interval and the base of the failure backoff (CFG-SCHED-1, CFG-SCHED-3) |
| `APPLY_JOB_MAX_HORIZON_DAYS` (inventory) | `30` | How far ahead a job may be scheduled (CFG-JOB-2) |
| `EXECUTION_SERVICE_URL`, `ACL_SERVICE_URL`, `RESERVATIONS_SERVICE_URL` (inventory) | the compose service names | Upstreams of section 10 |
| `INTERNAL_API_TOKEN` (both) | empty | Inventory: empty fails every internal-token call closed (CFG-AUTH-4, CFG-SCHED-5, CFG-JOB-4). Execution: empty answers 500 on its internal routes (CFG-EXEC-6) |
| `EXECUTION_TIMEOUT_SECONDS` (execution) | `30` | Timeout of every non-`status` driver call (CFG-SBX-5) |
| `STATUS_CHECK_TIMEOUT_SECONDS` (execution) | `10` | Timeout of `status` and of schema extraction (CFG-SBX-5, CFG-SCHEMA-11) |
| `DRIVER_RLIMIT_AS_BYTES`, `DRIVER_RLIMIT_CPU_SECONDS`, `DRIVER_RLIMIT_NOFILE`, `DRIVER_RLIMIT_NPROC` (execution) | `268435456`, `60`, `256`, `1024` | The child's resource limits; 0 turns one off (CFG-SBX-4) |
| `ALLOW_DRIVER_PIP_INSTALL` (execution) | `false` | Installs a package's `requirements.txt` at run time (CFG-SBX-8) |
| `DRIVER_CACHE_PATH` (execution) | `/data/driver-cache` | Where packages are extracted (CFG-LOAD-3) |
| `VALIDATE_PACKAGE_MAX_BYTES` (execution) | `10485760` | Largest package the validator decodes (CFG-VAL-2) |
| `VALIDATE_DRY_RUN_TIMEOUT_SECONDS` (execution) | `10` | Timeout of each validator lifecycle step (CFG-VAL-6) |

See [ENV_VARS.md](../ENV_VARS.md) for the rest.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/common/tests/test_device_config.py`, `services/common/tests/test_acl.py`; `services/inventory/tests/test_published_schema.py`, `test_apply_scheduler.py`; `services/execution/tests/test_driver_loader*.py`, `test_driver_sandbox*.py`, `test_runner.py`, `test_sandbox_isolation.py`, `test_dry_run.py`, `test_driver_transcript.py`, `test_config_schema_extraction.py`, `test_configure_capability_parity.py`, `test_package_validator.py`; frontend `frontend/src/test/components/DeviceConfigSection.test.tsx`, `AIApplyConfirmModal.test.tsx`, `frontend/src/test/api/deviceConfig.test.tsx`, `deviceConfigJobs.test.tsx` | SQLite in memory; the sandbox suites start real child processes |
| Functional (through the service API) | `services/inventory/tests/test_device_configs.py`, `test_device_configs_rbac.py`, `test_apply_jobs_reservation_owner.py`, `test_confirm_dry_run.py`, `test_configure_capability_gate.py`, `test_device_config_restore_reservation_guard.py`, `test_device_read_visibility_gate.py`, `test_apply_jobs_internal_summary.py`, `test_router_edge_cases.py`; `services/execution/tests/test_router_endpoints.py`, `test_router_direct.py`, `test_api_endpoints.py`, `test_command_log*.py`, `test_config_schema_endpoint.py`, `test_configure_capability_gate.py`, `test_execution_service_edges.py` | acl, reservations, and execution are patched |
| Integration (running stack) | `tests/integration/test_execution_configure_gate.py`, `test_execution_result_gating.py`, `test_package_validation.py`; the NOS lab tiers under `tests/nos_lab/` (`test_frr_mgmt_driver_live.py` drives the Management driver's `configure`) | None for config versions, scheduled applies, the scheduler, dry runs, or the schema proxy |
| Stress and load | None | `tests/load/locustfile.py` has no configuration task |
| Browser end-to-end | `tests/e2e/test_flows_effects_playwright.py` (`test_device_config_version_cycle`: create, view, diff, restore), `tests/e2e/test_device_config_apply.py` (the section and panel render) | Apply, schedule, cancel, and the dry-run review are not driven in a browser; nightly and the gates only |

Run while writing this document: the inventory suite (985 passed), the common suite
(462 passed, 7 skipped), the execution suite (1284 passed, 5 skipped), the four cited
frontend test files (18 passed), and `tests/unit/` (415 passed). Not run: the
integration, load, NOS lab, and end-to-end suites (no stack; a gate run held the host's
ports). Every rule was checked by reading the code at `cd3eaeed`; unpinned rules were
confirmed by reading only.

## 13. Known limits and gaps

### Open defects

- #1088 (CFG-STATE-4): a cancel racing the scheduler's claim answers 204 while the job
  fires.
- #1089 (CFG-STATE-5): the stale sweep measures from `scheduled_for`, so a late-claimed
  job can be fired twice by two schedulers.
- #1095 (CFG-VER-5): version numbers are not safe under concurrent writes, and fresh
  schemas lack the unique index.
- #1090 (CFG-SCHED-8): scheduled runs carry no reservation, so a reservation owner cannot
  read the dry-run transcript the review dialog asks for.
- #1091 (CFG-RUN-6): retrying a failed dry run pushes the configuration for real.
- #1092 (CFG-APPLY-5): immediate apply admits reservation owners whom execution then
  refuses, and [ROLES.md](../ROLES.md) says they pass.
- #1093 (CFG-APPLY-2, CFG-SCHED-9, CFG-EXEC-4): exception and upstream text reach job
  rows and API answers.
- #1094 (CFG-APPLY-3, CFG-APPLY-6, CFG-SCHED-10): immediate and scheduled applies judge
  a run and move the current config pointer differently.
- #1096 (CFG-AUTH-4, CFG-SCHEMA-7): a 200 whose JSON body is not an object raises instead
  of failing closed or open.
- #1097 (CFG-LOAD-6, CFG-RUNSTATE-5): concurrent first loads of a driver fail, and a run
  whose action raises unexpectedly stays `PENDING` or `RUNNING`.
- #1098 (CFG-UI-4): the device page shows structured refusals as generic text.

Documentation that disagrees with the code is tracked in #1099; the unpinned rules below
that should have a test are tracked in #1100.

### Limits by decision

- A schedule's reservation check proves ownership of some active reservation holding
  the device, not of the one named (the `_validate_reservation_for_job` docstring)
  (CFG-JOB-5).
- A driver's dry-run declaration is trusted ([DRIVERS.md](../DRIVERS.md), "Dry-run
  support") (CFG-DRY-4).
- Driver packages are trusted code and the sandbox limits resources only
  ([DRIVERS.md](../DRIVERS.md), "Execution sandbox") (CFG-SBX-9).
- The validator reports a raised step's message, which a driver run never stores (the
  `_validation_error_text` docstring) (CFG-VAL-8).
- The published-schema lookup fails open to the registry (ADR 0002 and the
  `published_schema.py` module docstring) (CFG-SCHEMA-7), while the restore guard fails
  closed (the `reservation_guard.py` module docstring) (CFG-VER-13).
- Only `Management` drivers can be pushed a configuration; versions on other types
  store intent ([DRIVERS.md](../DRIVERS.md), "Apply versus config versions") (CFG-GATE-1,
  CFG-GATE-3).

### Rules with no test

- CFG-STATE-4: a cancel racing the claim.
- CFG-RUNSTATE-5: a run left `PENDING` or `RUNNING` by an unexpected exception.
- CFG-AUTH-6: the write routes skip visibility.
- CFG-VER-5: concurrent version numbering.
- CFG-VER-10: the default restore description.
- CFG-VER-15: no version delete; the cascade from the device.
- CFG-APPLY-6: the current config pointer's writers and readers.
- CFG-JOB-3: the time checks run before the lookups.
- CFG-JOB-5: the named reservation is not itself proven.
- CFG-JOB-6: the schedule-time dry-run support check.
- CFG-JOB-13: confirm repeats no schedule-time check.
- CFG-SCHED-8: the fire request's body.
- CFG-SCHED-10: a scheduled success leaves the pointer.
- CFG-EXEC-3: the body's reservation and options are taken as sent.
- CFG-EXEC-8: `method_kwargs` stored on the run.
- CFG-RUN-3: an owner's list holds every run of the reservation.
- CFG-RUN-6: a retried dry run.
- CFG-DRY-4: the dry-run declaration is not verified.
- CFG-LOAD-6: concurrent first loads.
- CFG-SBX-9: no isolation beyond resource limits.
- CFG-UI-4: the Apply dialog's outcomes.
- CFG-UI-5: the scheduled-applies panel.
