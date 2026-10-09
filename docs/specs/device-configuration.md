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
| User | Read the config versions, diffs, and apply jobs of a device visible to them; read any driver's config schema; with a `manage` grant on the device or an active reservation of it, create and restore versions, apply now, schedule, and confirm a dry run; cancel their own pending jobs; with a `manage` grant, run `configure` through `POST /execute`; list the runs of a reservation they own that ran on devices visible to them and read those runs' command transcripts | See a hidden device's history or runs; run any driver action other than `configure`; read a single run's detail; retry a run |
| Admin | Everything on every device; run any driver action; list, read, and retry any run | Apply to a device whose driver contract has no `configure` (CFG-GATE-2, CFG-GATE-4) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Read a device's latest config version and its apply-job summary; read a driver's published schema; run `configure` through `POST /execute/internal`; validate a package | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Config version | One numbered snapshot of a device's configuration: the config object, the connection type it was validated against, a free-text description, the author, an optional `restored_from_id`, and the id of the last run that applied it | inventory | `device_config_versions` (`DeviceConfigVersion` in `services/inventory/app/models/device_config_version.py`); `last_apply_run_id` is a bare execution id, no foreign key |
| Current config pointer | `devices.current_config_version_id`, the version a successful apply last pushed for real, immediate or scheduled (a dry run never moves it); no route returns it | inventory | `devices` |
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
| job `pending` | job `cancelled` | `DELETE /apply-jobs/{id}` | creator or admin; conditional update on `pending` | nothing | CFG-STATE-3, CFG-STATE-4 |
| job `running` | job `pending` | the stale sweep | `fired_at` null and claimed (`claimed_at`) over 300 seconds ago | nothing | CFG-STATE-5 |
| job `running` | job `skipped` | the apply scheduler | reservation not active, or creator not authorized | nothing | CFG-STATE-6 |
| job `running` | job `success` or `failed` | the apply scheduler | the execute outcome | nothing | CFG-STATE-7 |
| any job status | job `failed` | the scheduler loop after `fire_job` raised | none (by id) | nothing | CFG-STATE-8 |
| (none) | run `PENDING` | `run_driver_action` | the action gate passed | nothing | CFG-RUNSTATE-1 |
| run `PENDING` | run `FAILED` | `run_driver_action` | driver load failed, or `configure` input refused | nothing | CFG-RUNSTATE-2 |
| run `PENDING` | run `RUNNING` | `run_driver_action` | none | nothing | CFG-RUNSTATE-3 |
| run `RUNNING` | run `FAILED` | `run_driver_action` | dry run refused | nothing | CFG-RUNSTATE-3 |
| run `RUNNING` | run `SUCCESS`, `FAILED`, or `TIMEOUT` | `run_driver_action` | the sandbox result | nothing | CFG-RUNSTATE-4 |
| run `PENDING` or `RUNNING` | run `FAILED` | `run_driver_action` (`_fail_run_unexpected`) | an unexpected exception; conditional update on `PENDING` or `RUNNING` | nothing | CFG-RUNSTATE-5 |

**Concurrency.** The scheduler's claim and the cancel are compare-and-swap updates on
`pending`, so exactly one of them wins a pending job. `_due_jobs` also selects with
`FOR UPDATE SKIP LOCKED` on Postgres, but the lock is released at the first commit
inside `fire_job`, so the claim is what keeps two schedulers off one job. Every other
job write and every run write reads the row and overwrites it, except the final
`FAILED` write after an unexpected exception (CFG-RUNSTATE-5), which is conditional on
`PENDING` or `RUNNING`.

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
- **CFG-STATE-4.** The cancel is a compare-and-swap on `status = 'pending'`, the same
  guard as the claim (CFG-STATE-2), so exactly one of the two wins. A cancel that loses
  (the claim committed after the cancel read the row) changes nothing and answers 409
  `Job is '<status>', not cancellable` with the status it finds; a claim that loses
  fires nothing. A 204 therefore means the job never fires. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`cancel_apply_job`); `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_cancel_loses_to_a_claim_that_committed_after_its_read`, `test_cancelled_job_is_never_fired_by_a_later_claim`); `services/inventory/tests/test_config_apply_races_live_pg.py` (`test_cancel_holds_the_row_first_claim_fires_nothing`, `test_claim_holds_the_row_first_cancel_answers_409`)
- **CFG-STATE-5.** The claim writes `claimed_at`. Each scheduler tick first returns to
  `pending`, with `claimed_at` cleared, every `running` job whose `fired_at` is null and
  whose `claimed_at` is more than `STALE_RUNNING_AFTER_SECONDS` (300) in the past. The
  age is measured from the claim, so a job claimed late (a backlog or an outage) is not
  re-queued while its claimer is still firing it; a job claimed before the column
  existed (inventory migration 0024) has it null and is measured from `scheduled_for`. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_resweep_stale_running`, `STALE_RUNNING_AFTER_SECONDS`, `fire_job`); `services/inventory/app/models/device_config_apply_job.py` (`DeviceConfigApplyJob`); `services/inventory/migrations/versions/0024_apply_job_claimed_at.py` (`upgrade`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_resweep_stale_running_requeues`, `test_resweep_leaves_fresh_running_alone`, `test_resweep_leaves_terminal_jobs_alone`, `test_claim_records_claimed_at`, `test_sweep_does_not_requeue_a_late_claimed_job_still_firing`, `test_sweep_requeues_a_job_claimed_past_the_threshold_and_clears_the_claim`, `test_sweep_leaves_a_recent_claim_alone_whatever_its_scheduled_time`)
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
- **CFG-RUNSTATE-5.** An exception nothing else handles that escapes the work after
  the row is written (a database error, a sandbox call that raises) ends the run
  `FAILED` with `execution failed: <ClassName>` and `completed_at`, through a
  compare-and-swap on `PENDING` or `RUNNING` after a session rollback, so a run already
  in a final status keeps it; the exception text goes to the log message only, and the
  call answers as any other finished run (CFG-EXEC-5). If that write fails, the
  original exception is raised. The 422 of a refused `configure` input (CFG-EXEC-10)
  passes through unchanged. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `_fail_run_unexpected`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_unexpected_load_error_records_failed`, `test_run_driver_action_unexpected_sandbox_error_records_failed`, `test_fail_run_unexpected_keeps_a_final_status`, `test_fail_run_unexpected_reraises_original_when_the_write_fails`); `services/execution/tests/test_router_endpoints.py` (`test_execute_unexpected_load_error_answers_201_with_failed_run`)

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
| GET | `/runs` | admin; a user, with the `reservation_id` of a reservation they own (runs on devices they may see) | 200 | CFG-RUN-1 to CFG-RUN-3 |
| GET | `/runs/{id}` | admin | 200 | CFG-RUN-4 |
| GET | `/runs/{id}/commands` | admin; a user who owns the run's reservation and may see the run's device | 200 | CFG-TX-5 |
| POST | `/runs/{id}/retry` | admin | 200 | CFG-RUN-5 to CFG-RUN-7 |

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
- **CFG-AUTH-3.** Any other caller passes create, restore, schedule, and confirm
  (the immediate apply is narrower, CFG-APPLY-5) only when it holds an explicit `manage` grant on the device (acl `POST /check` with
  the caller's own token) or owns an `ACTIVE` reservation that holds the device
  (reservations `GET /internal/active?user_id&device_id` with the internal token);
  otherwise 403
  `manage permission required on this device (or active reservation ownership)`. \
  Enforced in: `services/inventory/app/services/manage_guard.py` (`_user_can_manage_device`); `services/common/herd_common/acl.py` (`user_has_manage_or_owns_active_reservation`, `_explicit_acl_manage`, `_owns_active_reservation`) \
  Pinned by: `services/inventory/tests/test_device_configs_rbac.py` (`test_non_admin_create_version_denied_without_acl_grant`, `test_non_admin_create_version_succeeds_with_acl_grant`, `test_non_admin_restore_denied_without_acl_grant`, `test_non_admin_schedule_denied_without_acl_grant`, `test_non_admin_schedule_succeeds_with_acl_grant`); `services/inventory/tests/test_apply_jobs_reservation_owner.py` (`test_reservation_owner_can_schedule_without_explicit_grant`, `test_reservation_owner_can_create_config_version`, `test_non_owner_without_grant_still_rejected`); `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_non_admin_without_grant_rejected`, `test_confirm_non_admin_owner_allowed`); `services/common/tests/test_acl.py` (`test_explicit_grant_returns_true`, `test_no_explicit_grant_falls_through_to_reservation_check`, `test_no_grant_no_reservation_returns_false`)
- **CFG-AUTH-4.** The check fails closed. With no bearer token, or when acl is
  unreachable, answers non-200, or answers non-JSON, the grant counts as absent and the
  reservation check still runs; that check answers no when no internal token is
  configured or reservations is unreachable, non-200, or non-JSON. A 200 whose JSON body
  is not an object (a list, string, number, or null) is an unusable answer and counts as
  no on every leg, the internal-token grant check included. \
  Enforced in: `services/common/herd_common/acl.py` (`user_has_grant`, `_owns_active_reservation`, `_explicit_acl_manage_internal`, `_json_flag`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_no_bearer_token_skips_acl_check_and_tries_reservations`, `test_acl_service_unreachable_still_tries_reservations`, `test_acl_5xx_falls_through_to_reservations`, `test_malformed_acl_response_falls_through_to_reservations`, `test_reservations_service_unreachable_returns_false`, `test_reservations_non_200_returns_false`, `test_malformed_reservation_response_returns_false`, `test_no_internal_token_skips_reservation_lookup`, `test_acl_answer_not_an_object_is_no_grant`, `test_reservations_answer_not_an_object_is_not_owner`, `test_manage_internal_answer_not_an_object_is_false`)
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
  Pinned by: `services/inventory/tests/test_device_config_write_rules.py` (`test_write_routes_do_not_check_device_visibility`)
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
  config pointer; only a successful apply does (CFG-APPLY-4, CFG-SCHED-10). \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`create_config_version`, `restore_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_create_version_does_not_flip_current_pointer`, `test_restore_does_not_flip_current_pointer`)
- **CFG-VER-5.** The next number is the device's current maximum plus one, and the
  unique index `ix_device_config_versions_device_version` on device and version number
  is the arbiter: the model declares it, so a schema built by `create_all` has it, and
  inventory migration 0023 adds it (renumbering any duplicates above the device's
  maximum, earliest row kept) to a schema built before the declaration. A create or
  restore whose number collides rolls back, recomputes, and tries again, at most five
  times; then it answers 409
  `Could not allocate a config version number under concurrent writes; retry the request`.
  Only a unique violation is retried. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`_commit_new_version`, `_next_version_number`, `VERSION_ALLOCATION_CONFLICT_DETAIL`); `services/inventory/app/models/device_config_version.py` (`DeviceConfigVersion`); `services/inventory/migrations/versions/0023_config_version_unique_index.py` (`upgrade`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_model_declares_unique_device_version_index`, `test_create_all_schema_refuses_duplicate_version_number`, `test_create_retries_after_version_number_collision`, `test_restore_retries_after_version_number_collision`, `test_create_answers_409_when_version_allocation_keeps_colliding`); `services/inventory/tests/test_config_apply_races_live_pg.py` (`test_concurrent_creates_get_distinct_numbers`)
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
  the source's number; a given `description` is kept as sent. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`restore_config_version`) \
  Pinned by: `services/inventory/tests/test_device_config_write_rules.py` (`test_restore_without_description_is_labelled_with_the_source_number`)
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
  Pinned by: `services/inventory/tests/test_device_config_write_rules.py` (`test_no_route_deletes_or_edits_a_config_version`, `test_version_cannot_be_deleted_or_edited_over_http`, `test_deleting_a_device_deletes_its_versions_and_apply_jobs`)

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
  a non-JSON body, a JSON body that is not an object, `has_schema` false, or a
  non-object `schema` all mean no published schema, and the registry applies. \
  Enforced in: `services/inventory/app/services/published_schema.py` (`_fetch_published_schema`, `published_schema_for_device`) \
  Pinned by: `services/inventory/tests/test_published_schema.py` (`test_valid_200_parses_and_returns_schema`, `test_200_malformed_body_falls_back_to_none`, `test_200_has_schema_false_falls_back_to_none`, `test_non_200_falls_back_to_none_with_warning`, `test_transport_error_falls_back_to_none`, `test_published_schema_for_device_returns_none_when_no_driver`, `test_200_body_not_an_object_falls_back_to_none`); `services/inventory/tests/test_device_configs.py` (`test_create_falls_back_to_registry_when_no_published_schema`, `test_create_fails_open_to_registry_when_execution_unreachable`)
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
  timeout, sending `device_id`, `action` `configure`, `user_id` (the caller), the
  version's config as `method_kwargs`, and the version's id as `config_version_id` (the
  reference a retry reads the configuration back from, CFG-RUN-7); it sends no
  `dry_run` and no `reservation_id`. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_apply_calls_execution_with_method_kwargs`)
- **CFG-APPLY-2.** Every execution outcome answers 200
  `{version_id, run_id, status, error}`: a transport error is `failed` with
  `execution service unreachable (<ClassName>)`, and an execution status of 400 or more
  is `failed` with `execution answered HTTP <status>`, followed by `: <message>` only
  when the detail is an object carrying a non-empty string `message` (the structured
  `driver_cannot_configure` and `device_has_no_driver` refusals). A plain string detail,
  a validation list, and a body that is not JSON are never relayed; the exception text
  and the raw body go to the log message only. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`); `services/inventory/app/services/apply_outcome.py` (`unreachable_error`, `refusal_error`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_apply_handles_execution_transport_error`, `test_apply_handles_non_json_error_body`); `services/inventory/tests/test_device_configs.py` (`test_apply_reports_execution_403_by_status_only`); `services/inventory/tests/test_apply_outcome_errors.py` (`test_unreachable_error_is_the_class_name_only`, `test_unreachable_error_names_each_transport_class`, `test_refusal_error_carries_status_and_structured_message_only`, `test_refusal_error_non_json_body_is_status_only_and_logged`)
- **CFG-APPLY-3.** Both apply paths judge a 2xx answer by one rule: only a JSON object
  whose run `status` is `SUCCESS` (any case) is a success. A missing or null status is
  `failed` with `execution returned non-success status`, and a body that is not JSON, or
  JSON that is not an object, is `failed` with `execution returned malformed JSON`. For
  the immediate apply `status` is otherwise the run's status in lower case and `error`
  the run's (`execution returned non-success status` when it has none); a scheduled job
  records the same verdict as `success` or `failed` (CFG-SCHED-9). \
  Enforced in: `services/inventory/app/services/apply_outcome.py` (`judge_success_answer`, `ApplyOutcome`, `MALFORMED_ANSWER_ERROR`, `NON_SUCCESS_ERROR`); `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_router_edge_cases.py` (`test_apply_handles_non_json_success_body`); `services/inventory/tests/test_device_configs.py` (`test_apply_success_flips_current_pointer`, `test_immediate_apply_without_a_success_status_is_failed`, `test_immediate_apply_relays_a_timeout_run_status`)
- **CFG-APPLY-4.** When the answer names a run, the version's `last_apply_run_id` is set
  (null when the id is not a UUID) and, only when the status is `success`, the device's
  current config pointer moves to the version; both are committed before the answer. \
  Enforced in: `services/inventory/app/routers/device_configs.py` (`apply_config_version`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_apply_success_flips_current_pointer`, `test_apply_failure_does_not_flip_current_pointer`); `services/inventory/tests/test_device_configs_rbac.py` (`test_apply_with_malformed_run_id_returns_200_and_persists_pointer`)
- **CFG-APPLY-5.** The immediate apply forwards the caller's token to execution, which
  admits a non-admin's `configure` only with an explicit `manage` grant (CFG-EXEC-1), so
  inventory asks the same question up front with no reservation widening: a non-admin
  passes only with an explicit acl `manage` grant on the device (no bearer token, or any
  acl failure, counts as no grant), otherwise 403
  `manage grant required on this device for an immediate apply (a reservation owner can schedule the apply instead)`
  before any execution call. A reservation owner without a grant schedules the apply
  instead (CFG-AUTH-3), which runs through execution's internal route. \
  Enforced in: `services/inventory/app/services/manage_guard.py` (`_user_has_explicit_manage`, `IMMEDIATE_APPLY_FORBIDDEN_DETAIL`); `services/inventory/app/routers/device_configs.py` (`apply_config_version`); `services/execution/app/routers/executions.py` (`manual_execute`, `_user_has_acl_manage`) \
  Pinned by: `services/inventory/tests/test_apply_jobs_reservation_owner.py` (`test_reservation_owner_without_grant_is_refused_immediate_apply`, `test_explicit_manage_check_without_token_is_false_and_asks_nobody`, `test_explicit_manage_check_relays_the_acl_answer`); `services/inventory/tests/test_device_configs_rbac.py` (`test_non_admin_apply_denied_without_acl_grant`); `services/execution/tests/test_router_endpoints.py` (`test_execute_non_admin_configure_without_grant_forbidden`)
- **CFG-APPLY-6.** The device's current config pointer is written through one helper by
  both paths: an immediate apply that succeeded and a scheduled job that succeeded and
  was not a dry run (CFG-SCHED-10). No route returns it; the latest-version internal
  read deliberately uses the highest number instead (CFG-VER-7). \
  Enforced in: `services/inventory/app/services/apply_outcome.py` (`move_current_config_pointer`); `services/inventory/app/routers/device_configs.py` (`apply_config_version`); `services/inventory/app/services/apply_scheduler.py` (`fire_job`) \
  Pinned by: `services/inventory/tests/test_device_configs.py` (`test_apply_success_flips_current_pointer`, `test_apply_failure_does_not_flip_current_pointer`); `services/inventory/tests/test_apply_scheduler.py` (`test_scheduled_apply_moves_pointer_only_on_a_real_success`)

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
  bad time on an unknown device is a 422, and before the authorization and reservation
  checks, which then ask nobody. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: `services/inventory/tests/test_device_config_write_rules.py` (`test_schedule_time_checks_run_before_the_device_and_version_lookups`); `services/inventory/tests/test_apply_jobs_reservation_scope.py` (`test_reservation_check_runs_after_the_time_checks_404s_and_403`)
- **CFG-JOB-4.** A `reservation_id`, when given, is checked after the authorization
  check and the driver gate and before the dry-run gate, with two reads of reservations,
  both with the internal token and 5 seconds: `GET /internal/{id}` must answer 200 with
  `is_active` true (status `ACTIVE` inside its window, the judgement the scheduler
  repeats at fire time, CFG-SCHED-5), and `GET /internal/by-device/{device_id}`, which
  lists every reservation of any status and any owner that holds the device, must list
  the id. A 404 or an inactive reservation is 422 without the second read; an id the
  by-device list does not hold is 422. The 422 detail is `RESERVATION_MISMATCH_ERROR`
  (`reservation_id must reference an active reservation you own that includes this device`)
  for a non-admin and `RESERVATION_MISMATCH_ADMIN_ERROR`
  (`reservation_id must reference an active reservation that includes this device`) for an
  admin. No internal token, a transport error, any other status, a body that is not JSON,
  a status body that is not an object, or a by-device body that is not a list of objects
  with string `id` and `user_id` is 503 `RESERVATION_UNAVAILABLE_ERROR`
  (`Could not verify the reservation; nothing was scheduled. Retry the request.`); the
  reason goes to the log only. Nothing is written on any refusal. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`_validate_reservation_for_job`, `_reservation_unavailable`, `RESERVATION_MISMATCH_ERROR`, `RESERVATION_MISMATCH_ADMIN_ERROR`, `RESERVATION_UNAVAILABLE_ERROR`) \
  Pinned by: `services/inventory/tests/test_apply_jobs_reservation_scope.py` (`test_reservation_id_valid_and_owned_schedules_successfully`, `test_foreign_reservation_id_returns_422_and_writes_no_row`, `test_reservation_id_inactive_returns_422_and_writes_no_row`, `test_reservation_id_validation_fails_closed_when_unreachable`, `test_reservation_id_answer_not_an_object_fails_closed_503`, `test_reservation_check_without_an_internal_token_fails_closed`, `test_schedule_without_a_reservation_asks_nothing`); `tests/integration/test_config_apply_flow.py` (`test_scheduled_dry_run_fires_and_its_confirm_queues_a_real_apply`, `test_schedule_refuses_a_reservation_that_does_not_hold_the_device`)
- **CFG-JOB-5.** The named reservation itself must hold the device and, for a non-admin,
  belong to the caller (its `user_id` in the by-device list); an admin is exempt from
  ownership only, not from activeness or the device. A non-admin who owns one active
  reservation holding the device cannot name another of theirs that does not hold it, or
  another user's that does (issue #1104). The check does not ask reservations
  `GET /internal/active`: the named reservation being active, holding the device, and the
  caller's implies that answer. The reservation-owner widening of CFG-AUTH-3 is a separate
  check and is unchanged. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`_validate_reservation_for_job`) \
  Pinned by: `services/inventory/tests/test_apply_jobs_reservation_scope.py` (`test_non_admin_cannot_name_their_own_reservation_without_the_device`, `test_reservation_id_active_but_not_owned_by_caller_returns_422`, `test_admin_may_name_another_users_active_reservation_holding_the_device`, `test_admin_cannot_name_an_active_reservation_without_the_device`)
- **CFG-JOB-6.** A dry-run job needs the device's driver to declare `supports_dry_run`;
  otherwise 422
  `this driver does not advertise dry-run support; refuse to fire a dry-run that would hit the wire`. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`schedule_apply_job`) \
  Pinned by: `services/inventory/tests/test_dry_run_gate.py` (`test_schedule_dry_run_rejected_against_non_supporting_driver`, `test_schedule_dry_run_rejected_when_metadata_absent`, `test_schedule_dry_run_succeeds_against_supporting_driver`)
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
  `Source dry-run is '<status>'; only successful dry-runs can be promoted`). The order
  is 404 `Apply job not found` for an unknown job (no authority lookup runs), then the
  caller's authority (CFG-AUTH-3) on the job's device, then the two 409s, so a caller
  without authority gets the 403 in every state and learns only that the job exists. \
  Enforced in: `services/inventory/app/routers/apply_jobs.py` (`confirm_dry_run_apply`) \
  Pinned by: `services/inventory/tests/test_confirm_dry_run.py` (`test_confirm_404_when_job_missing`, `test_confirm_409_when_source_is_not_dry_run`, `test_confirm_409_when_dry_run_pending`, `test_confirm_409_when_dry_run_failed`, `test_confirm_non_owner_gets_403_not_409_in_every_refusing_state`, `test_confirm_authorized_non_admin_still_gets_409`, `test_confirm_unknown_job_is_404_before_authority`)
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
  Pinned by: `services/inventory/tests/test_device_config_write_rules.py` (`test_confirm_repeats_no_schedule_time_check`)
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
  transport error, another status, non-JSON, or JSON that is not an object skips the job with
  `reservation not currently active`. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_reservation_active`, `fire_job`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_fire_job_skipped_when_reservation_not_active`, `test_reservation_gate_hits_internal_url`, `test_reservation_gate_closed_default_when_token_missing`, `test_reservation_gate_closed_default_on_403`, `test_reservation_active_http_error_returns_false`, `test_reservation_active_malformed_json_returns_false`, `test_reservation_active_answer_not_an_object_returns_false`)
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
  creator), the version's config as `method_kwargs`, `dry_run`, the job's
  `reservation_id` (null for a job tied to none), so the run carries the reservation and
  its owner can read the transcript (CFG-TX-5) and find the run in the reservation's run
  list, and the job's version id as `config_version_id` (CFG-RUN-7). \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_post_internal_execute`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_post_internal_execute_sends_the_job_reservation_id`)
- **CFG-SCHED-9.** The job is `success` only for a 2xx JSON answer whose `status` is
  `SUCCESS` in any case. Otherwise it is `failed` with
  `execution service unreachable (<ClassName>)` (a transport error), the immediate
  apply's refusal text (400 or more, CFG-APPLY-2),
  `execution returned malformed JSON`, or the run's `error`, else
  `execution returned non-success status` (a missing or null status included). A run id
  that is not a UUID is stored as null. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`_post_internal_execute`); `services/inventory/app/services/apply_outcome.py` (`unreachable_error`, `refusal_error`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_post_internal_execute_http_error`, `test_post_internal_execute_error_body_not_json`, `test_post_internal_execute_refusal_stores_herd_text_only`, `test_post_internal_execute_success_body_not_json`, `test_post_internal_execute_malformed_run_id_degrades_to_none`, `test_post_internal_execute_non_success_status`, `test_post_internal_execute_missing_status_records_failed`, `test_post_internal_execute_null_status_records_failed`)
- **CFG-SCHED-10.** A successful job sets its version's `last_apply_run_id` when the
  answer named a run, and, unless it was a dry run, moves the device's current config
  pointer to the version, the same record an immediate apply writes (CFG-APPLY-6). A
  failed job, and a successful dry run, leave the pointer where it was. \
  Enforced in: `services/inventory/app/services/apply_scheduler.py` (`fire_job`); `services/inventory/app/services/apply_outcome.py` (`move_current_config_pointer`) \
  Pinned by: `services/inventory/tests/test_apply_scheduler.py` (`test_scheduled_apply_moves_pointer_only_on_a_real_success`)

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
  and 5 seconds through the shared closed-by-default reader (CFG-AUTH-4); no token, a
  transport error, a non-200, non-JSON, a JSON body that is not an object (a list,
  string, number, or null), or no grant is 403
  `Admin access or device manage grant required`. Owning a reservation does not count. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`, `_user_has_acl_manage`); `services/common/herd_common/acl.py` (`user_has_grant`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_execute_non_admin_status_action_forbidden`, `test_execute_non_admin_configure_without_grant_forbidden`, `test_execute_non_admin_configure_with_grant_succeeds`, `test_execute_non_admin_configure_acl_answer_not_an_object_forbidden`); `services/execution/tests/test_router_direct.py` (`test_acl_manage_false_without_authorization`, `test_acl_manage_false_on_httpx_error`, `test_acl_manage_false_on_non_200`, `test_acl_manage_false_on_malformed_json`, `test_acl_manage_true_when_allowed`, `test_acl_manage_false_when_not_allowed`, `test_acl_manage_false_when_answer_not_an_object`)
- **CFG-EXEC-2.** On `POST /execute` the run is attributed to the token's `sub`; the
  body's required `user_id` is ignored. \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_manual_execute_overrides_user_id_with_jwt_subject`)
- **CFG-EXEC-3.** On `POST /execute`, after the checks of CFG-EXEC-1 and before any
  device read, run row, or driver call, a `reservation_id` must name a reservation whose
  device set holds the device (any status) and, for a non-admin, one the caller owns;
  admins are exempt from ownership only. It is read from reservations
  `GET /internal/by-device/{device_id}` with the internal token and 5 seconds, which lists
  every holder of the device with its owner (the caller-token `GET /{id}` answers only
  the caller's own reservations, admins included, so it cannot confirm an admin's id).
  A reservation that is not listed, or a non-admin's reservation owned by someone else,
  is 422 `reservation_id must reference a reservation you own that includes this device`
  (an admin's: `reservation_id must reference a reservation that includes this device`);
  no internal token, a transport error, a non-200, a body that is not JSON, or a body that
  is not a list of objects with string `id` and `user_id` is 503
  `Could not verify the reservation; nothing was run. Retry the request.` An omitted
  `reservation_id` asks nothing. `POST /execute/internal` does not check it (its caller is
  trusted through the internal token). `port_a`, `port_b`, `method_kwargs`, `dry_run`, and
  `config_version_id` are used as sent; the `config_version_id` is only recorded (a retry
  checks it, CFG-RUN-7). \
  Enforced in: `services/execution/app/routers/executions.py` (`manual_execute`, `_assert_execute_reservation`) \
  Pinned by: `services/execution/tests/test_manual_execute_reservation_scope.py` (`test_owner_runs_configure_under_their_own_reservation`, `test_non_admin_cannot_tag_a_run_with_another_users_reservation`, `test_non_admin_cannot_tag_a_run_with_a_reservation_without_the_device`, `test_admin_may_tag_a_run_with_another_users_reservation_holding_the_device`, `test_admin_cannot_tag_a_run_with_a_reservation_without_the_device`, `test_execute_without_a_reservation_asks_nothing`, `test_reservation_check_fails_closed`, `test_reservation_check_without_an_internal_token_fails_closed`, `test_non_admin_without_a_grant_is_refused_before_the_reservation_check`, `test_internal_execute_reservation_is_not_checked`, `test_execute_uses_ports_arguments_and_dry_run_as_sent`); `tests/integration/test_execution_device_scope.py` (`test_execute_refuses_a_reservation_that_does_not_hold_the_device`, `test_execute_accepts_the_callers_reservation_holding_the_device`)
- **CFG-EXEC-4.** The device and its template are read through inventory's internal
  routes with the internal token and 10 seconds; a 404 is 404 `Device <id> not found` or
  `Template <id> not found`, and any other failure is 503 `Failed to fetch device: <reason>`
  (or template), where the reason is HERD-authored: `upstream service answered HTTP <status>`,
  `upstream service unreachable (<ClassName>)`, `upstream service answered with a malformed body`
  (a body that is not JSON or not a JSON object), or the exception's class name. The
  exception text goes to the log message only. \
  Enforced in: `services/execution/app/services/execution_service.py` (`fetch_device`, `fetch_template`, `_fetch_inventory_internal`, `_inventory_failure_text`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_fetch_device_returns_payload`, `test_fetch_device_404_raises_404`, `test_fetch_device_other_error_raises_503`, `test_fetch_template_404_raises_404`, `test_fetch_template_other_error_raises_503`, `test_fetch_failure_detail_never_carries_foreign_text`)
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
  `password`-typed template field replaced by `***REDACTED***`, plus the keys CFG-EXEC-8
  and CFG-RUN-6 add. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`, `redact_context_for_logging`, `extract_password_keys`) \
  Pinned by: `services/execution/tests/test_api_endpoints.py` (`test_execute_success`); `services/execution/tests/test_execution_service.py` (`test_redact_context`, `test_extract_password_keys`)
- **CFG-EXEC-8.** A non-empty `method_kwargs` is stored in `input_params` under
  `method_kwargs` only as a masked copy, for every action: a value under a key whose name
  matches the log formatter's credential key pattern is `[redacted]`, and inside a string
  everything after a credential keyword token (`password`, `passwd`, `passphrase`,
  `secret`, `community`, `key`, `key-string`, `md5`, or a hyphenated or underscored
  name ending in one) on the same line is `[redacted]`. `method_kwargs_redacted` records
  whether anything was masked, and a `config_version_id` the caller sent is stored
  beside it. The driver receives the arguments as sent. \
  Enforced in: `services/execution/app/services/execution_service.py` (`create_execution_run`); `services/common/herd_common/config_redaction.py` (`redact_config`, `redact_command_text`) \
  Pinned by: `services/execution/tests/test_run_arguments_storage.py` (`test_configure_run_stores_no_credential_value_from_an_frr_config`, `test_run_without_credentials_stores_its_arguments_as_sent`, `test_internal_execute_records_the_config_version`, `test_create_execution_run_masks_any_action_arguments`); `services/common/tests/test_config_redaction.py` (`test_frr_config_keeps_no_credential_value`, `test_value_under_a_credential_named_key_is_masked_whole`, `test_configuration_without_credentials_is_unchanged`, `test_words_that_only_contain_a_keyword_are_not_keywords`)
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
- **CFG-RUN-3.** A non-admin's list holds only the runs whose device the caller may see
  under device-group visibility, asked once per request of inventory
  `GET /device-groups/visible-devices` with the caller's own token and 10 seconds,
  after the ownership check of CFG-RUN-1. The filter is part of the query, so `total`
  and paging count only those runs; a wiring consumer's run on a switch outside the
  caller's visibility (WIRE-DRIVER-5), or a recipe run filed under a hypervisor id,
  is not listed. No token, a transport error, a non-200, a body that is not JSON, or a
  body that is not `{"device_ids": [<str>, ...]}` is 503
  `Could not verify device visibility; nothing was returned. Retry the request.` with
  no rows. An admin's list is not filtered and asks nothing. \
  Enforced in: `services/execution/app/routers/executions.py` (`_authorize_runs_list`, `list_runs`); `services/execution/app/services/device_visibility.py` (`fetch_visible_device_ids`, `resolve_caller_visibility`); `services/execution/app/services/execution_service.py` (`list_execution_runs`) \
  Pinned by: `services/execution/tests/test_run_reads_device_visibility.py` (`test_owner_run_list_holds_only_visible_devices`, `test_owner_run_list_empty_when_no_device_is_visible`, `test_owner_run_list_device_filter_on_hidden_device_is_empty`, `test_owner_run_list_fails_closed_when_visibility_unanswerable`, `test_owner_run_list_ownership_is_still_the_first_gate`, `test_admin_run_list_is_unfiltered_and_asks_nothing`, `test_fetch_visible_device_ids_without_a_token_is_unanswerable`, `test_resolve_caller_visibility_is_none_for_admins`)
- **CFG-RUN-4.** `GET /runs/{id}` is for admins (403
  `Admin or superadmin role required`); an unknown run is 404 `Execution run not found`. \
  Enforced in: `services/execution/app/routers/executions.py` (`get_run`) \
  Pinned by: `services/execution/tests/test_router_direct.py` (`test_get_run_404_when_missing`, `test_get_run_returns_persisted`); `services/execution/tests/test_api_endpoints.py` (`test_get_run_detail`)
- **CFG-RUN-5.** An admin's retry of a `FAILED` or `TIMEOUT` run runs the same action
  again as a new run, with the original run's user, reservation, ports, and
  `method_kwargs` (read back as CFG-RUN-7 says when the stored copy is masked), and
  answers 200 with the new run; any other status is 400
  `Only failed or timed-out runs can be retried`, an unknown run 404. \
  Enforced in: `services/execution/app/routers/executions.py` (`retry_run`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_retry_rejects_successful_run`, `test_retry_failed_run_rebuilds_and_runs`); `services/execution/tests/test_api_endpoints.py` (`test_retry_failed_run`, `test_retry_success_run_rejected`); `services/execution/tests/test_router_direct.py` (`test_retry_run_404_when_missing`)
- **CFG-RUN-6.** Every run records whether it was a dry run as a boolean `dry_run` in
  its `input_params` (False on runs that never dry-run, such as the wiring consumer's),
  and a retry passes the recorded value on, so a retried dry run stays a dry run. A run
  whose `input_params` carries no boolean `dry_run` (one written before the record
  existed) is refused with 409
  `This run does not record whether it was a dry run, so it cannot be retried; start a new run instead`
  before any device read or driver call. \
  Enforced in: `services/execution/app/routers/executions.py` (`retry_run`); `services/execution/app/services/execution_service.py` (`create_execution_run`, `run_driver_action`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_retry_failed_dry_run_stays_a_dry_run`, `test_retry_failed_real_run_stays_real`, `test_retry_refuses_run_without_dry_run_record`, `test_execute_records_dry_run_on_the_run`)
- **CFG-RUN-7.** A retry of a run whose `method_kwargs_redacted` is true reads the
  configuration back from the run's `config_version_id` through inventory
  `GET /devices/{device_id}/config-versions/{version_id}` (the run's device) with the
  retrying admin's own token and 10 seconds, and uses it only when masking it (CFG-EXEC-8)
  gives exactly the stored copy; the new run records the same `config_version_id`. A run
  with no `config_version_id` is 409
  `This run stores its configuration masked and names no config version to read it back from, so it cannot be retried; start a new run instead`;
  a 404 or a configuration that does not match is 409
  `The configuration this run pushed could not be read back from its config version, so it cannot be retried; start a new run instead`;
  no token, a transport error, another non-200, or a body without a JSON object `config`
  is 503 `Could not read the run's config version; nothing was retried. Retry the request.`
  Each refusal comes after the check of CFG-RUN-6 and before any device read or driver
  call. A run whose arguments needed no masking, or one written before masking (no
  `method_kwargs_redacted`), is retried with its stored arguments. \
  Enforced in: `services/execution/app/routers/executions.py` (`retry_run`, `_recover_masked_kwargs`) \
  Pinned by: `services/execution/tests/test_run_arguments_storage.py` (`test_retry_reads_a_masked_configuration_back_from_its_config_version`, `test_retry_of_a_masked_run_without_a_config_version_is_refused`, `test_retry_refuses_a_version_that_does_not_match_the_run`, `test_retry_fails_closed_when_the_config_version_cannot_be_read`, `test_retry_of_an_unmasked_run_reuses_its_stored_arguments`)

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
  ownership check of CFG-RUN-1 (403 `Reservation not owned by caller`) and whose device
  the caller may see under the visibility answer of CFG-RUN-3; a run on a device
  outside it answers byte for byte as an unknown run (404 `Execution run not found`),
  and an unanswerable lookup is the 503 of CFG-RUN-3. A run with no reservation is 403
  `Admin access required`. \
  Enforced in: `services/execution/app/routers/executions.py` (`list_run_commands`, `_authorize_run_read`); `services/execution/app/services/device_visibility.py` (`resolve_caller_visibility`); `services/execution/app/services/execution_service.py` (`list_command_log`) \
  Pinned by: `services/execution/tests/test_command_log_acl.py` (`test_admin_can_read_any_run`, `test_non_admin_owner_can_read`, `test_non_admin_non_owner_rejected`, `test_non_admin_run_without_reservation_rejected`, `test_reservations_service_error_is_closed_by_default`); `services/execution/tests/test_router_direct.py` (`test_list_run_commands_404_when_run_missing`, `test_list_run_commands_admin_returns_empty`); `services/execution/tests/test_command_log.py` (`test_get_run_commands_admin`, `test_get_run_commands_not_found`); `services/execution/tests/test_run_reads_device_visibility.py` (`test_owner_reads_transcript_of_visible_run`, `test_hidden_run_transcript_answers_exactly_like_an_unknown_run`, `test_transcript_read_fails_closed_when_visibility_unanswerable`, `test_admin_reads_any_transcript_without_a_lookup`)

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
  otherwise, and schedules at the chosen local time when one is set. A refusal the server
  explains reaches the user in words, through the helpers in `frontend/src/lib/errors.ts`:
  the 409 `driver_cannot_configure` on Apply now or Schedule shows its `message` and the
  driver's name, a restore 409 shows its sentence and up to three blocking reservations
  (first 8 characters of the id and the status, then `and N more`), and any plain-string
  `detail` (a 403, a 422, the fail-closed 503) shows as given; anything else shows
  `Apply request failed`, `Schedule failed`, or `Restore failed`. A New version refusal
  shows a string `detail` and never renders a validation list. \
  Enforced in: `frontend/src/components/device-config/DeviceConfigSection.tsx` (`DeviceConfigSection`); `frontend/src/lib/errors.ts` (`configApplyErrorText`, `configRestoreErrorText`, `formatRestoreBlocked`) \
  Pinned by: `frontend/src/test/components/DeviceConfigSection.test.tsx` (`applies now and shows the run id on success`, `shows the stored error for a failed apply answer`, `shows the driver gate's sentence when Apply now is refused with 409`, `shows the server's sentence when Apply now is refused with 403`, `schedules at the chosen time and sends it as ISO`, `shows the driver gate's sentence, not an object, when a schedule is refused`, `lists the blocking reservations when a restore is refused with 409`, `shows the guard's sentence when a restore fails closed with 503`, `shows a create refusal's string detail and never renders a validation list`); `frontend/src/test/lib/errors.test.ts` (`shows at most three ids, then the count of the rest`, `falls back for any other shape, never returning an object`)
- **CFG-UI-5.** The scheduled-applies panel is hidden when the device has no jobs,
  refreshes every 10 seconds, shows each job's status and error, and offers Cancel only
  on a `pending` job; a refused cancel shows the server's string `detail`, else
  `Cancel failed`. \
  Enforced in: `frontend/src/components/device-config/ApplyJobsPanel.tsx` (`ApplyJobsPanel`); `frontend/src/api/deviceConfigJobs.ts` (`useApplyJobs`) \
  Pinned by: `frontend/src/test/components/ApplyJobsPanel.test.tsx` (`renders nothing when the device has no scheduled applies`, `shows each job's status, error, author, and run, with Cancel only on pending`, `cancels a pending job through the API and confirms with a toast`, `shows the server's sentence when a cancel loses the race (409)`, `never toasts an object detail; a non-string refusal falls back`, `refreshes the job list every 10 seconds`)
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
- **CFG-LOAD-3.** The archive is extracted into a directory of the load's own,
  `DRIVER_CACHE_PATH/<driver id>-<random hex>`, never shared with another load: a
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
  are written to the driver's cache row. With no row, the row is inserted with
  `ON CONFLICT (driver_id) DO NOTHING` and read back. A row for another SHA256, or whose
  directory is gone, is updated in place to this load's directory and the replaced
  directory is removed. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`load_driver`, `_record_cache_row`) \
  Pinned by: `services/execution/tests/test_driver_loader_load.py` (`test_load_driver_download_and_cache`, `test_load_driver_updates_existing_cache`, `test_load_driver_updates_existing_row_under_concurrent_insert`, `test_load_driver_replaces_row_of_another_package_and_removes_its_directory`)
- **CFG-LOAD-6.** Concurrent first loads of one driver both succeed: each extracts into
  its own directory (CFG-LOAD-3), the insert that loses the unique driver id does
  nothing, and the loser, finding the winner's row for the same SHA256 with its
  directory present, returns the winner's directory and removes its own. \
  Enforced in: `services/execution/app/services/driver_loader.py` (`load_driver`, `_record_cache_row`); `services/execution/app/models/driver_cache.py` (`DriverCache`) \
  Pinned by: `services/execution/tests/test_driver_loader_load.py` (`test_concurrent_first_loads_of_one_driver_both_succeed`, `test_load_driver_adopts_row_written_after_its_own_read`)

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
  plus, when there are keyword arguments, the path of a second temporary JSON file that
  holds them; no argument text is on the child's command line. Both files are written
  with owner-only permissions and deleted when the call ends. \
  Enforced in: `services/execution/app/services/driver_sandbox.py` (`execute_driver_method`); `services/execution/app/services/_runner.py` (`main`) \
  Pinned by: `services/execution/tests/test_driver_sandbox.py` (`test_execute_login`, `test_execute_context_passed`, `test_execute_connect_ports_via_method_kwargs`); `services/execution/tests/test_runner.py` (`test_runner_login_action`, `test_runner_connect_ports_with_args`, `test_runner_invalid_context_file`); `services/execution/tests/test_run_arguments_storage.py` (`test_driver_arguments_never_on_the_child_command_line`, `test_driver_call_without_arguments_passes_no_arguments_file`)
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
| 403 | `manage grant required on this device for an immediate apply (a reservation owner can schedule the apply instead)` | a non-admin immediate apply without an explicit `manage` grant | CFG-APPLY-5 |
| 403 | `Not authorized to cancel this job` | a cancel by someone other than the creator or an admin | CFG-JOB-10 |
| 403 | `Admin access required` | a non-admin `POST /execute` of another action; a non-admin `GET /runs` without `reservation_id`; a transcript of a run with no reservation | CFG-EXEC-1, CFG-RUN-1, CFG-TX-5 |
| 403 | `Admin access or device manage grant required` | a non-admin `configure` without a `manage` grant | CFG-EXEC-1 |
| 403 | `Reservation not owned by caller` | a run list or transcript for a reservation the caller does not own | CFG-RUN-1, CFG-TX-5 |
| 403 | `Admin or superadmin role required` | a non-admin run detail or retry | CFG-RUN-4, CFG-RUN-5 |
| 403 | `Invalid internal token` | an internal route with a wrong token | CFG-VER-14, CFG-EXEC-6, CFG-VAL-1 |
| 404 | `Device not found` | an unknown device, or a hidden one for a non-admin read | CFG-AUTH-5, CFG-AUTH-8, CFG-VER-14, CFG-JOB-8 |
| 404 | `Config version not found` | an unknown version, or one of another device | CFG-AUTH-8, CFG-VER-7, CFG-VER-8 |
| 404 | `No config versions for device` | the internal latest read of a device with no versions | CFG-VER-14 |
| 404 | `Apply job not found` | an unknown job, or a job of a hidden device on the read | CFG-AUTH-5, CFG-JOB-9, CFG-JOB-10, CFG-JOB-11 |
| 404 | `Driver package not found` | the schema proxy for an unknown driver | CFG-SCHEMA-9 |
| 404 | `Device <id> not found`, `Template <id> not found` | execution cannot find the device or its template | CFG-EXEC-4 |
| 404 | `Execution run not found` | an unknown run, or, on a non-admin transcript read, a run on a device the caller may not see | CFG-RUN-4, CFG-RUN-5, CFG-TX-5 |
| 400 | `Only failed or timed-out runs can be retried` | a retry of a run that is not `FAILED` or `TIMEOUT` | CFG-RUN-5 |
| 409 | `{"error": "driver_cannot_configure", "connection_type", "driver", "message"}` | a push to a device whose driver cannot configure | CFG-GATE-2, CFG-GATE-4 |
| 409 | `{"error": "device_has_no_driver", "message"}` | any execution action on a device with no driver | CFG-GATE-4 |
| 409 | `{"message": "Device has active reservations; restore blocked", "reservations": [...]}` | a restore while another user's reservation holds the device | CFG-VER-12 |
| 409 | `Job is '<status>', not cancellable` | a cancel of a job that is not `pending`, or one the scheduler claimed first | CFG-JOB-10, CFG-STATE-4 |
| 409 | `Could not allocate a config version number under concurrent writes; retry the request` | a create or restore that collided five times | CFG-VER-5 |
| 409 | `Source job is not a dry-run; nothing to promote`, `Source dry-run is '<status>'; only successful dry-runs can be promoted` | a confirm of the wrong kind of job | CFG-JOB-11 |
| 409 | `This run does not record whether it was a dry run, so it cannot be retried; start a new run instead` | a retry of a run written before runs recorded `dry_run` | CFG-RUN-6 |
| 409 | `This run stores its configuration masked and names no config version to read it back from, so it cannot be retried; start a new run instead` | a retry of a masked run with no `config_version_id` | CFG-RUN-7 |
| 409 | `The configuration this run pushed could not be read back from its config version, so it cannot be retried; start a new run instead` | a retry whose config version is unknown or no longer matches | CFG-RUN-7 |
| 422 | `Device has no driver-defined connection_type; cannot validate config` | a version for a device without a driver connection type | CFG-VER-2 |
| 422 | `device '<name>': config failed schema validation: <message>` and the other validator messages | a config the schema refuses | CFG-VER-3, CFG-SCHEMA-3, CFG-SCHEMA-4, CFG-EXEC-10 |
| 422 | `scheduled_for must be in the future`, `scheduled_for must be within <N> days from now` | a bad schedule time | CFG-JOB-1, CFG-JOB-2 |
| 422 | `reservation_id must reference an active reservation you own that includes this device`, `reservation_id must reference an active reservation that includes this device` (an admin) | a schedule naming a reservation that fails CFG-JOB-4 or CFG-JOB-5 | CFG-JOB-4, CFG-JOB-5 |
| 422 | `reservation_id must reference a reservation you own that includes this device`, `reservation_id must reference a reservation that includes this device` (an admin) | a `POST /execute` naming a reservation that fails CFG-EXEC-3 | CFG-EXEC-3 |
| 422 | `this driver does not advertise dry-run support; refuse to fire a dry-run that would hit the wire` | a dry-run schedule for a driver without dry-run support | CFG-JOB-6 |
| 422 | `internal execute is restricted to action='configure'; got '<action>'` | `POST /execute/internal` of another action | CFG-EXEC-6 |
| 422 | `Only the Hypervisor connection type is supported for package validation`; `package_b64 is not valid base64`; `package exceeds the <N> byte validation limit`; `package is empty` | a validation request the route refuses | CFG-VAL-1, CFG-VAL-2 |
| 500 | `Internal API token not configured` | an execution internal route when execution has no token | CFG-EXEC-6, CFG-VAL-1 |
| 503 | `Could not verify the reservation; nothing was scheduled. Retry the request.` | a schedule whose reservation cannot be checked | CFG-JOB-4 |
| 503 | `reservations service unreachable while checking active reservations` and the two sibling details | a restore whose guard cannot be answered | CFG-VER-13 |
| 503 | `Failed to fetch device: <reason>`, `Failed to fetch template: <reason>` (an upstream status, a class name, or a malformed-body note; never upstream text) | execution cannot read the device or template | CFG-EXEC-4 |
| 503 | the visibility lookup's own detail (`inventory.md`) | a non-admin read whose visibility lookup fails | CFG-AUTH-5 |
| 503 | `Could not verify the reservation; nothing was run. Retry the request.` | a `POST /execute` whose reservation cannot be checked | CFG-EXEC-3 |
| 503 | `Could not read the run's config version; nothing was retried. Retry the request.` | a retry whose config version cannot be read | CFG-RUN-7 |
| 503 | `Could not verify device visibility; nothing was returned. Retry the request.` | a non-admin run list or transcript read whose visibility lookup cannot be answered | CFG-RUN-3, CFG-TX-5 |

## 10. Interactions with other services

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| inventory to acl | acl | `POST /check` with the caller's token, 5 s | explicit `manage` for a write | fail closed: counts as no grant, the reservation check still runs (CFG-AUTH-4) |
| inventory to acl | acl | `POST /internal/check` (`X-Internal-Token`, 5 s) | fire-time `manage` | fail closed: counts as no grant (CFG-AUTH-7) |
| inventory to reservations | reservations | `GET /internal/active?user_id&device_id` (`X-Internal-Token`, 5 s) | active-reservation ownership, at request and fire time | fail closed: counts as not owner (CFG-AUTH-4, CFG-AUTH-7) |
| inventory to reservations | reservations | `GET /internal/{id}` (`X-Internal-Token`, 5 s) | a schedule's and a fire's reservation status | schedule: 503 (CFG-JOB-4); fire: the job is skipped (CFG-SCHED-5) |
| inventory to reservations | reservations | `GET /internal/by-device/{id}` (`X-Internal-Token`, 5 s) | the restore guard; a schedule's `reservation_id` | fail closed: 503 (CFG-VER-13, CFG-JOB-4) |
| inventory to execution | execution | `GET /drivers/{id}/config-schema` (`X-Internal-Token`, 10 s) | the published schema | fail open: the registry applies (CFG-SCHEMA-7) |
| inventory to execution | execution | `POST /execute` with the caller's token, 30 s | immediate apply | 200 with `status` `failed` and the error (CFG-APPLY-2) |
| inventory to execution | execution | `POST /execute/internal` (`X-Internal-Token`, 30 s) | a scheduled job | the job is `failed` (CFG-SCHED-9) |
| execution to acl | acl | `POST /check` with the caller's token, 5 s | a non-admin `configure` | fail closed: 403 (CFG-EXEC-1) |
| execution to reservations | reservations | `GET /{id}` with the caller's token, 5 s | run list and transcript ownership | fail closed: 403 (CFG-RUN-1, CFG-TX-5) |
| execution to reservations | reservations | `GET /internal/by-device/{id}` (`X-Internal-Token`, 5 s) | a `POST /execute` that names a reservation | fail closed: 503 (CFG-EXEC-3) |
| execution to inventory | inventory | `GET /devices/{id}/config-versions/{vid}` with the admin's token, 10 s | a retry of a masked run | 404 is 409, anything else unreadable is 503 (CFG-RUN-7) |
| execution to inventory | inventory | `GET /device-groups/visible-devices?user_id` with the caller's token, 10 s | a non-admin's run list and transcript read | fail closed: 503 (CFG-RUN-3, CFG-TX-5) |
| execution to inventory | inventory | `GET /devices/{id}/internal`, `GET /templates/{id}/internal` (`X-Internal-Token`, 10 s) | the device and template of an action | 404 is relayed; anything else, a body that is not a JSON object included, 503 (CFG-EXEC-4) |
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
| Unit | `services/common/tests/test_device_config.py`, `services/common/tests/test_acl.py`; `services/inventory/tests/test_published_schema.py`, `test_apply_scheduler.py`; `services/execution/tests/test_driver_loader*.py`, `test_driver_sandbox*.py`, `test_runner.py`, `test_sandbox_isolation.py`, `test_dry_run.py`, `test_driver_transcript.py`, `test_config_schema_extraction.py`, `test_configure_capability_parity.py`, `test_package_validator.py`; frontend `frontend/src/test/components/DeviceConfigSection.test.tsx`, `ApplyJobsPanel.test.tsx`, `AIApplyConfirmModal.test.tsx`, `frontend/src/test/lib/errors.test.ts`, `frontend/src/test/api/deviceConfig.test.tsx`, `deviceConfigJobs.test.tsx` | SQLite in memory; the sandbox suites start real child processes |
| Functional (through the service API) | `services/inventory/tests/test_device_configs.py`, `test_device_configs_rbac.py`, `test_apply_jobs_reservation_owner.py`, `test_apply_jobs_reservation_scope.py`, `test_device_config_write_rules.py`, `test_dry_run_gate.py`, `test_confirm_dry_run.py`, `test_configure_capability_gate.py`, `test_device_config_restore_reservation_guard.py`, `test_device_read_visibility_gate.py`, `test_apply_jobs_internal_summary.py`, `test_router_edge_cases.py`; `services/execution/tests/test_router_endpoints.py`, `test_router_direct.py`, `test_api_endpoints.py`, `test_command_log*.py`, `test_config_schema_endpoint.py`, `test_configure_capability_gate.py`, `test_execution_service_edges.py`, `test_manual_execute_reservation_scope.py` | acl, reservations, and execution are patched |
| Integration (running stack) | `tests/integration/test_config_apply_flow.py` (a config version validated through the schema proxy, a scheduled dry run fired by the scheduler on `drivers/frr_mgmt`, its transcript, the confirm, and the schedule's reservation check), `test_execution_configure_gate.py`, `test_execution_result_gating.py`, `test_execution_device_scope.py`, `test_package_validation.py`; the NOS lab tiers under `tests/nos_lab/` (`test_frr_mgmt_driver_live.py` drives the Management driver's `configure`) | The immediate apply and a real (not dry-run) scheduled push are not driven against a stack; the dev and gate stacks run the scheduler every 2 seconds (`docker-compose.override.yml`) |
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

None recorded.

### Limits by decision

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
- A retry accepts a config version whose masked copy equals the run's stored copy, so
  two configurations that differ only inside masked text compare equal; config versions
  are never edited in place, so a version id names one configuration (the
  `_recover_masked_kwargs` docstring) (CFG-RUN-7). A run that a direct `POST /execute`
  started with masked arguments and no `config_version_id` cannot be retried; a new run
  is the way (CFG-RUN-7).

### Rules with no test

- CFG-DRY-4: the dry-run declaration is not verified.
- CFG-SBX-9: no isolation beyond resource limits.
