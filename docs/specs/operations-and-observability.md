# Operations and observability specification

| | |
|---|---|
| Area prefix | `OPS` (used in rule identifiers, for example `OPS-OUTBOX-1`) |
| Last full review | 2026-10-08 |
| Owning services | common (`services/common/herd_common/`: logging, settings sources, schema lifecycle, outbox, JetStream helpers, version, the request-validation handler), config (`services/config/`) and its restarter (`infra/restarter/`), secrets (`services/secrets/`), user-profile (`services/user-profile/`), execution (the device health scheduler and its read routes), reservations (the utilization report routes); the About, Config, and Reporting pages in `frontend/` |
| Other services involved | every backend service (each serves `/health` and `/version`, logs through the shared formatter, and resolves settings through the shared sources), inventory (the poll registry route, the secret reference lookup), auth (user groups for the report), cabling (transit devices for the report), acl (secret grants), notifications and integration (consumers of health events), NATS JetStream, Postgres, Docker (reached only by the restarter, for the config service's restart) |
| Design records | [ADR 0003](../design/0003-encrypted-credential-store.md), [ADR 0013](../design/0013-lab-purpose-classification.md), [ADR 0017](../design/0017-config-restart-without-the-docker-socket.md) |
| Related guides | [OPERATIONS.md](../OPERATIONS.md), [TROUBLESHOOTING.md](../TROUBLESHOOTING.md), [ENV_VARS.md](../ENV_VARS.md), [LOAD_TESTING.md](../LOAD_TESTING.md), [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md), [ARCHITECTURE.md](../ARCHITECTURE.md), [ROLES.md](../ROLES.md), [FRESH_SETUP.md](../../FRESH_SETUP.md) |

Paths in the route tables are each service's own paths. Through the gateway they are
prefixed with `/api/<service>`, except ai-orchestrator (`/api/ai`) and integration
(`/api/v1`).

This document specifies the machinery every area shares and the operator-facing
surfaces. What a consumer does with an event lives in the consumer's area: the
execution consumer (heartbeat use, NAK schedule use, dead-letter subjects, the
corroboration gate) is `provisioning-and-wiring.md` (WIRE-CONSUME-1 to WIRE-CONSUME-15,
WIRE-GATE-1 to WIRE-GATE-5); the reservations producer and what it stages is
`reservations.md` (RES-EVENT-1 to RES-EVENT-4); notifications and webhook delivery are
`integration.md`. Inventory's poll interval, its floor, and the registry route it serves
to the scheduler are `inventory.md` (INV-POLL-1 to INV-POLL-4, INV-INT-1). Each
consumer's dead-letter queue and the `HERD_DLQ` stream are WIRE-CONSUME-13. The AI
provider status route is `ai-features.md`. The driver sandbox, driver runs, and the
`/runs` routes are `device-configuration.md`.

## 1. Purpose

An operator running HERD needs to know which build each service is on, whether each
one is alive, what it logged, and whether its events reached the bus; they need to set
configuration once and have every service pick it up; they need upgrades that do not
corrupt a schema; and they need a credential store, device health polling, and usage
reports. This area provides that shared machinery: liveness and version routes, JSON
logs with key-name redaction, the settings precedence and the first-run config service,
the schema bootstrap and drift warnings, the transactional outbox and the JetStream
helpers, the health scheduler, the encrypted secret store, per-user preferences, and the
utilization report. It does not alert by itself (notifications does, `integration.md`),
ship metrics, or run a monitoring stack.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). How a token is verified is
`identity-and-access.md`; every route here reads the role from the JWT claim. Rules
beyond role (secret grants, own-row preferences) are numbered in section 8.

| Actor | May | May not |
|---|---|---|
| Unauthenticated caller | Read `/health` and `/version` on every service; read the config service's status and field schema; log in to the config service with its password | Anything else |
| Config operator (config-page password, not a HERD account) | Read the merged settings (secret values masked), change the config password, save settings and restart this compose project's services that read `config.json` once the password is rotated (OPS-CONFIG-9) | Save or restart before rotating a generated password; restart services of another compose project or outside the restart set, or pass anything to the restart (OPS-CONFIG-13, OPS-CONFIG-14) |
| User | Read the health snapshot of a device they can see (OPS-HEALTH-1); read, replace, merge, and reset their own preferences; list and read the secrets they hold a `view` or `manage` grant on, and reveal those they hold `manage` on | List all health snapshots; read reports; create, edit, delete, or rotate secrets; read another user's preferences |
| Admin | Everything a user may; list health snapshots; read the utilization report and its CSV; create, list, read, reveal, edit, and delete every secret; rotate the data-encryption key; open the About page | Delete a secret a hypervisor references (OPS-SECRET-13) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Read a secret's plaintext by id or name; read a user's preferences; run an on-demand device check (section 7) | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Build identifier | `HERD_BUILD` (`git describe --tags --always --dirty`, else `dev`) and `HERD_BUILD_DATE` (HEAD commit date, UTC), stamped into every image at build time | the Makefile, each image | image environment |
| Service version | The installed distribution version from the service's `pyproject.toml` (`frontend/package.json` for the frontend) | each service | package metadata |
| Log line | One JSON object per record: `timestamp`, `level`, `service`, `logger`, `message`, optional `exception`, plus every extra | `herd_common.logging` | stdout |
| Config file | `config.json` written by the config service and read by every other service as a settings source | config | the `herd-config` volume (`/data/herd-config` in config, `/etc/herd` read-only elsewhere) |
| Bootstrap marker | `config.bootstrapped` beside `config.json`: present means the file is a copy of the environment and ranks below it | config | the `herd-config` volume |
| Config auth file | The bcrypt hash of the config-page password and whether it was rotated | config | `config_auth.json` on the `herd-config` volume |
| Alembic stamp | The `alembic_version` row in a service schema; its presence decides whether boot may create tables | each service with a database | each schema |
| Outbox row | An event staged in the producer's transaction: `id` (also the payload `event_id`), `subject`, `payload`, `created_at`, `published_at`, `attempts` | reservations, execution | each producer's `outbox` table (`OutboxMixin`) |
| Stream | A JetStream stream: `HERD_RESERVATIONS` (owner reservations), `HERD_HEALTH` (owner execution), `HERD_DLQ` (owner execution) | NATS | the NATS store (`nats-data` volume under `make prod`) |
| Health status row | One per polled device: `last_status`, `last_polled_at`, `last_run_id`, `consecutive_failures`, `next_poll_at`, `poll_tier`. `device_id` is a bare inventory id | execution | `device_health_status` (`DeviceHealthStatus` in `services/execution/app/models/device_health_status.py`) |
| Poll registry | The device id to resolved interval map read from inventory, cached in the execution process | execution (cache), inventory (source) | process memory |
| Secret | A named credential: metadata plus a JSON object of string values encrypted with AES-GCM under a data-encryption key, the secret id and key version bound as associated data | secrets | `secrets` (`Secret` in `services/secrets/app/models/secret.py`) |
| Key version | A data-encryption key wrapped under the environment key-encryption key (`SECRETS_KEK`); `retired_at` marks a superseded one | secrets | `key_versions` (`KeyVersion`) |
| Preferences | Per-user `saved_filters`, `page_sizes`, and `extras` objects | user-profile | `user_preferences` (`UserPreferences`), keyed by the bare user id |
| Utilization report | Reservation hours over a window, bucketed by user, device, topology type, day, group, purpose, and fleet | reservations (computed, not stored) | none |

## 4. State model

This section covers the two lifecycles this area owns. Reservation statuses are
`reservations.md`'s.

**Statuses.**

Outbox row:

- unpublished: `published_at` is null; the relay will try it.
- published: JetStream acknowledged the publish; `published_at` is set. The row is
  deleted once older than the retention window.

Health status row (`last_status`):

- `UNKNOWN`: seeded, not yet polled.
- `HEALTHY`: the last poll's `login` and `status` succeeded.
- `DEGRADED`: `login` succeeded and `status` did not.
- `UNREACHABLE`: the device or template could not be read, or `login` failed.

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | outbox unpublished | the producer's own transaction (`enqueue_event`) | none; commits with the state change | the event itself | OPS-OUTBOX-1 |
| outbox unpublished | outbox published | `run_outbox_relay` (`_publish_pending`) | row unclaimed (`FOR UPDATE SKIP LOCKED`); JetStream acknowledged | nothing | OPS-OUTBOX-4, OPS-OUTBOX-5 |
| outbox published | (deleted) | `run_outbox_relay` (`prune_published`) | `published_at` older than the retention window | nothing | OPS-OUTBOX-8 |
| (none) | health `UNKNOWN` | scheduler registry refresh (`_seed_missing_status_rows`) or a tier transition (`apply_tier_transition`) | no row for the device | nothing | OPS-POLL-3, OPS-TIER-3 |
| any health status | `HEALTHY`, `DEGRADED`, or `UNREACHABLE` | `fire_poll` | the scheduler won the row's claim | `herd.health.status_changed` when the failure count crosses the threshold or recovers | OPS-POLL-7, OPS-POLL-8, OPS-POLL-11 |

**Concurrency.** Outbox rows are claimed one at a time with `FOR UPDATE SKIP LOCKED`, so
several relay instances never publish the same row (OPS-OUTBOX-4). A health row is
claimed by a conditional `UPDATE` that pushes `next_poll_at` five minutes ahead only
while it is still due, so concurrent schedulers and replicas poll disjoint rows
(OPS-POLL-5). Tier writes are absolute updates and are idempotent on redelivery
(OPS-TIER-4).

**Rules.** The state rules are in sections 8.8 and 8.10.

## 5. API surface

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| GET | `/health` (every service) | anyone | 200 | OPS-LIVE-1, OPS-LIVE-2 |
| GET | `/version` (every service) | anyone | 200 | OPS-VER-1 to OPS-VER-6 |
| GET | `/device-health/{device_id}` (execution) | any signed-in user (non-admins: devices they can see) | 200 | OPS-HEALTH-1, OPS-HEALTH-2 |
| GET | `/device-health` (execution) | admin | 200 | OPS-HEALTH-3, OPS-HEALTH-4 |
| GET | `/status` (config) | anyone | 200 | OPS-CONFIG-4 |
| POST | `/login` (config) | anyone with the config password, within the attempt limits | 200 | OPS-CONFIG-6, OPS-CONFIG-7, OPS-CONFIG-22 |
| POST | `/change-password` (config) | config session | 200 | OPS-CONFIG-8 |
| GET | `/schema` (config) | anyone | 200 | OPS-CONFIG-10 |
| GET | `/settings` (config) | config session | 200 | OPS-CONFIG-10 |
| PUT | `/settings` (config) | config session, password rotated | 200 | OPS-CONFIG-9, OPS-CONFIG-11, OPS-CONFIG-12, OPS-CONFIG-23, OPS-CONFIG-31 |
| POST | `/apply` (config) | config session, password rotated | 200 | OPS-CONFIG-9, OPS-CONFIG-13 to OPS-CONFIG-16, OPS-CONFIG-24, OPS-CONFIG-32 |
| GET | `/apply/last` (config) | config session | 200 | OPS-CONFIG-32 |
| POST | `/secrets` (secrets) | admin | 201 | OPS-SECRET-1, OPS-SECRET-2 |
| GET | `/secrets` (secrets) | any signed-in user | 200 | OPS-SECRET-5 |
| GET | `/secrets/{secret_id}` (secrets) | admin, or a `view` or `manage` grant | 200 | OPS-SECRET-6 |
| GET | `/secrets/{secret_id}/value` (secrets) | admin, or a `manage` grant | 200 | OPS-SECRET-7, OPS-SECRET-8, OPS-SECRET-19 |
| PUT | `/secrets/{secret_id}` (secrets) | admin | 200 | OPS-SECRET-11 |
| DELETE | `/secrets/{secret_id}` (secrets) | admin | 204 | OPS-SECRET-12 to OPS-SECRET-14 |
| POST | `/keys/rotate` (secrets) | admin | 200 | OPS-SECRET-17, OPS-SECRET-18 |
| GET | `/preferences` (user-profile) | any signed-in user, own row | 200 | OPS-PREF-1 |
| PUT | `/preferences` (user-profile) | any signed-in user, own row | 200 | OPS-PREF-2, OPS-PREF-4 |
| PATCH | `/preferences` (user-profile) | any signed-in user, own row | 200 | OPS-PREF-3, OPS-PREF-4, OPS-PREF-5 |
| DELETE | `/preferences` (user-profile) | any signed-in user, own row | 200 | OPS-PREF-6 |
| GET | `/reports/utilization` (reservations) | admin | 200 | OPS-REPORT-1 to OPS-REPORT-12 |
| GET | `/reports/utilization.csv` (reservations) | admin | 200 | OPS-REPORT-1, OPS-REPORT-13 to OPS-REPORT-15 |

## 6. Events

| Subject | Producer | Staged when | Consumers | Payload keys | Rules |
|---|---|---|---|---|---|
| `herd.health.status_changed` | execution health scheduler, through its outbox | a poll moves `consecutive_failures` from below the threshold to at or above it (`bad_news`), or from above zero to zero (`recovery`), while `HEALTH_POLL_NOTIFY_ENABLED` is on | notifications and integration (`integration.md`) | `event` (`device.health_transition`), `device_id`, `device_name`, `old_status`, `new_status`, `transition_kind`, `consecutive_failures`, `last_run_id`, `timestamp`, `event_id` | OPS-POLL-11, OPS-POLL-12, OPS-OUTBOX-2 |

Every outbox-published event, on any subject, carries the `Nats-Msg-Id` header and the
payload `event_id` (OPS-OUTBOX-2, OPS-OUTBOX-5).

### Events consumed

| Subject | Published by | Consumer | What this area does | Rules |
|---|---|---|---|---|
| `herd.reservations.created` | reservations | execution | moves the reservation's devices to the `in_use` poll tier | OPS-TIER-1, OPS-TIER-2 |
| `herd.reservations.updated` | reservations | execution | moves `device_ids` to `in_use` and `removed_device_ids` to `idle` | OPS-TIER-1 |
| `herd.reservations.completed`, `cancelled`, `failed` | reservations | execution | moves the reservation's devices to `idle` | OPS-TIER-1 |

The tier update runs only for events the corroboration gate accepted, and its failure
never fails the message (`provisioning-and-wiring.md`, WIRE-GATE-1, WIRE-DISPATCH-5).

## 7. Internal API

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| GET | `/internal/secrets/{secret_id}/value` (secrets) | `X-Internal-Token` | execution (hypervisor credentials, `dynamic-resources.md`), inventory (hypervisor registration, `inventory.md`) | `{id, name, data}` | OPS-SECRET-9, OPS-SECRET-10, OPS-SECRET-19 |
| GET | `/internal/secrets/by-name/{name}/value` (secrets) | `X-Internal-Token` | none in the services today | `{id, name, data}` | OPS-SECRET-9, OPS-SECRET-10, OPS-SECRET-19 |
| GET | `/preferences/internal?user_id` (user-profile) | `X-Internal-Token` | notifications (channel and event opt-outs) | the user's preferences, created empty if missing | OPS-PREF-7 |
| POST | `/device-check` (execution) | `X-Internal-Token` | none in the services today | `{run_id, device_id, status, output, error}` | OPS-HEALTH-6 to OPS-HEALTH-10 |
| POST | `/restart` (restarter) | none; reachable only on the internal `herd-ctl` network | config (`POST /apply`) | `{restarted, errors}`, or 409 | OPS-CONFIG-13, OPS-CONFIG-25 to OPS-CONFIG-28 |
| GET | `/health` (restarter) | none; reachable only on `herd-ctl` | the compose healthcheck | `{status, service}` | OPS-CONFIG-25 |

This area calls inventory's `GET /devices/health-config` (`inventory.md`, INV-POLL-3) and
`GET /hypervisors/by-secret/{id}/internal` (`inventory.md`); their answers are specified
there.

## 8. Features

### 8.1 Liveness and version routes

**What it does.** Every service answers two unauthenticated questions: is the process
up, and which version and build is it running.

**Surfaces.** `GET /health` and `GET /version` on all twelve services (section 5); the
shared helper `services/common/herd_common/version.py` and the config service's own copy
`services/config/app/version.py`.

**Rules.**

- **OPS-LIVE-1.** `GET /health` answers 200 `{"status": "ok", "service": "<name>"}` with
  no authentication and checks nothing else: no database, broker, or peer is consulted,
  so it reports only that the process serves HTTP. \
  Enforced in: `services/auth/app/main.py` (`health`); `services/reservations/app/main.py` (`health`); `services/execution/app/main.py` (`health`); `services/config/app/main.py` (`health`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_health_endpoint`); `services/reservations/tests/test_reservations.py` (`test_health_endpoint`); `services/execution/tests/test_executions.py` (`test_health`); `services/config/tests/test_config.py` (`test_health`)
- **OPS-LIVE-2.** An execution replica started with `EXECUTION_POLLER_ONLY=true` mounts
  no API router but still answers `/health` and `/version`. \
  Enforced in: `services/execution/app/main.py` (`mount_api_routers`, `health`, `add_version_route`) \
  Pinned by: `services/execution/tests/test_version.py` (`test_version_route_not_part_of_mount_api_routers`); `services/execution/tests/test_health_scheduler_scale.py` (`test_mount_api_routers_poller_only_mounts_nothing`)
- **OPS-VER-1.** `GET /version` answers exactly four keys, `service`, `version`,
  `build`, and `build_date`, with no authentication. \
  Enforced in: `services/common/herd_common/version.py` (`add_version_route`, `version_payload`, `VersionResponse`) \
  Pinned by: `services/common/tests/test_version.py` (`test_route_returns_exactly_four_keys`, `test_version_payload_shape`)
- **OPS-VER-2.** `version` is the service's installed distribution version, and the
  service's OpenAPI `info.version` is the same value; an unresolvable distribution
  reports `0+unknown` instead of failing the boot. \
  Enforced in: `services/common/herd_common/version.py` (`service_version`) \
  Pinned by: `services/common/tests/test_version.py` (`test_service_version_resolves_real_distribution`, `test_service_version_unknown_distribution_falls_back`); `services/auth/tests/test_version.py` (`test_openapi_info_version_matches_distribution`)
- **OPS-VER-3.** `build` is `HERD_BUILD` and `build_date` is `HERD_BUILD_DATE`, both
  read from the environment on every call; unset or empty reports `dev` and null. \
  Enforced in: `services/common/herd_common/version.py` (`build_info`) \
  Pinned by: `services/common/tests/test_version.py` (`test_build_info_unset`, `test_build_info_set`, `test_build_info_empty_string_counts_as_unset`); `services/acl/tests/test_version.py` (`test_version_build_unset_is_dev`, `test_version_build_set`)
- **OPS-VER-4.** The integration service keeps its FastAPI version at `1.0.0`, the
  published `/api/v1` contract version, and leaves `/version` out of its OpenAPI
  document while still answering it. By decision; issue #846. \
  Enforced in: `services/integration/app/main.py` (`add_version_route`) \
  Pinned by: `services/integration/tests/test_version.py` (`test_version_absent_from_openapi_paths`, `test_facade_contract_version_untouched`, `test_version_answers_200`)
- **OPS-VER-5.** The config service answers the same four keys from a standard-library
  copy of the helper, and nothing under its `app/` imports `herd_common`, because its
  image does not contain that package. By decision; issue #846. \
  Enforced in: `services/config/app/version.py` (`add_version_route`, `service_version`, `VersionResponse`, `DISTRIBUTION`) \
  Pinned by: `services/config/tests/test_version.py` (`test_response_fields_match_the_shared_contract`, `test_config_app_never_imports_herd_common`, `test_version_build_unset_is_dev`)
- **OPS-VER-6.** Every one of the twelve services registers `/version`. \
  Enforced in: `services/common/herd_common/version.py` (`add_version_route`) \
  Pinned by: `services/acl/tests/test_version.py` (`test_version`); `services/auth/tests/test_version.py` (`test_version`); `services/cabling/tests/test_version.py` (`test_version`); `services/ai-orchestrator/tests/test_version.py` (`test_version`); `services/config/tests/test_version.py` (`test_version`); `services/execution/tests/test_version.py` (`test_version`); `services/inventory/tests/test_version.py` (`test_version`); `services/notifications/tests/test_version.py` (`test_version`); `services/reservations/tests/test_version.py` (`test_version`); `services/secrets/tests/test_version.py` (`test_version`); `services/user-profile/tests/test_version.py` (`test_version`); `services/integration/tests/test_version.py` (`test_version_answers_200`)

**Out of scope.** Readiness checks: no route reports whether a dependency is reachable.
The AI provider status route (`ai-features.md`).

### 8.2 Build identifier stamping

**What it does.** Every image built through the Makefile carries the git-derived build
string and the commit date, so a partly rebuilt stack can be told apart from a fresh one.

**Surfaces.** The `HERD_BUILD` block and `make version` in `Makefile`; the build args in
`docker-compose.yml`; every `Dockerfile`; `frontend/vite.config.ts`.

**Rules.**

- **OPS-BUILD-1.** The Makefile computes `HERD_BUILD` as `git describe --tags --always
  --dirty` (`dev` when git gives nothing) and `HERD_BUILD_DATE` as the HEAD commit date
  in UTC, not the wall clock, and exports both; a value already in the environment wins
  and git is not called. \
  Enforced in: `Makefile` (`HERD_BUILD`, `HERD_BUILD_DATE`) \
  Pinned by: none
- **OPS-BUILD-2.** Every compose build stanza passes the two values as build args, with
  `dev` and empty as the defaults; the frontend receives them as `VITE_HERD_BUILD` and
  `VITE_HERD_BUILD_DATE`. \
  Enforced in: `docker-compose.yml` (`HERD_BUILD`, `VITE_HERD_BUILD`) \
  Pinned by: `tests/unit/test_build_args_wiring.py` (`test_every_build_service_is_covered_by_this_test`, `test_every_compose_build_stanza_carries_the_build_args`)
- **OPS-BUILD-3.** Every service Dockerfile declares each of the two args once, in the
  stage that ships, with no `RUN`, `COPY`, or `ADD` after them (so every dependency
  install is ahead of them, whatever it is called), and exports them in one `ENV`
  instruction after both declarations, so a new build string rebuilds only that layer.
  A `RUN` that would need the value after the `ARG` is refused by the guard: the `ENV`
  line already puts the value in the image. \
  Enforced in: `services/auth/Dockerfile` (`HERD_BUILD`) \
  Pinned by: `tests/unit/test_build_args_wiring.py` (`test_every_service_dockerfile_declares_the_build_args_and_env`)
- **OPS-BUILD-4.** The frontend Dockerfile declares its two args, and exports them
  after both, in the stage that runs `npm run build`, before the build and after that
  stage's `npm ci` or `npm install` when it has one, and Vite injects the package
  version, the build, and the build date as compile-time constants (`dev` and empty
  when unset). \
  Enforced in: `frontend/Dockerfile` (`VITE_HERD_BUILD`); `frontend/vite.config.ts` (`__APP_VERSION__`, `__APP_BUILD__`, `__APP_BUILD_DATE__`) \
  Pinned by: `tests/unit/test_build_args_wiring.py` (`test_frontend_dockerfile_declares_vite_build_args_before_npm_run_build`)

**Out of scope.** Release tagging and the version bump (CHANGELOG.md and the release
runbook).

### 8.3 Version display in the browser

**What it does.** The login page and the header show the frontend version. An admin's
About page lists the version, build, and build date of the frontend and of every
backend service, and flags a service that runs a different release or build.

**Surfaces.** `frontend/src/lib/appVersion.ts`, `frontend/src/api/about.ts`,
`frontend/src/pages/admin/AboutPage.tsx` (route `/admin/about`, behind the admin route
guard in `identity-and-access.md`), `frontend/src/components/layout/AppLayout.tsx`,
`frontend/src/pages/LoginPage.tsx`.

**Rules.**

- **OPS-ABOUT-1.** The login page and the header show the frontend version only, never
  the build string or date. \
  Enforced in: `frontend/src/pages/LoginPage.tsx` (`APP_VERSION`); `frontend/src/components/layout/AppLayout.tsx` (`APP_VERSION`) \
  Pinned by: `frontend/src/test/pages/LoginPage.test.tsx` (`shows the injected app version, and nothing else version-shaped (issue #846)`); `frontend/src/test/components/AppLayout.test.tsx` (`shows the injected app version beside the HERD wordmark (issue #846)`)
- **OPS-ABOUT-2.** `appVersion.ts` is the one reader of the injected constants; the build
  falls back to `dev` and the build date to null. \
  Enforced in: `frontend/src/lib/appVersion.ts` (`APP_VERSION`, `APP_BUILD`, `APP_BUILD_DATE`) \
  Pinned by: `frontend/src/test/lib/appVersion.test.ts` (`APP_VERSION is read from package.json's version field, not a hardcoded literal`, `APP_BUILD falls back to 'dev' when VITE_HERD_BUILD is unset (default vitest env)`, `APP_BUILD_DATE is null when VITE_HERD_BUILD_DATE is unset (default vitest env)`)
- **OPS-ABOUT-3.** Two versions are the same release when their release numbers match
  and their pre-release states match, accepting the PEP 440 and semver spellings of one
  dev or rc pre-release; any unparseable string matches nothing. \
  Enforced in: `frontend/src/lib/appVersion.ts` (`sameRelease`) \
  Pinned by: `frontend/src/test/lib/appVersion.test.ts` (`treats matching PEP 440 and semver dev spellings of the same release as the same`, `does not match a tagged release against the dev build of the same number`, `treats matching PEP 440 and semver rc spellings of the same rc as the same`, `does not match different rc numbers on the same release`, `fails closed on an unparseable version string`, `fails closed on a post-release`)
- **OPS-ABOUT-4.** Two builds differ only when both are real build strings and unequal;
  a `dev` build on either side is never flagged. \
  Enforced in: `frontend/src/lib/appVersion.ts` (`buildsDiffer`) \
  Pinned by: `frontend/src/test/lib/appVersion.test.ts` (`is false for two identical build strings`, `is true for two different real build strings`, `never flags a 'dev' build on either side, even against a different real build`)
- **OPS-ABOUT-5.** A build date renders as UTC with a `UTC` label, a dash when absent,
  and the raw string when it does not parse. \
  Enforced in: `frontend/src/lib/appVersion.ts` (`formatBuildDate`) \
  Pinned by: `frontend/src/test/lib/appVersion.test.ts` (`renders UTC with a label, matching what make version prints on the host`, `renders a dash for a missing date`, `returns an unparseable string unchanged`)
- **OPS-ABOUT-6.** The About page asks each of the twelve services separately, with no
  retry, so one service that does not answer shows `unreachable` in its own row while
  the others render. \
  Enforced in: `frontend/src/api/about.ts` (`SERVICES`, `useServiceVersions`) \
  Pinned by: `frontend/src/test/pages/AboutPage.test.tsx` (`renders all 12 services once every /version call answers`, `shows 'unreachable' for one failing service while the other 11 still render`)
- **OPS-ABOUT-7.** A 200 whose body is not the four-key version shape is `invalid
  response`, distinct from `unreachable`, and is never compared. \
  Enforced in: `frontend/src/api/about.ts` (`fetchServiceVersion`, `InvalidVersionResponseError`) \
  Pinned by: `frontend/src/test/api/about.test.ts` (`throws InvalidVersionResponseError for an HTML string body (proxy error page served with 200)`, `throws InvalidVersionResponseError for a body missing version`); `frontend/src/test/pages/AboutPage.test.tsx` (`renders 'invalid response' with dashed cells and no 'differs' for a malformed 200 body (issue #874)`, `still renders 'unreachable' (not 'invalid response') for an ordinary transport/HTTP failure`); `frontend/src/test/api/answerGuard.test.tsx` (`the About page still reads an HTML /version answer as an invalid response`)
- **OPS-ABOUT-8.** A service row is marked `differs` when its version is not the
  frontend's release (OPS-ABOUT-3) or its build differs (OPS-ABOUT-4); Refresh asks every
  service again. \
  Enforced in: `frontend/src/pages/admin/AboutPage.tsx` (`AboutPage`) \
  Pinned by: `frontend/src/test/pages/AboutPage.test.tsx` (`flags a service reporting a different version as 'differs'`, `flags a service reporting a different, non-dev build as 'differs'`, `does not flag a 'dev' build as differing, even against a different frontend build`, `Refresh re-fetches every service`)
- **OPS-ABOUT-9.** Against a running stack, every row of the About page equals the
  service's own `/version` read back through the API. \
  Enforced in: `frontend/src/pages/admin/AboutPage.tsx` (`AboutPage`) \
  Pinned by: `tests/e2e/test_about_page_playwright.py` (`test_about_page_service_versions_match_the_api_readback`)

**Out of scope.** The admin route guard and the Administration menu
(`identity-and-access.md`).

### 8.4 Structured JSON logging

**What it does.** Every service except config writes one JSON object per log line, with
the request log and any structured context, and replaces the value of any field whose
name looks like a credential.

**Surfaces.** `services/common/herd_common/logging.py`, wired in each service's
`app/main.py` through `setup_logging` and `RequestLoggingMiddleware`.

**Rules.**

- **OPS-LOG-1.** Each line carries `timestamp`, `level`, `service`, `logger`, and
  `message`, plus `exception` when the record has exception info. \
  Enforced in: `services/common/herd_common/logging.py` (`JSONFormatter`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_basic_output`, `test_json_formatter_includes_exception`); `services/reservations/tests/test_service_wiring_as_deployed.py` (`test_every_request_logs_one_access_line_through_the_json_handler`)
- **OPS-LOG-2.** Every `extra=` key that is not one of the standard library's own
  record attributes is emitted, and an extra whose value is None is omitted. \
  Enforced in: `services/common/herd_common/logging.py` (`RESERVED_LOG_RECORD_ATTRS`, `JSONFormatter`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_emits_unlisted_extras`, `test_json_formatter_old_allowlisted_keys_unchanged`, `test_json_formatter_omits_reserved_attributes`, `test_json_formatter_omits_none_extra`)
- **OPS-LOG-3.** An extra named like an envelope key (`timestamp`, `level`, `service`,
  `logger`, `message`, `exception`) is emitted as `extra_<name>` and never overwrites the
  envelope. \
  Enforced in: `services/common/herd_common/logging.py` (`ENVELOPE_KEYS`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_envelope_collision_renamed`, `test_json_formatter_envelope_collision_exception_with_real_exc_info`)
- **OPS-LOG-4.** An extra whose key contains (case-insensitively) password, passwd,
  secret, authorization, api key, kek, cookie, credential, jwt, bearer, private key, ssh
  key, community, or token renders `[redacted]`; `input_tokens`, `output_tokens`,
  `token_id`, and `token_count` are not redacted, and any other counter-shaped name that
  contains token is. \
  Enforced in: `services/common/herd_common/logging.py` (`_REDACT_KEY_PATTERN`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_redacts_credential_shaped_keys`, `test_json_formatter_does_not_redact_lookalike_keys`)
- **OPS-LOG-5.** Redaction applies at any depth inside dicts, lists, and tuples, coerces a
  non-string key to text only for the match, stops at depth 8 with `<depth limit>`, and
  never changes the caller's object. \
  Enforced in: `services/common/herd_common/logging.py` (`_redact_nested`, `_MAX_REDACT_DEPTH`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_redacts_nested_dict_key`, `test_json_formatter_redacts_dict_inside_list`, `test_json_formatter_redaction_depth_cap`, `test_json_formatter_redaction_does_not_mutate_caller_object`, `test_json_formatter_redacts_non_string_nested_keys`)
- **OPS-LOG-6.** A value that even `default=str` cannot serialize becomes
  `<unserializable>` for that key alone; the line is still written. \
  Enforced in: `services/common/herd_common/logging.py` (`JSONFormatter`, `_UNSERIALIZABLE_VALUE`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_serializes_uuid_datetime_and_raising_object`)
- **OPS-LOG-7.** Every request logs one `herd.access` line with `method`, `path` (no
  query string), `status_code`, and `duration_ms`; no header or body is logged. \
  Enforced in: `services/common/herd_common/logging.py` (`RequestLoggingMiddleware`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_request_logging_middleware_emits_access_record`); `services/secrets/tests/test_api.py` (`test_plaintext_never_hits_the_logs`); `services/reservations/tests/test_service_wiring_as_deployed.py` (`test_every_request_logs_one_access_line_through_the_json_handler`)
- **OPS-LOG-8.** `setup_logging` installs the one JSON handler on the root logger at
  `LOG_LEVEL` (INFO when the name is unknown) and holds `uvicorn.access`,
  `uvicorn.error`, and `sqlalchemy.engine` at WARNING. \
  Enforced in: `services/common/herd_common/logging.py` (`setup_logging`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_setup_logging_configures_root_logger`); `services/reservations/tests/test_service_wiring_as_deployed.py` (`test_the_root_logger_is_the_json_handler_at_log_level`); `tests/unit/test_schema_placement_and_logging_wiring.py` (`test_the_root_logger_is_the_json_handler_at_log_level`)
- **OPS-LOG-9.** The config service does not use this formatter: it logs through the
  standard library with no handler of its own, so its lines are plain text and its INFO
  lines are not emitted. `FEATURES.md` and `docs/ENV_VARS.md` (`LOG_LEVEL`) say so. \
  Enforced in: `services/config/app/main.py` (`logger`) \
  Pinned by: `tests/unit/test_schema_placement_and_logging_wiring.py` (`test_config_logs_through_the_standard_library_with_no_handler_of_its_own`)
- **OPS-LOG-10.** Key-name redaction cannot see inside a string value, so a driver run's
  start line logs the names of the context and method arguments, never their values. By
  decision; issues #872 and #905. \
  Enforced in: `services/execution/app/services/execution_service.py` (`run_driver_action`) \
  Pinned by: `services/execution/tests/test_execution_service_edges.py` (`test_run_driver_action_start_log_omits_method_kwargs_values`, `test_run_driver_action_start_log_emits_context_keys_not_values`)

**Out of scope.** Log shipping, retention, and metrics. What ai-orchestrator keeps out of
its own log lines (`ai-features.md`).

### 8.5 Settings precedence

**What it does.** Every backend service reads its settings from four places in a fixed
order, so an operator can set a value once in `.env` or once in the config page and know
which one wins.

**Surfaces.** `services/common/herd_common/config_loader.py` and
`services/common/herd_common/base_settings.py`; each service's `app/config.py`
subclasses `HerdBaseSettings`. The full ladder for operators is in
[ENV_VARS.md](../ENV_VARS.md).

**Rules.**

- **OPS-SET-1.** Without the bootstrap marker the order is constructor arguments, then
  `config.json`, then the container environment, then `.env`, then file secrets: a key
  saved in the file outranks the same key in the environment. \
  Enforced in: `services/common/herd_common/config_loader.py` (`herd_settings_sources`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_herd_sources_file_beats_env`, `test_herd_sources_init_kwargs_beat_file`); `services/common/tests/test_base_settings.py` (`test_file_beats_env_without_bootstrap_marker`)
- **OPS-SET-2.** While `config.bootstrapped` exists beside `config.json` the file ranks
  below the environment and `.env`. \
  Enforced in: `services/common/herd_common/config_loader.py` (`herd_settings_sources`, `BOOTSTRAP_MARKER`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_herd_sources_env_first_while_bootstrap_marker_present`); `services/common/tests/test_base_settings.py` (`test_env_beats_file_while_bootstrap_marker_present`)
- **OPS-SET-3.** A key absent from the file, and every key when the file is missing,
  resolves from the environment as if the file did not exist. \
  Enforced in: `services/common/herd_common/config_loader.py` (`HerdJsonConfigSource`, `_load_json`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_herd_sources_env_applies_for_keys_absent_from_file`, `test_herd_sources_pure_env_when_file_missing`); `services/common/tests/test_base_settings.py` (`test_pure_env_when_config_file_missing`)
- **OPS-SET-4.** An empty or whitespace-only string in the file means unset. A path
  where nothing exists, not even a link, is a missing file (OPS-SET-3) and logs nothing.
  Any other path that does not hold a readable JSON object is unusable: unparseable JSON
  (an empty file and a UTF-8 BOM included), a byte that is not UTF-8 (the file is read
  as UTF-8 whatever the locale), nesting deeper than the parser allows, an integer over
  Python's 4300-digit limit, valid JSON that is not an object (a list, a string, a
  number, a boolean, `null`), an unreadable file, a dangling or looping link, and a path
  that is not a regular file (a directory, a FIFO, a socket, a device), which is refused
  from its `stat` before any read, so a FIFO never blocks the import. What an unusable
  file does depends on who wrote it, read from the `config.bootstrapped` marker beside
  it (OPS-SET-2), which every service sees on its read-only `herd-config` mount. With the
  marker, the file is the first-boot copy of the environment and is ignored with one
  WARNING naming the path and the reason, so the environment's values, which the copy
  held, apply. Without it, the file holds the operator's own settings (a config-page
  save, a file from before the marker existed, a hand edit), and every service but
  config refuses to start: the settings load raises `ConfigFileUnusable` naming the
  path, the reason, and the remedy (repair the file, remove it to use the environment,
  or save the settings again on the config page). Falling back to the environment there
  could widen access (a saved `AUTH_METHOD=ldap` or `oidc` becoming compose's default
  `local`, where registration is open) or revive an `AUTH_SECRET_KEY`,
  `INTERNAL_API_TOKEN`, or superadmin password the operator rotated away from. Neither
  answer quotes the content. The config service is not one of the refusing services: its
  own reader, `config_store.load_config`, ignores every unusable path state and content
  above with a WARNING, marker or not, so it keeps serving its page and the operator can
  re-save from it. A valid file's values can also stop a service at import: a value of
  the wrong type (`{"LOG_LEVEL": 5}`) is judged by the field's own validation, as an
  environment value is, and a refusal is a `ValidationError` naming the field, never the
  value (`HerdBaseSettings` sets `hide_input_in_errors`); the `POSTGRES_*` parts are
  OPS-SET-5's. A `MemoryError` on a huge file is not handled. \
  Enforced in: `services/common/herd_common/config_loader.py` (`HerdJsonConfigSource`, `_load_json`, `_read_config_object`, `ConfigFileUnusable`, `BOOTSTRAP_MARKER`); `services/config/app/config_store.py` (`load_config`, `_read_config_object`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_source_skips_empty_string_values`, `test_source_falls_back_when_config_json_is_corrupt`, `test_unusable_bootstrap_copy_is_ignored_with_one_warning`, `test_unusable_bootstrap_copy_leaves_every_key_to_the_environment`, `test_unusable_operator_file_refuses_to_start`, `test_unusable_path_state_is_judged_by_who_wrote_it_without_blocking`, `test_readable_file_is_read_whoever_wrote_it`, `test_missing_config_file_logs_nothing`, `test_missing_config_file_with_a_marker_logs_nothing`, `test_link_to_a_readable_file_is_read`, `test_config_file_is_read_as_utf8_whatever_the_locale`); `services/acl/tests/test_settings_config_file.py` (`test_settings_load_from_the_environment_over_an_unusable_bootstrap_copy`, `test_settings_refuse_to_load_over_an_unusable_operator_file`, `test_settings_load_a_valid_file_whose_postgres_parts_form_no_url`, `test_settings_still_read_a_readable_file`); `services/user-profile/tests/test_settings_config_file.py` (`test_settings_load_from_the_environment_over_an_unusable_bootstrap_copy`, `test_settings_refuse_to_load_over_an_unusable_operator_file`, `test_settings_load_a_valid_file_whose_postgres_parts_form_no_url`, `test_settings_still_read_a_readable_file`); `services/config/tests/test_load_config.py` (`test_load_config_ignores_an_unusable_file_naming_only_the_reason`, `test_load_config_refuses_a_path_that_is_not_a_regular_file_without_blocking`, `test_load_config_logs_nothing_for_a_missing_file`, `test_load_config_reads_utf8_whatever_the_locale`)
- **OPS-SET-5.** The file's `AUTH_*` keys map to the services' unprefixed names, and the
  file's `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` build a percent-encoded
  `DATABASE_URL` against `postgres:5432` only when all three are set; `null`, an empty
  string, and a whitespace-only string are unset (OPS-SET-4). A part that is a string
  is used as is; an integer or a float is used as Python writes it (`12345`, `1.5`; `1e3`
  becomes `1000.0`). A part that is a boolean, a list, an object, or a string holding a
  lone surrogate derives no URL, and one WARNING names the keys, never the values. \
  Enforced in: `services/common/herd_common/config_loader.py` (`_KEY_MAP`, `_build_database_url`, `_is_unset`, `_url_part`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_source_maps_auth_keys`, `test_source_builds_database_url_when_postgres_fields_present`, `test_source_url_encodes_special_chars_in_password`, `test_source_skips_database_url_when_postgres_incomplete`, `test_postgres_part_that_is_a_number_builds_the_url_from_its_text`, `test_postgres_part_that_cannot_be_url_text_leaves_database_url_underived`, `test_postgres_part_that_is_unset_leaves_database_url_underived_silently`)
- **OPS-SET-6.** A `DATABASE_URL` in the environment always beats the file-derived one,
  in both orders. \
  Enforced in: `services/common/herd_common/config_loader.py` (`HerdJsonConfigSource`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_source_database_url_yields_to_env_database_url`); `services/common/tests/test_base_settings.py` (`test_env_database_url_beats_file_derived_database_url`)
- **OPS-SET-7.** Every service but config reads the file at `HERD_CONFIG_FILE`, default
  `/etc/herd/config.json`, mounted read-only from the `herd-config` volume; a missing
  file is not an error (OPS-SET-3). \
  Enforced in: `services/common/herd_common/config_loader.py` (`CONFIG_FILE`); `docker-compose.yml` (`herd-config`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_source_returns_nothing_when_file_missing`)
- **OPS-SET-8.** Every service's `Settings` subclasses `HerdBaseSettings`, which supplies
  the source order and `env_file=.env` with case-insensitive names; the config service
  has no `Settings` model. \
  Enforced in: `services/common/herd_common/base_settings.py` (`HerdBaseSettings`) \
  Pinned by: `services/common/tests/test_base_settings.py` (`test_model_config_env_file_and_case_sensitivity`, `test_subclass_inherits_settings_customise_sources_without_override`)
- **OPS-SET-9.** Each service reads its settings once, at import, so a change to the
  file or the environment takes effect only when the container restarts (an environment
  change needs a recreate). \
  Enforced in: `services/execution/app/config.py` (`settings`) \
  Pinned by: none

**Out of scope.** Which keys each service has (each area's Configuration section and
[ENV_VARS.md](../ENV_VARS.md)); the compose wiring of those keys (section 8.16).

### 8.6 Config service

**What it does.** On a new stack the config service writes `config.json` from `.env`
when every required value is there; otherwise an operator opens the config page from
the login screen, signs in with the config password, fills in the required values, and
presses Save and Restart. While the config service answers that the file does not
exist, the HERD login form is disabled; while that answer is still loading, or when the
config service cannot be reached, the form stays enabled and a sign-in goes to auth as
usual (IAM-UI-9 in `identity-and-access.md`).

**Surfaces.** `services/config/app/` (`main.py`, `config_store.py`, `auth.py`,
`docker_ctl.py`, `restart_record.py`, `config_schema.py`, `auth_rules.py`, `validation_errors.py`); the restarter,
`infra/restarter/restarter.py`, the one HERD container that holds the Docker socket
([ADR 0017](../design/0017-config-restart-without-the-docker-socket.md));
`frontend/src/pages/ConfigPage.tsx`, `frontend/src/lib/configRestart.ts`, and `frontend/src/stores/configStore.ts`. The login page's use of `GET /status` is
`identity-and-access.md` (IAM-UI-9).

**Rules.**

- **OPS-CONFIG-1.** At startup, when `config.json` does not exist and every required
  field has a non-blank environment value and the access-token settings pass
  OPS-CONFIG-23, the service writes the file from the environment values of every schema
  field and writes the `config.bootstrapped` marker beside it; otherwise it logs the
  missing names or the refusal and writes nothing. \
  Enforced in: `services/config/app/config_store.py` (`bootstrap_from_env`, `load_env_values`) \
  Pinned by: `services/config/tests/test_bootstrap.py` (`test_bootstrap_writes_config_when_required_env_set`, `test_bootstrap_writes_marker`, `test_bootstrap_skips_when_required_missing`, `test_bootstrap_skips_blank_required`, `test_lifespan_invokes_bootstrap`)
- **OPS-CONFIG-2.** The bootstrap never overwrites an existing `config.json`. \
  Enforced in: `services/config/app/config_store.py` (`bootstrap_from_env`) \
  Pinned by: `services/config/tests/test_bootstrap.py` (`test_bootstrap_skips_when_file_exists`)
- **OPS-CONFIG-3.** A save through `PUT /settings` deletes the bootstrap marker, so the
  file outranks the environment from each service's next start (OPS-SET-1). \
  Enforced in: `services/config/app/config_store.py` (`save_config`) \
  Pinned by: `services/config/tests/test_bootstrap.py` (`test_save_config_clears_bootstrap_marker`)
- **OPS-CONFIG-4.** `GET /status` answers `configured` (the file exists) and
  `password_changed`, which is true only when the auth file is a JSON object whose
  `password_changed` is the JSON boolean `true`. An auth file that is corrupt or not an
  object (a list, a string, a number, `null`, a boolean), or whose `password_changed` is
  anything else (`"yes"`, `1`, `null`, missing), reports `password_changed` false with a
  200 instead of failing (before issue #1427 a file that was not an object was a 500). \
  Enforced in: `services/config/app/main.py` (`get_status`); `services/config/app/config_store.py` (`is_configured`, `is_password_changed`, `load_auth`) \
  Pinned by: `services/config/tests/test_config.py` (`test_status_unconfigured`, `test_status_configured`, `test_status_with_corrupt_auth_file`, `test_status_answers_200_not_rotated_for_a_file_that_is_not_an_object`, `test_is_password_changed_is_false_unless_the_stored_value_is_json_true`, `test_is_password_changed_is_true_for_json_true`)
- **OPS-CONFIG-5.** On first use the config password is `CONFIG_ADMIN_PASSWORD` when set,
  counted as rotated; otherwise a random password is generated, logged once at WARNING,
  and counted as not rotated. No fixed default exists. A blank or whitespace-only value
  counts as unset; any other value is used as set, of any length, once it passes
  OPS-CONFIG-8's character rule (`password_problem`). The environment cannot hold a NUL,
  but a byte that is not UTF-8 arrives as a lone surrogate: such a value seeds nothing.
  No auth file is written and no password is generated, every login is a 401, `/status`
  reports `password_changed` false, and each use (each `/status` read, each login
  attempt) logs one ERROR, the fixed text `CONFIG_ADMIN_PASSWORD is not valid UTF-8
  text, so it cannot be the config password; no auth file written, login refused` with
  no arguments, so neither the value nor anything derived from it is logged. Fixing the
  variable is the recovery (before issue #1428 this was a 500 on `/status` and on
  login). \
  Enforced in: `services/config/app/config_store.py` (`_initial_password`, `load_auth`, `password_problem`, `ENV_VALUE_REFUSED_LOG`) \
  Pinned by: `services/config/tests/test_config.py` (`test_old_default_password_rejected`, `test_random_seed_used_and_unrotated`, `test_env_password_is_used_and_marks_rotated`); `services/config/tests/test_config_passwords.py` (`test_the_environment_cannot_hold_a_nul`, `test_an_admin_password_that_is_not_utf8_seeds_nothing_and_fails_closed`, `test_the_only_refusal_an_environment_value_can_reach_is_not_utf8`, `test_a_blank_admin_password_still_means_a_generated_one`, `test_a_legal_admin_password_of_any_length_seeds_and_signs_in`)
- **OPS-CONFIG-6.** `POST /login` checks the password against the stored bcrypt hash and
  answers a session token valid for 30 minutes plus `password_changed`; a wrong password
  is 401 `Invalid password`. Only the first 72 UTF-8 bytes of a password reach bcrypt,
  when hashing and when checking, so two passwords match exactly when those bytes are
  equal (a 32-character change-password value can be 128 bytes). bcrypt 4.0.1 cut there
  silently and bcrypt 5 raises, so the cut is explicit (issue #1355); a table of 284
  decisions the bcrypt 4.0.1 code made is replayed by the tests. New hashes are `$2b$` at
  cost 12. A login password that is empty, holds a NUL anywhere, or cannot be encoded as
  UTF-8 never signs in, whatever the stored hash (issue #1429): bcrypt appends a NUL to
  the key and repeats it, so a hash of NULs alone matches the empty password and every
  run of NULs, and bcrypt alone cannot be trusted with these. Of the 284 replayed
  decisions, the 13 accepted pairs whose password is empty or holds a NUL are now
  refused, by name. An operator whose stored password holds a NUL (only a crafted
  change-password request before #1429 could store one; a browser field cannot type a
  NUL) can no longer sign in with it and recovers by removing the auth file
  (OPS-CONFIG-7). One door stays open until that operator acts: a hash stored from a
  password with NUL separators, such as `ab`, NUL, `ab`, NUL, `ab`, still matches the
  NUL-free password `ab`, because their bcrypt keys are equal. \
  Enforced in: `services/config/app/main.py` (`login`); `services/config/app/auth.py` (`create_session_token`, `_EXPIRE_MINUTES`); `services/config/app/config_store.py` (`_password_bytes`, `_hash_password`, `_check_password`, `password_problem`, `BCRYPT_MAX_BYTES`, `BCRYPT_ROUNDS`) \
  Pinned by: `services/config/tests/test_config.py` (`test_login_success`, `test_login_wrong_password`); `services/config/tests/test_auth.py` (`test_create_session_token_round_trips`); `services/config/tests/test_config_passwords.py` (`test_every_legacy_decision_is_kept`, `test_two_passwords_match_exactly_when_their_first_72_utf8_bytes_match`, `test_bcrypt_never_receives_more_than_72_bytes`, `test_new_hashes_are_2b_at_cost_12`, `test_a_stored_legacy_hash_over_72_bytes_still_signs_in`, `test_change_password_to_32_multibyte_characters_then_sign_in`, `test_an_operator_password_over_72_bytes_seeds_and_verifies`, `test_eight_nuls_never_become_the_password_and_the_empty_password_never_signs_in`, `test_a_legacy_hash_of_a_password_with_a_nul_never_signs_in_with_a_nul_or_empty_password`, `test_a_legacy_nul_separated_hash_still_matches_its_shorter_twin`, `test_every_refused_nul_or_empty_password_has_a_different_bcrypt_twin`)
- **OPS-CONFIG-7.** A present but unreadable auth file denies every login and is never
  regenerated; removing the file is the operator's recovery. Unreadable is every way
  `open` and `json.load` can fail short of running out of memory: unparseable JSON, a
  byte that is not UTF-8, nesting deeper than the parser allows, an integer over
  Python's digit limit, and a path that cannot be opened or is a directory (the middle
  three were a 500 on `/status`, login, save, and change-password before the issue
  #1427 review). A file that parses but
  holds no usable hash (not an object, the key missing, not a string, or a value bcrypt
  cannot read) denies every login the same way, with a 401 and one ERROR log naming the
  class only, never the value (before issue #1355 most of these were a 500). That ERROR
  is logged exactly when the stored value is unusable, whatever the client sent: a
  password that is not a string, or that OPS-CONFIG-8's character rule refuses (empty, a
  NUL, a lone surrogate), is refused with no log line, any other password (an over-long
  one) is checked as usual, and against an unusable file the line is still logged once. A file that is
  not an object is refused by `load_auth` itself, so every reader fails closed the
  same way. Nothing rewrites an unreadable or non-object file: a `POST /change-password`
  from a session issued before the file became unusable answers 503 `The config password
  store is unusable; the config service log names the cause` and leaves the file as it
  was (before issue #1427 it was a 500). \
  Enforced in: `services/config/app/config_store.py` (`load_auth`, `verify_password`, `ConfigAuthError`, `_check_password`, `UNUSABLE_HASH_MESSAGE`, `STORE_UNUSABLE_DETAIL`); `services/config/app/main.py` (`config_store_unusable`) \
  Pinned by: `services/config/tests/test_config.py` (`test_verify_password_fails_closed`, `test_default_password_not_regenerated_on_corrupt`, `test_login_with_corrupt_auth_file`); `services/config/tests/test_config_passwords.py` (`test_an_unusable_auth_file_refuses_login_without_raising`, `test_login_against_an_unusable_auth_file_is_401_not_500`, `test_a_hostile_client_password_against_a_good_auth_file_never_blames_the_file`, `test_a_non_string_client_password_is_refused_by_the_schema_without_blaming_the_file`, `test_check_password_logs_nothing_for_a_hostile_password_and_a_good_hash`, `test_an_unusable_stored_hash_is_reported_exactly_once_whatever_the_password`, `test_a_change_password_that_is_refused_keeps_the_file_and_blames_nothing`, `test_a_hostile_password_never_matches_a_hash_of_the_empty_password`, `test_the_refused_set_is_the_non_strings_the_non_encodable_nul_and_empty`); `services/config/tests/test_config.py` (`test_load_auth_refuses_a_file_that_is_not_an_object`, `test_change_password_from_an_old_session_never_rewrites_an_unusable_file`, `test_load_auth_raises_config_auth_error`, `test_every_route_fails_closed_on_an_unreadable_auth_file`)
- **OPS-CONFIG-8.** `POST /change-password` needs a session and a new password of 8 to 32
  characters, counted as code points, not bytes, and marks the password rotated. One
  character rule, `password_problem`, decides which strings can be a config password at
  every entry point (this route, login, `CONFIG_ADMIN_PASSWORD`), and hashing itself
  refuses what it refuses: no NUL anywhere (issue #1429), encodable as UTF-8, so no lone
  surrogate (issue #1428), and not empty. Every other character is accepted, other
  control characters included. A refusal is a 422 whose `detail` is a string that never
  holds the submitted value, and the stored hash is unchanged. The length is judged
  first: `Password must be at least 8 characters`, `Password must be at most 32
  characters`; then `Password must not contain a NUL character`; then `Password must be
  valid UTF-8 text`. Before these issues eight NULs were accepted (and then the empty
  password signed in), a lone surrogate was a 500, and the length refusals were a
  validation list that echoed the password. A body whose `new_password` is missing or
  not a string is OPS-CONFIG-30's validation list. The config page judges the same
  bound before it sends, counting code points (`configPasswordLengthOk`), and refuses
  nothing else, so it is never stricter than the route; its inputs carry no `minlength`
  or `maxlength`, which count UTF-16 units (an emoji is two) and cut a pasted value. It
  sends the password unchanged, shows a 422 string `detail` from this route as is, and
  shows `Failed to change password` for every other failure, the 503 and a validation
  list included. Before issue #1452 the page refused 17 to 32 emoji, cut a pasted
  value at 32 units, and showed only the generic text for every refusal. \
  Enforced in: `services/config/app/main.py` (`change_password_endpoint`, `ChangePasswordRequest`); `services/config/app/config_store.py` (`password_problem`, `new_password_problem`, `change_password`, `_hash_password`, `InvalidPassword`); `frontend/src/lib/configPassword.ts` (`codePointLength`, `configPasswordLengthOk`); `frontend/src/lib/errors.ts` (`configChangePasswordErrorText`, `CONFIG_CHANGE_PASSWORD_FALLBACK`); `frontend/src/pages/ConfigPage.tsx` (`PasswordChange`) \
  Pinned by: `frontend/src/test/lib/configPassword.test.ts` (`codePointLength (issue #1452)`, `configPasswordLengthOk (issue #1452, OPS-CONFIG-8)`, `configChangePasswordErrorText (issue #1452)`); `frontend/src/test/pages/ConfigPage.test.tsx` (`ConfigPage change password (issue #1452)`); `services/config/tests/test_config.py` (`test_change_password`, `test_change_password_too_short`, `test_change_password_too_long`, `test_change_password_unauthenticated`); `services/config/tests/test_config_passwords.py` (`test_change_password_refuses_an_illegal_password_without_echo_and_keeps_the_file`, `test_change_password_accepts_every_other_password_and_it_signs_in`, `test_password_problem_enumerates_the_space`, `test_every_legal_password_in_the_space_hashes_and_signs_in`, `test_new_password_problem_checks_the_bound_in_characters_then_the_rule`, `test_an_illegal_password_never_reaches_bcrypt`, `test_new_password_problem_counts_characters`, `test_change_password_bound_counts_characters_over_http`)
- **OPS-CONFIG-9.** `PUT /settings` and `POST /apply` answer 403 `Change the config
  password before modifying or applying configuration` until the password is rotated,
  read on every request as OPS-CONFIG-4 reads it: a session issued before the auth file
  became unusable, or before its `password_changed` became anything but `true`, gets the
  403 and writes nothing. \
  Enforced in: `services/config/app/main.py` (`require_password_rotated`); `services/config/app/config_store.py` (`is_password_changed`) \
  Pinned by: `services/config/tests/test_config.py` (`test_put_settings_locked_until_rotated`, `test_apply_locked_until_rotated`, `test_write_allowed_with_operator_password`, `test_a_session_from_before_the_file_changed_cannot_write_or_apply`)
- **OPS-CONFIG-10.** `GET /schema` lists the editable fields to anyone; `GET /settings`
  needs a session and answers the environment values overlaid by the file's values,
  limited to the schema's keys, with every non-empty secret field shown as `********`.
  A `config.json` that is not a JSON object, or that OPS-CONFIG-7 would call unreadable,
  is logged at WARNING and read as empty: the editor shows the environment's values and
  a save writes a new file (the non-UTF-8, too-deep, and over-long-integer cases were a
  500 on `GET /settings` and `PUT /settings` before the issue #1427 review). \
  Enforced in: `services/config/app/main.py` (`get_schema`, `get_settings`, `SCHEMA_KEYS`); `services/config/app/config_store.py` (`load_config`) \
  Pinned by: `services/config/tests/test_config.py` (`test_schema`, `test_get_settings_includes_env_values_when_no_file`, `test_get_settings_file_overrides_env`, `test_get_settings_redacts_env_secrets`, `test_settings_unauthenticated`, `test_get_settings_answers_schema_keys_only`, `test_get_settings_with_corrupt_config_file`, `test_load_config_ignores_it`, `test_the_settings_routes_treat_an_unreadable_config_file_as_corrupt`)
- **OPS-CONFIG-11.** A secret field sent back as `********` keeps the file's value, else
  the environment's; with neither it is dropped, and the placeholder itself is never
  written. \
  Enforced in: `services/config/app/main.py` (`update_settings`) \
  Pinned by: `services/config/tests/test_config.py` (`test_save_settings_preserves_redacted_secrets`, `test_save_settings_resolves_masked_secret_from_env`, `test_save_settings_drops_masked_optional_secret_without_source`, `test_save_settings_masked_required_secret_without_source_is_422`)
- **OPS-CONFIG-12.** A save may write only keys `CONFIG_SCHEMA` declares: a body naming
  any other key is 422 `{"errors": ["Unknown settings: <KEY>, ..."]}` (the unknown keys
  sorted, checked first) and writes nothing. A save missing a required field, or carrying
  it blank, is 422 `{"errors": ["<KEY> is required", ...]}` and writes nothing, except
  `AUTH_SECRET_KEY`, which reports OPS-CONFIG-23's message after those entries. Otherwise
  the body's values are written, and a key already in the file outside the schema (placed
  there by hand) is carried over unchanged. A file that is not a JSON object reads as
  empty. \
  Enforced in: `services/config/app/main.py` (`update_settings`, `SCHEMA_KEYS`); `services/config/app/config_schema.py` (`SCHEMA_KEYS`); `services/config/app/config_store.py` (`save_config`, `load_config`) \
  Pinned by: `services/config/tests/test_config.py` (`test_save_settings_refuses_keys_outside_the_schema`, `test_save_settings_with_unknown_key_leaves_existing_file_unchanged`, `test_unknown_key_is_refused_before_the_required_check`, `test_save_settings_with_schema_keys_only_saves`, `test_save_settings_keeps_keys_already_in_the_file_outside_the_schema`, `test_load_config_non_object_returns_empty`, `test_save_settings_missing_required`, `test_save_config_blank_required`, `test_save_and_get_settings`)
- **OPS-CONFIG-13.** `POST /apply` makes one call, a `POST /restart` with no query and
  an empty body to the restarter at `restarter:8000` on the internal `herd-ctl`
  network; nothing from the apply request (query, body, headers, session) is passed
  on. The restarter restarts only containers of its own compose project,
  `HERD_COMPOSE_PROJECT`, which compose sets from `COMPOSE_PROJECT_NAME`; when that is
  unset or blank it makes no Docker request and its one error is
  `HERD_COMPOSE_PROJECT is unset or blank; refusing a restart that is not scoped to one compose project`. \
  Enforced in: `services/config/app/docker_ctl.py` (`restart_services`, `RESTART_PATH`); `services/config/app/main.py` (`apply_config`); `infra/restarter/restarter.py` (`run_restart`, `PROJECT_ENV`, `PROJECT_UNSET_ERROR`) \
  Pinned by: `services/config/tests/test_docker_ctl.py` (`test_the_one_call_is_a_bare_post_restart`, `test_apply_passes_nothing_from_the_request_on`); `infra/restarter/tests/test_restarter_docker.py` (`test_restarts_exactly_the_allowlist_within_the_project`, `test_unset_or_blank_project_refuses_with_no_docker_request`)
- **OPS-CONFIG-14.** The restarter restarts each container of the daemon's container
  list whose own labels name its project and a service in `RESTART_SERVICES`, the
  eleven services that mount `herd-config:/etc/herd:ro` (acl, ai-orchestrator, auth,
  cabling, execution, integration, inventory, notifications, reservations, secrets,
  user-profile), every replica of such a service included, in service order, each with
  a 30 second stop timeout. A one-off container (`com.docker.compose.oneoff=True`, from
  `docker compose run`) and any other container (config, traefik, postgres, nats,
  frontend, selenium, the restarter, a container of another project, one without
  labels) is never restarted, also when the daemon's answer includes it. The answer's
  `restarted` names each service once, sorted, when every one of its containers
  restarted; each container that did not adds one `errors` entry (OPS-CONFIG-16). \
  Enforced in: `infra/restarter/restarter.py` (`RESTART_SERVICES`, `ONEOFF_LABEL`, `STOP_TIMEOUT_SECONDS`, `run_restart`) \
  Pinned by: `infra/restarter/tests/test_restarter_docker.py` (`test_restart_services_is_exactly_the_expected_literal`, `test_restarts_exactly_the_allowlist_within_the_project`, `test_only_running_members_of_a_are_restarted`, `test_every_replica_is_restarted_and_the_service_is_named_once`, `test_a_service_with_one_failed_replica_is_not_listed_as_restarted`, `test_a_one_off_container_is_never_restarted`); `tests/unit/test_restarter_compose_guard.py` (`test_restart_allowlist_equals_the_services_that_read_config_json`)
- **OPS-CONFIG-15.** `POST /apply` with no `config.json` is 400 `No configuration to
  apply`. \
  Enforced in: `services/config/app/main.py` (`apply_config`) \
  Pinned by: `services/config/tests/test_config.py` (`test_apply_not_configured`)
- **OPS-CONFIG-16.** The restarter reports each failure as an entry in `errors` that
  names what failed and the exception class only (`Cannot connect to Docker: <ClassName>`,
  `Cannot list containers: <ClassName>`, `Failed to restart <service>: <ClassName>`); the
  exception text, the daemon's answer included, goes to its log. A list failure
  restarts nothing; a failed restart does not stop the others. \
  Enforced in: `infra/restarter/restarter.py` (`run_restart`, `DockerAPIError`, `UnexpectedDockerResponse`, `UnexpectedContainerId`) \
  Pinned by: `infra/restarter/tests/test_restarter_docker.py` (`test_missing_socket_is_cannot_connect_with_the_class_only`, `test_list_failure_is_cannot_list_with_the_class_only`, `test_a_malformed_list_answer_is_cannot_list`, `test_a_failed_restart_names_the_service_and_class_and_the_rest_go_on`, `test_restart_is_accepted_on_any_2xx_and_refused_otherwise`)
- **OPS-CONFIG-17.** Session tokens are signed with `CONFIG_SESSION_SECRET` when set,
  else a random per-process key, so a restart ends every session; an expired, tampered,
  or foreign-key token is 401. \
  Enforced in: `services/config/app/auth.py` (`_load_session_secret`, `_verify_token`) \
  Pinned by: `services/config/tests/test_auth.py` (`test_config_session_secret_env_var_is_honored`, `test_verify_token_rejects_expired_token`, `test_verify_token_rejects_bad_signature`, `test_verify_token_rejects_token_signed_with_old_forgeable_constant`)
- **OPS-CONFIG-18.** The browser keeps the config session token in session storage and
  shows the config sign-in form until it has one. \
  Enforced in: `frontend/src/stores/configStore.ts` (`setConfigToken`, `clearConfigToken`); `frontend/src/pages/ConfigPage.tsx` (`ConfigPage`) \
  Pinned by: `frontend/src/test/stores/configStore.test.ts` (`setConfigToken updates the state and persists to sessionStorage`, `clearConfigToken nulls the state and removes the sessionStorage entry`); `frontend/src/test/pages/ConfigPage.test.tsx` (`renders the config login form when no token is present`, `posts the password and stores the token on success`, `toasts an error when the config login is rejected`)
- **OPS-CONFIG-19.** Save and Restart saves, then applies, and shows the restarted
  services and each error under "Restart Result", from the answer to `POST /apply` or,
  when that answer is lost, from the config service's record of the run
  (OPS-CONFIG-33). The browser test runs only with `HERD_E2E_RESTART=1`; nightly.yml
  sets it in its last test step, with IPv6 on, where the test asserts no "Restart
  Result" panel before the click, then that the panel lists exactly `RESTART_SERVICES`
  and no error, then that every container of those services has a later start time
  than before the click and is running and healthy within 300 seconds (issue #1326),
  then that `GET /apply/last` answers that same result with a UTC `finished_at`, and
  that after a page reload the result shows again under a `Last restart, finished ...`
  heading (issue #1433). \
  Enforced in: `frontend/src/pages/ConfigPage.tsx` (`handleSaveAndRestart`, `showRestartResult`); `.github/workflows/nightly.yml` (`HERD_E2E_RESTART`) \
  Pinned by: `tests/e2e/test_config_playwright.py` (`test_config_save_and_restart_gated`); `frontend/src/test/pages/ConfigPage.test.tsx` (`shows the answered result directly when POST /apply answers`, `shows an answered result with errors as a partial failure`)
- **OPS-CONFIG-20.** Against a running stack, a value saved in the config page reads back
  through `GET /settings`. \
  Enforced in: `frontend/src/pages/ConfigPage.tsx` (`ConfigPage`) \
  Pinned by: `tests/e2e/test_config_playwright.py` (`test_config_save_persists_value_via_api_readback`, `test_config_full_cycle_edit_and_restore_via_ui`)
- **OPS-CONFIG-21.** The config service answers cross-origin requests from any origin
  (`allow_origins=["*"]`, deliberately not `herd_common`'s CORS helper) and never allows
  credentials: a preflight or a request carrying a cookie gets
  `Access-Control-Allow-Origin: *` and no `Access-Control-Allow-Credentials`. The page
  sends its session in the `Authorization` header, which needs no credentials mode. \
  Enforced in: `services/config/app/main.py` (`CORSMiddleware`, `allow_credentials=False`) \
  Pinned by: `services/config/tests/test_cors.py` (`test_cors_middleware_options_are_wildcard_without_credentials`, `test_preflight_from_any_origin_allows_no_credentials`, `test_simple_request_with_cookie_does_not_echo_the_origin`)
- **OPS-CONFIG-22.** `POST /login` limits failed attempts in process, per source address
  and across all sources. From a source's third consecutive failure on, it waits before
  its next attempt: 1 second, doubling with each further failure, capped at 60 seconds;
  a source with no failure for 15 minutes starts over. When `CONFIG_LOGIN_MAX_ATTEMPTS`
  failures (default 20) from any sources fall within `CONFIG_LOGIN_LOCKOUT_SECONDS`
  (default 300), every source waits that long. While a wait applies the login is 429
  `Too many failed login attempts; try again later` with `Retry-After` in whole seconds,
  answered before the password is checked. A successful login clears its source's
  failures and the cross-source count. Each wait that begins logs one WARNING line
  starting `config_login_locked` with the scope (`source` or `global`), the source
  address, the failure count, and the wait, and never the password. The source is the
  first `X-Forwarded-For` entry when it is an IP address, else the peer, which relies on
  Traefik replacing any client-supplied value; state is per process on the monotonic
  clock, with idle sources dropped and at most 10,000 kept. A knob that is not a positive
  integer falls back to its default with a warning. The config page shows the 429 as
  `Too many failed login attempts; try again in N seconds` when `Retry-After` is a
  positive whole number, and as the server's sentence otherwise, never as the
  wrong-password message a 401 gets; Sign in stays disabled for those N seconds. \
  Enforced in: `services/config/app/login_limits.py` (`LoginLimiter`, `source_delay_seconds`, `client_source`, `LIMITER`); `services/config/app/main.py` (`login`, `LOGIN_LOCKED_DETAIL`); `frontend/src/lib/errors.ts` (`loginRetryAfterText`, `loginRetryAfterSeconds`); `frontend/src/pages/ConfigPage.tsx` (`ConfigLogin`) \
  Pinned by: `frontend/src/test/lib/errors.test.ts` (`loginRetryAfterSeconds and loginRetryAfterText`); `frontend/src/test/pages/ConfigPage.test.tsx` (`shows the wait from Retry-After and disables Sign in until it passes`, `shows the server's sentence and keeps Sign in enabled when Retry-After is absent`, `treats a Retry-After that is not a whole number of seconds as no stated wait`, `keeps the wrong-password wording for a 401 and leaves Sign in enabled`); `services/config/tests/test_login_limits.py` (`test_source_delay_schedule_starts_after_the_third_failure_and_caps_at_60`, `test_each_failure_sets_the_scheduled_wait`, `test_wait_counts_down_and_expires`, `test_success_resets_the_source`, `test_sources_are_isolated`, `test_a_quiet_source_starts_over`, `test_global_lockout_after_max_attempts_across_sources`, `test_global_window_slides`, `test_success_clears_the_cross_source_count`, `test_tracked_sources_are_bounded`, `test_idle_sources_are_dropped`, `test_knobs_are_read_from_the_environment`, `test_knob_defaults`, `test_a_knob_that_is_not_a_positive_integer_falls_back_to_its_default`, `test_source_is_the_first_forwarded_address`, `test_source_falls_back_to_the_peer`, `test_lock_log_line_names_source_scope_and_wait`, `test_login_waits_after_the_third_failure`, `test_login_success_resets_the_count`, `test_login_sources_are_keyed_on_the_forwarded_address`, `test_login_global_lockout_refuses_every_source`, `test_login_lock_log_never_carries_the_password`, `test_login_limits_imports_only_the_standard_library`)

- **OPS-CONFIG-23.** A save through `PUT /settings`, and the first-boot bootstrap, apply
  the services' startup rules for the access-token settings (IAM-JWT-4) through a
  standard-library copy, since the config service has no `herd_common`: an
  `AUTH_SECRET_KEY` that is missing, not a string, has no printable character other than
  white space, or, trimmed, starts with `<` and ends with `>` is refused with `AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env, or in the config UI if a configuration was saved there, to a random value, for example the output of openssl rand -hex 32`, a
  masked `********` that resolves to such a value included (OPS-CONFIG-11); an
  `AUTH_ALGORITHM` other than an empty string (which the services read as unset), HS256,
  HS384, or HS512 is refused with `AUTH_ALGORITHM must be one of HS256, HS384, HS512`. A
  save answers 422 `{"errors": [...]}` with these after any `<KEY> is required` entries
  and writes nothing; the bootstrap logs them and writes nothing (OPS-CONFIG-1). The two
  messages are the shared module's, spelled identically. The services' key-shape check and
  signing round trip through PyJWT are not copied, so a value only they refuse is refused at service startup
  with `AUTH_SECRET_KEY cannot be used as an HMAC secret; set it in .env, or in the config UI if a configuration was saved there, to a random value, for example the output of openssl rand -hex 32`. \
  Enforced in: `services/config/app/auth_rules.py` (`auth_setting_errors`, `secret_key_refused`, `algorithm_refused`, `SECRET_KEY_REFUSED_MESSAGE`, `ALGORITHM_REFUSED_MESSAGE`); `services/config/app/config_store.py` (`save_config`, `bootstrap_from_env`) \
  Pinned by: `services/config/tests/test_auth_rules.py` (`test_messages_are_the_shared_modules_spelled_identically`, `test_the_copy_refuses_the_secrets_the_shared_rule_refuses`, `test_the_copy_refuses_other_algorithms`, `test_save_refuses_a_blank_or_placeholder_secret_and_writes_nothing`, `test_save_refuses_an_rs256_algorithm_and_writes_nothing`, `test_save_reports_every_refusal_together`, `test_save_refusal_leaves_an_existing_file_unchanged`, `test_put_refuses_with_the_shared_message_and_leaves_the_file`, `test_put_keeping_a_stored_placeholder_secret_is_refused`, `test_put_keeping_an_env_placeholder_secret_is_refused`, `test_bootstrap_with_a_placeholder_secret_writes_nothing`, `test_bootstrap_with_an_rs256_algorithm_writes_nothing`, `test_bootstrap_with_good_auth_settings_writes`)

- **OPS-CONFIG-24.** The config service passes the restarter's 200 body through only
  when it is exactly `{restarted, errors}`, two lists of at most 64 printable strings
  of 1 to 200 characters, in at most 64 KiB. Otherwise `POST /apply` answers one
  `errors` entry: `Cannot reach the restart service: <ClassName>` (no answer within
  720 seconds, a refused connection, an unresolvable name), `A restart is already in
  progress` (a 409), `The restart service answered HTTP <status>` (any other status),
  or `The restart service answered an unexpected body`. The restarter's body is never
  echoed in those cases; it goes to the log. \
  Enforced in: `services/config/app/docker_ctl.py` (`restart_services`, `_validated`, `IN_PROGRESS_ERROR`, `UNEXPECTED_BODY_ERROR`, `RESTART_TIMEOUT_SECONDS`) \
  Pinned by: `services/config/tests/test_docker_ctl.py` (`test_the_answer_is_passed_through_when_it_has_exactly_the_shape`, `test_a_409_is_the_in_progress_entry`, `test_another_status_is_named_and_the_body_never_echoed`, `test_any_other_200_body_is_the_unexpected_body_entry`, `test_a_refused_connection_is_cannot_reach_with_the_class_only`, `test_an_unresolvable_restarter_is_cannot_reach`, `test_a_silent_restarter_times_out`, `test_apply_passes_the_409_on_as_an_error_entry`)
- **OPS-CONFIG-25.** The restarter's HTTP surface is `POST /restart` and `GET /health`
  (`{"status": "ok", "service": "restarter"}`, with no Docker call). `POST /restart`
  ignores its query string, body, and headers. Any other path is 404 `Not found` and
  another method on those two paths is 405 `Method not allowed` with `Allow`. A request
  with `Transfer-Encoding`, or a Content-Length that is not one whole number, is 400;
  a body over 4096 bytes is 413; more than 32 headers is 431. Every answer is JSON,
  carries a status line, and closes the connection; a client that sends nothing is
  dropped after 10 seconds. \
  Enforced in: `infra/restarter/restarter.py` (`RestarterHandler`, `_ROUTES`, `MAX_REQUEST_BODY_BYTES`, `MAX_REQUEST_HEADERS`, `REQUEST_READ_TIMEOUT_SECONDS`) \
  Pinned by: `infra/restarter/tests/test_restarter_http.py` (`test_post_restart_answers_the_result_shape`, `test_no_request_parameter_reaches_docker`, `test_any_other_path_is_404_and_reaches_no_docker_call`, `test_another_method_on_restart_is_405`, `test_head_is_405_with_no_body`, `test_another_method_on_health_is_405`, `test_health_answers_without_asking_docker`, `test_a_body_framing_the_server_does_not_take_is_refused`, `test_an_overlong_request_line_is_414_json`, `test_too_many_headers_is_431_json`, `test_garbage_is_400_json`, `test_an_http_0_9_request_line_still_gets_a_status_line_and_json`, `test_a_client_that_sends_nothing_is_dropped_and_others_are_served`)
- **OPS-CONFIG-26.** The restarter's Docker requests are only
  `GET /v1.44/containers/json` with exactly the filter
  `{"label":["com.docker.compose.project=<project>"]}` and
  `POST /v1.44/containers/<id>/restart?t=30`, where `<id>` is 64 lowercase hex
  characters; a listed id of any other shape is never put in a path and is reported
  as `Failed to restart <service>: UnexpectedContainerId`. \
  Enforced in: `infra/restarter/restarter.py` (`DOCKER_API_VERSION`, `_CONTAINER_ID`, `run_restart`) \
  Pinned by: `infra/restarter/tests/test_restarter_docker.py` (`test_restarts_exactly_the_allowlist_within_the_project`, `test_an_odd_container_id_never_reaches_a_path`)
- **OPS-CONFIG-27.** One restart pass runs at a time: a `POST /restart` while another
  runs is 409 `A restart is already in progress` and reaches no Docker call. The pass
  releases the lock when it ends, also after a failed pass or an unexpected error
  (then 500 `Internal Server Error`). \
  Enforced in: `infra/restarter/restarter.py` (`RestarterServer`, `IN_PROGRESS_DETAIL`) \
  Pinned by: `infra/restarter/tests/test_restarter_http.py` (`test_single_flight_under_twenty_concurrent_calls`, `test_the_lock_is_released_after_a_failed_pass`, `test_the_lock_is_released_after_an_unexpected_failure`)
- **OPS-CONFIG-28.** The restarter reads each whole daemon answer within its own
  deadline, 10 seconds for the list and 60 seconds for each restart, and refuses an
  answer over 4 MiB; no call runs past one deadline for the whole pass,
  `PASS_DEADLINE_SECONDS`, a fixed 660 seconds however many containers match. A
  container not reached by then is reported as
  `Failed to restart <service>: PassDeadlineExceeded`, so a hung or slow daemon ends a
  pass within 660 seconds plus the time to write the answer; the config service waits
  720 seconds. \
  Enforced in: `infra/restarter/restarter.py` (`LIST_DEADLINE_SECONDS`, `RESTART_DEADLINE_SECONDS`, `PASS_DEADLINE_SECONDS`, `PassDeadlineExceeded`, `MAX_DOCKER_RESPONSE_BYTES`); `services/config/app/docker_ctl.py` (`RESTART_TIMEOUT_SECONDS`) \
  Pinned by: `infra/restarter/tests/test_restarter_docker.py` (`test_a_hung_daemon_times_out_within_the_deadline`, `test_a_trickling_daemon_is_cut_off_at_the_whole_answer_deadline`, `test_an_oversized_list_answer_is_refused`, `test_the_pass_deadline_is_a_fixed_constant_above_one_list_and_one_restart`, `test_the_whole_pass_ends_at_the_pass_deadline_however_many_containers_match`, `test_a_pass_deadline_shorter_than_the_list_deadline_bounds_the_list`); `tests/unit/test_restarter_compose_guard.py` (`test_config_client_outlasts_the_restarters_worst_case`)
- **OPS-CONFIG-29.** The config service runs as `appuser`. When it cannot write its data
  directory or one of its files (a volume an earlier root-run image created), startup
  logs one error line, `config data directory <dir> is not writable by uid <uid>; run:
  docker compose run --rm --no-deps --user root config chown -R <uid>:<gid> <dir>`, and
  skips the first-boot bootstrap. `GET /status` and `GET /schema` still answer 200;
  with no auth file in such a directory `/status` reports `password_changed` false and
  no password is generated, hashed, or logged. Login and the session reads work when
  the auth file exists. A write the directory refuses, and a login that would have to
  create the auth file, is 503 `The config data directory is not writable; the config
  service log names the one-time fix` with the remedy logged again, never a 500. \
  Enforced in: `services/config/Dockerfile` (`appuser`); `services/config/app/config_store.py` (`data_dir_writable`, `data_dir_remedy`, `DATA_DIR_NOT_WRITABLE_DETAIL`, `DataDirNotWritable`, `load_auth`, `is_password_changed`); `services/config/app/main.py` (`lifespan`, `data_dir_not_writable`) \
  Pinned by: `services/config/tests/test_data_dir.py` (`test_the_remedy_names_the_directory_the_uid_and_the_command`, `test_a_read_only_directory_is_not_writable`, `test_one_read_only_file_makes_the_directory_not_writable`, `test_startup_logs_one_error_line_and_skips_the_bootstrap`, `test_startup_on_a_writable_directory_logs_no_error_and_bootstraps`, `test_reads_are_served_from_a_read_only_directory`, `test_status_with_no_auth_file_on_a_read_only_directory_generates_nothing`, `test_a_refused_write_is_503_with_the_fixed_detail`, `test_a_first_login_that_cannot_write_the_auth_file_is_503`); `tests/unit/test_dockerfile_users.py` (`test_the_image_runs_as_a_non_root_user_unless_exempt`)
- **OPS-CONFIG-30.** No 422 the config service answers carries pydantic's `input` or
  `ctx`: each validation error is rendered as exactly `{type, loc, msg}`, the shape the
  config service's OpenAPI document advertises, and any lone surrogate in `loc` or `msg`
  is replaced with U+FFFD, so the body always encodes and the 422 never becomes a 500.
  So no 422 holds a submitted password, for every way a login or change-password body
  can fail (the field missing, misspelled, a number, a list, an object, a lone
  surrogate, a body that is not an object or not JSON): each error a password field
  raises has fixed text, and which strings may become the password is OPS-CONFIG-8's
  fixed-string 422. It is not a promise that no submitted text appears: `msg` is the
  validator's own text and could quote part of a value, and `loc` names the keys the
  caller sent. The handler is a standalone copy of the shared one every other service
  installs (OPS-VALID-1 to OPS-VALID-5), its code pinned identical to it, because the
  config service imports neither `herd_common` nor auth; before it, `{"password":
  ["\ud800"]}` on login and `{"np": "abcdefg\ud800"}` on change-password were 500s and
  every other refusal echoed its input. The copy carries the shared body check for a
  lone surrogate (OPS-VALID-6) too, but config installs it with
  `refuse_lone_surrogates=False`, by decision: its password answers below (OPS-CONFIG-8,
  a 401 at login) are its own, and its settings save refuses a surrogate itself
  (OPS-CONFIG-31). \
  Enforced in: `services/config/app/validation_errors.py` (`validation_error_handler`, `render_validation_errors`, `install_validation_errors`, `restrict_validation_error_schema`, `RENDERED_KEYS`); `services/config/app/main.py` (`app`) \
  Pinned by: `services/config/tests/test_validation_errors.py` (`test_the_handler_is_installed_for_request_validation_errors`, `test_the_handler_keeps_type_loc_msg_and_drops_input_ctx_and_everything_else`, `test_the_handler_replaces_a_lone_surrogate_in_loc_and_msg_and_keeps_integers`, `test_the_handler_keeps_honest_non_ascii_text_unchanged`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_every_rendered_error_has_exactly_the_advertised_keys`, `test_login_never_echoes_the_password`, `test_change_password_never_echoes_the_password`, `test_save_settings_never_echoes_a_secret_in_a_malformed_body`, `test_the_review_inputs_are_a_clean_422`, `test_the_code_is_identical_to_the_shared_module`)
- **OPS-CONFIG-31.** A settings save whose `values` hold a lone surrogate in any key or
  value, nested included, is refused first, before the unknown-key and required-field
  checks, with 422 `{"errors": ["Settings must be valid UTF-8 text"]}`, and writes
  nothing. Before it, a value was written to `config.json`, which every service reads
  its settings from, and an unknown key holding one made the unknown-key detail
  unencodable (a 500). Other non-ASCII text and a character outside the BMP are saved. \
  Enforced in: `services/config/app/main.py` (`update_settings`, `SETTINGS_NOT_UTF8_DETAIL`); `services/config/app/validation_errors.py` (`holds_lone_surrogate`) \
  Pinned by: `services/config/tests/test_config.py` (`test_a_lone_surrogate_is_the_fixed_422_and_writes_nothing`, `test_a_surrogate_pair_and_other_non_ascii_text_are_saved`)
- **OPS-CONFIG-32.** The config service keeps its last restart in memory, and
  `GET /apply/last` (a config session) answers it as `{id, state, result, finished_at}`.
  `state` is `none`, with `id`, `result`, and `finished_at` null, until the first
  restart this process starts; `running`, with a new random 32-character lowercase hex
  `id` and `result` and `finished_at` null, from the moment a `POST /apply` has passed
  its session, rotation, and configured checks; and `done` once the restart call
  returns, with `result` exactly what that `POST /apply` answers (OPS-CONFIG-24) and
  `finished_at` the UTC time the worker recorded it, ISO 8601 to the second with its
  `+00:00` offset (`2026-11-27T16:51:23+00:00`). The worker thread that runs the restart
  records the result itself, so it is kept when the apply request is cut off
  mid-restart; a restart call that raises is recorded as `The restart ended without a
  result; check service health` and the apply answers 500. A refused apply (401, 403,
  or 400) leaves the record unchanged. While a run is `running`, another `POST /apply`
  answers 200 `{"restarted": [], "errors": ["A restart is already in progress"]}`,
  makes no call to the restarter, and leaves the record unchanged. The record is per
  process: a restart of the config container itself forgets it. Before issue #1433
  the result existed only in the answer to the one `POST /apply`. \
  Enforced in: `services/config/app/restart_record.py` (`RestartRecord`, `RECORD`, `RESULT_LOST_ERROR`, `STATE_NONE`, `STATE_RUNNING`, `STATE_DONE`, `utc_now`); `services/config/app/main.py` (`apply_config`, `_restart_and_record`, `last_apply`) \
  Pinned by: `services/config/tests/test_restart_record.py` (`test_a_new_record_holds_no_run`, `test_begin_records_a_running_run_with_a_random_hex_id`, `test_begin_while_a_run_is_running_is_refused_and_keeps_that_run`, `test_finish_records_the_result_and_its_time_and_frees_the_next_run`, `test_finished_at_is_the_clock_at_finish_in_utc_with_its_offset`, `test_the_default_clock_is_utc_and_parses_back`, `test_every_run_gets_a_distinct_id`, `test_finish_with_a_stale_id_changes_nothing`, `test_a_second_finish_of_the_same_run_changes_nothing`, `test_the_snapshot_and_the_stored_result_are_copies`, `test_the_lost_result_wording`, `test_restart_record_imports_only_the_standard_library`); `services/config/tests/test_docker_ctl.py` (`test_last_is_no_run_before_any_restart`, `test_last_needs_a_session`, `test_last_holds_the_finished_result_and_each_run_has_a_new_id`, `test_last_records_an_error_result_as_answered`, `test_a_refused_apply_leaves_the_record`, `test_last_is_running_during_the_restart_and_a_second_apply_is_refused`, `test_the_result_is_recorded_when_the_request_is_cut_off`, `test_a_restart_that_raises_is_recorded_as_lost_and_frees_the_next`)
- **OPS-CONFIG-33.** The config page judges Save and Restart by whether the restart
  finished, not by whether its one `POST /apply` got an answer. Once the save succeeds
  it removes any result panel from the page, so an earlier run's result never stands as
  this click's; a refused save leaves the page as it was. It then reads
  `GET /apply/last` for the current `id` (when that read fails it sends no apply and
  says `The restart did not start; try Save and Restart again`) and sends
  `POST /apply`. An answer from the config service decides: a 200 is shown as
  OPS-CONFIG-19 says, any other status is `Failed to save or restart`. A lost answer is
  not a failure: no response at all (a restart on a Linux Docker host aborts the
  requests of a browser on that host with `ERR_NETWORK_CHANGED`), or a 502, 503, or 504
  whose body has no string `detail` (every 503 the config service itself sends carries
  one). The page then reads `GET /apply/last` every 2 seconds and shows the result of
  the first run whose `id` differs from the one read before, once it is `done`, exactly
  as an answered result. Each read of `GET /apply/last` is given up and aborted after 10
  seconds; a read given up that way, or otherwise lost, is retried. The wait ends with
  `The restart did not start; try Save and Restart again` when a read that answered
  still shows no new run 10 seconds or more after the wait began, with `The connection
  dropped during the restart and its result could not be read; check service health`
  when the config service refuses a read, and with `The connection dropped during the
  restart and no result arrived in time; check service health` once 750 seconds have
  passed, at the end of the first read that ends after that (so within 762 seconds:
  at most one more 2 second pause and one 10 second read), above the
  config service's own 720 second wait (OPS-CONFIG-28). Save and Restart stays
  disabled, reading `Restarting...`, while the page waits, and a wait ends with no
  further read and no message when the editor goes away. When the editor opens it reads
  `GET /apply/last` once: a `done` run's result is shown with no toast under the
  heading `Last restart, finished <local YYYY-MM-DD HH:MM>`, its `finished_at` in the
  viewer's timezone, never under "Restart Result", which only this click's own run
  gets; a `running` one is waited for the same way and shown under the same kind of
  heading (ending with `The restart that was running did not report a result; check
  service health` when no result comes); nothing is shown for `none`, a failed read, or
  a read that answers after the editor went away; and once a Save and Restart has been
  clicked, the read on open neither shows nor follows anything, so one run is never
  reported twice. Before issue #1433 any failure of the one request was `Failed to save
  or restart` and nothing could show the result afterward. \
  Enforced in: `frontend/src/lib/configRestart.ts` (`answerWasLost`, `waitForRestart`, `readWithin`, `readOnOpen`, `LastRestartReadTimedOut`, `formatFinishedAt`, `lastRestartHeading`, `RESTART_RESULT_HEADING`, `RESTART_POLL_INTERVAL_MS`, `RESTART_NOT_STARTED_GRACE_MS`, `RESTART_WAIT_MS`, `RESTART_READ_TIMEOUT_MS`, `RESTART_NOT_STARTED_TEXT`, `RESTART_TIMED_OUT_TEXT`, `RESTART_UNREADABLE_TEXT`, `RESTART_FOLLOW_LOST_TEXT`); `frontend/src/api/config.ts` (`fetchLastRestart`); `frontend/src/pages/ConfigPage.tsx` (`handleSaveAndRestart`, `reportLostAnswer`, `awaitingRestart`, `clickStarted`, `setRestartPanel`) \
  Pinned by: `frontend/src/test/lib/configRestart.test.ts` (`answerWasLost (issue #1433)`, `waitForRestart (issue #1433)`, `readWithin and readOnOpen (issue #1433)`, `formatFinishedAt and lastRestartHeading (issue #1433)`); `frontend/src/test/pages/ConfigPage.test.tsx` (`ConfigPage Save and Restart outcome (issue #1433)`, `ConfigPage last restart on open (issue #1433)`); `frontend/src/test/api/config.test.tsx` (`fetchLastRestart reads GET /apply/last with the bearer token (issue #1433)`); `tests/e2e/test_config_playwright.py` (`test_config_save_and_restart_gated`)
- **OPS-CONFIG-34.** The config editor builds its form from the schema and fills it from
  the saved settings. When either read fails, it shows `Could not load the configuration
  schema.` or `Could not load the saved configuration.` with the server's reason, Retry,
  and Sign out, and renders no form, Save, or Save and Restart, so a save never writes a
  blank form over settings the page never read (issue #1320). An answer that is not the
  JSON the read expects (the web app's HTML page, a schema that is not a list, settings
  that are not an object) is such a failure. When a later refresh of either fails, the
  form stays and `Could not refresh ...` with Retry shows above it. \
  Enforced in: `frontend/src/pages/ConfigPage.tsx` (`ConfigEditor`) \
  Pinned by: `frontend/src/test/pages/pageLoadFailureForms.test.tsx` (`Retry loads the saved values, not the schema defaults`, `ConfigPage: settings answered with HTML show the alert and no Save`, `keeps the record on screen and says the refresh failed, with Retry`)

**Out of scope.** The superadmin seed that reads `SUPERADMIN_*` (`identity-and-access.md`,
IAM-BOOT-1 to IAM-BOOT-5); the meaning of each field (each area's Configuration section).

### 8.7 Schema lifecycle

**What it does.** A brand-new database gets its tables at first boot and is stamped so
later migrations apply cleanly; an existing, migration-managed schema is never touched
at boot, and the service says in its log what an operator must run.

**Surfaces.** `services/common/herd_common/schema_init.py` and
`services/common/herd_common/consumer_schema_gate.py`, called from each database
service's lifespan; `infra/postgres/init.sql`; the upgrade order in
[OPERATIONS.md](../OPERATIONS.md) (Upgrade path).

**Rules.**

- **OPS-SCHEMA-1.** The schema is classified from its state before anything is created:
  a schema with an `alembic_version` stamp is managed whatever tables it has; with no
  stamp, no model tables means fresh and some model tables means legacy. \
  Enforced in: `services/common/herd_common/schema_init.py` (`decide_schema_action`, `_inspect_state`) \
  Pinned by: `services/common/tests/test_schema_init.py` (`test_decide_fresh_stamps`, `test_decide_legacy_unstamped`, `test_decide_already_managed_when_stamped`)
- **OPS-SCHEMA-2.** A fresh schema gets every model table and is stamped at the
  migration head; a chain with no revisions leaves it unstamped, with a warning. \
  Enforced in: `services/common/herd_common/schema_init.py` (`create_all_and_stamp`, `_stamp_head`) \
  Pinned by: `services/common/tests/test_schema_init.py` (`test_fresh_schema_stamps_head`, `test_fresh_schema_without_revisions_is_left_unstamped`)
- **OPS-SCHEMA-3.** A managed schema gets no table created and no stamp written at boot;
  its new tables appear only through `make migrate`. By decision; issue #419. \
  Enforced in: `services/common/herd_common/schema_init.py` (`create_all_and_stamp`) \
  Pinned by: `services/common/tests/test_schema_init.py` (`test_already_stamped_schema_is_untouched`, `test_stamped_schema_gets_no_ghost_tables`, `test_upgrade_in_place_unguarded_migration_applies_cleanly`)
- **OPS-SCHEMA-4.** A legacy schema still gets missing tables created, is not stamped,
  and logs a warning that `make migrate` will fail and the volume must be recreated. \
  Enforced in: `services/common/herd_common/schema_init.py` (`create_all_and_stamp`) \
  Pinned by: `services/common/tests/test_schema_init.py` (`test_legacy_unstamped_tables_warn_and_do_not_stamp`, `test_legacy_volume_still_gets_new_tables`)
- **OPS-SCHEMA-5.** On a managed schema the boot logs one advisory warning when the stamp
  is behind the head (naming both and `make migrate`), when the stamp is not in this
  build's chain (an image rollback), when the stamp is at the head but model tables are
  missing (a missing migration), or when the stamp exists with no model table at all; a
  managed schema at the head with every table logs nothing. \
  Enforced in: `services/common/herd_common/schema_init.py` (`_log_managed_schema_drift`) \
  Pinned by: `services/common/tests/test_schema_init.py` (`test_upgrade_in_place_unguarded_migration_applies_cleanly`, `test_stamp_ahead_of_build_head_warns_rollback`, `test_stamped_at_head_missing_table_warns_missing_migration`, `test_stamped_empty_schema_warns_stamp_mismatch`, `test_managed_steady_state_is_quiet`)
- **OPS-SCHEMA-6.** The drift check never blocks boot: an unreadable migration directory
  is a warning, and the missing tables are still reported to the caller. \
  Enforced in: `services/common/herd_common/schema_init.py` (`_log_managed_schema_drift`, `SchemaInitOutcome`) \
  Pinned by: `services/common/tests/test_schema_init.py` (`test_managed_path_survives_broken_script_location`, `test_broken_script_location_still_surfaces_missing_tables`, `test_managed_missing_tables_surfaced_in_outcome`)
- **OPS-SCHEMA-7.** When a managed schema lacks model tables, an event consumer's start is
  deferred: the rest of the service starts, a poll every 5 seconds re-checks the tables
  (warning every 60 seconds, retrying a failed check), and the consumer starts as soon as
  the tables exist, with no restart. \
  Enforced in: `services/common/herd_common/consumer_schema_gate.py` (`start_consumer_when_schema_ready`); `services/common/herd_common/schema_init.py` (`missing_model_tables`) \
  Pinned by: `services/common/tests/test_consumer_schema_gate.py` (`test_managed_schema_with_no_drift_starts_immediately`, `test_fresh_and_legacy_outcomes_never_gate`, `test_gated_defers_then_starts_when_table_appears`, `test_gated_warns_periodically_while_waiting`, `test_readiness_check_failure_is_retried`); `services/common/tests/test_schema_init.py` (`test_missing_model_tables_recheck_clears_when_table_appears`)
- **OPS-SCHEMA-8.** Shutdown cancels a pending gate and waits for at most one in-flight
  readiness query to close its connection. \
  Enforced in: `services/common/herd_common/consumer_schema_gate.py` (`stop_consumer_schema_gate`) \
  Pinned by: `services/common/tests/test_consumer_schema_gate.py` (`test_stop_cancels_pending_gate`, `test_stop_is_noop_when_never_gated`, `test_cancel_mid_query_does_not_leak_the_connection`)
- **OPS-SCHEMA-9.** Service schemas are created by `infra/postgres/init.sql`, which runs
  only on an empty data volume; a new service's schema on an existing volume needs that
  volume recreated or the schema created by hand. The file names no role and no
  database: it runs as `POSTGRES_USER` connected to `POSTGRES_DB`, that role owns the
  schemas it creates, and the database search path is set through
  `current_database()`, so any `POSTGRES_USER` and `POSTGRES_DB` initialize (issue
  #1325). That the file runs only on an empty data volume is the postgres image's
  entrypoint behavior; no HERD test exercises it. The tests pin the file's text and,
  against a running stack, that it ran to its last statement. \
  Enforced in: `infra/postgres/init.sql` (`CREATE SCHEMA`, `current_database`) \
  Pinned by: `tests/unit/test_postgres_init.py` (`test_init_files_name_no_role_or_database`, `test_init_sql_still_creates_every_service_schema_and_sets_the_search_path`); `tests/integration/test_postgres_init_applied.py` (`test_init_script_set_the_database_search_path`, `test_init_script_schemas_exist_and_belong_to_the_connected_user`)

**Out of scope.** Each service's own migrations. Which consumers use the gate
(`provisioning-and-wiring.md`, WIRE-CONSUME-5, and `integration.md`).

### 8.8 Durable event delivery: the transactional outbox

**What it does.** A service that changes state and owes an event writes the event in the
same database transaction, and a background relay publishes it later, so a broker outage
delays an event but cannot lose one.

**Surfaces.** `services/common/herd_common/outbox.py`; each producer's `outbox` table and
relay task (reservations, started in `services/reservations/app/main.py`; execution,
`start_outbox_relay` in `services/execution/app/main.py`).

**Rules.**

- **OPS-OUTBOX-1.** `enqueue_event` adds the outbox row to the caller's session without
  committing, so the event exists if and only if the caller's transaction commits. \
  Enforced in: `services/common/herd_common/outbox.py` (`enqueue_event`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_enqueue_does_not_commit`)
- **OPS-OUTBOX-2.** The row id is stamped into the payload as `event_id` (a given id is
  honored) and is the stable key consumers deduplicate on. \
  Enforced in: `services/common/herd_common/outbox.py` (`enqueue_event`, `EVENT_ID_FIELD`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_enqueue_stamps_event_id_into_payload`, `test_enqueue_honors_explicit_event_id`)
- **OPS-OUTBOX-3.** On Postgres, `enqueue_event` also issues `pg_notify` on the channel
  `herd_outbox_<schema>` in the same transaction, so the wake arrives only after commit;
  on any other database it adds the row only. \
  Enforced in: `services/common/herd_common/outbox.py` (`enqueue_event`, `outbox_channel`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_enqueue_event_postgresql_issues_pg_notify_on_same_session`, `test_enqueue_event_sqlite_only_adds_no_pg_notify`, `test_outbox_channel_derives_from_table_schema`, `test_outbox_channel_defaults_to_public_with_no_schema`)
- **OPS-OUTBOX-4.** The relay claims unpublished rows oldest first, one at a time, with
  `FOR UPDATE SKIP LOCKED`, up to `OUTBOX_BATCH_SIZE` per pass, so two relays never
  publish one row. \
  Enforced in: `services/common/herd_common/outbox.py` (`_publish_pending`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_publish_respects_batch_size`, `test_publish_is_idempotent_skips_already_published`)
- **OPS-OUTBOX-5.** Each publish carries `Nats-Msg-Id` set to the row id, and the row is
  marked published only after JetStream acknowledges it within 10 seconds. \
  Enforced in: `services/common/herd_common/outbox.py` (`_publish_pending`, `NATS_MSG_ID_HEADER`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_publish_marks_rows_and_sets_msg_id_header`)
- **OPS-OUTBOX-6.** A failed or timed-out publish counts an attempt, leaves the row
  unpublished, and ends the pass. \
  Enforced in: `services/common/herd_common/outbox.py` (`_publish_pending`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_publish_failure_leaves_row_unpublished_and_stops_batch`)
- **OPS-OUTBOX-7.** While the broker client is missing or disconnected the relay
  publishes nothing and retries at the base tick; a failed pass backs off by doubling to
  a cap of the larger of ten ticks and 300 seconds, and a healthy pass resets it. \
  Enforced in: `services/common/herd_common/outbox.py` (`run_outbox_relay`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_relay_backoff_doubles_on_failure_and_resets_on_success`, `test_relay_backoff_caps_at_max`); `tests/integration/test_outbox_durability.py` (`test_outbox_survives_nats_outage`)
- **OPS-OUTBOX-8.** Once per `prune_every_seconds` (an hour) a healthy pass deletes
  published rows older than `OUTBOX_RETENTION_SECONDS`; unpublished rows are never
  pruned. \
  Enforced in: `services/common/herd_common/outbox.py` (`run_outbox_relay`, `prune_published`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_prune_removes_only_old_published_rows`, `test_relay_prune_gate_runs_once_interval_elapsed`, `test_relay_prune_gate_skips_before_interval_elapses`)
- **OPS-OUTBOX-9.** With `OUTBOX_WAKE_ON_WRITE` on and a Postgres engine, a listener on a
  dedicated connection wakes the relay on every notification, so a healthy relay drains a
  committed row at once instead of at the next tick; the tick stays the fallback. \
  Enforced in: `services/common/herd_common/outbox.py` (`run_outbox_relay`, `_listen_for_wakeups`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_relay_wake_seam_drains_immediately_after_wake_set`, `test_relay_starts_and_cancels_listener_for_postgres_engine`, `test_relay_never_starts_listener_without_a_postgres_engine`); `services/common/tests/test_outbox_wake_live_pg.py` (`test_wake_on_write_publishes_within_200ms_of_commit`, `test_wake_on_write_disabled_does_not_publish_within_1s`)
- **OPS-OUTBOX-10.** The wake is cleared before each drain, so a notification that arrives
  during a drain triggers the next drain at once; a full batch drains again with no wait;
  a wake is ignored while the relay waits out a broker outage or a failure backoff. \
  Enforced in: `services/common/herd_common/outbox.py` (`run_outbox_relay`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_relay_wake_set_during_drain_causes_immediate_next_drain`, `test_relay_saturated_batch_drains_remainder_without_waiting_a_tick`, `test_relay_wake_ignored_during_backoff`)
- **OPS-OUTBOX-11.** The listener retries a failed connect or a lost connection after one
  tick, closes the connection it opened on every exit, sets the wake once per successful
  registration (catching up rows committed while it was away), and connects with the
  engine's own driver arguments. A failure to build those arguments (or to import the
  driver) is not retried: it is logged once at ERROR with `action`
  `outbox_listener_setup_failed` and the exception class name only, and the listener
  returns, leaving the relay on its tick. A listener task that ends with an `Exception`,
  or a `BaseException` subclass that asyncio does not treat as a process-exit signal, is
  logged when it ends (OPS-OUTBOX-13) and again, by class name, when the relay's shutdown
  awaits it through `await_stopped_task` (a `KeyboardInterrupt` or `SystemExit` leaves the
  event loop as the listener dies, OPS-OUTBOX-13); the relay, when cancelled, still ends with `CancelledError`, never
  the listener's exception. A `GeneratorExit` thrown into the relay while it awaits the
  listener (the relay coroutine closed there) is not the listener's and propagates at once
  (issue #1549). \
  Enforced in: `services/common/herd_common/outbox.py` (`_listen_for_wakeups`, `run_outbox_relay`); `services/common/herd_common/background_tasks.py` (`await_stopped_task`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_listen_for_wakeups_retries_then_wakes_on_connect_and_notify`, `test_listen_for_wakeups_termination_closes_conn_then_backs_off_before_reconnect`, `test_listen_for_wakeups_registration_failure_closes_conn_and_leaks_none`, `test_listen_for_wakeups_dsn_from_dialect_connect_args`, `test_listen_for_wakeups_setup_failure_is_logged_once_and_returns`, `test_relay_with_a_listener_whose_setup_fails_ends_cancelled`, `test_relay_logs_a_listener_that_dies_and_still_ends_cancelled`, `test_closing_the_relay_while_it_awaits_its_listener_lets_generator_exit_through`, `test_a_relay_that_ends_with_its_own_exception_keeps_it_after_cancelling_its_listener`)
- **OPS-OUTBOX-12.** A consumer's deduplication key is the payload `event_id`, else the
  JetStream `<stream>:<sequence>`, else none. \
  Enforced in: `services/common/herd_common/outbox.py` (`event_dedupe_key`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_dedupe_key_prefers_payload_event_id`, `test_dedupe_key_falls_back_to_stream_sequence`, `test_dedupe_key_none_when_no_id_and_no_metadata`, `test_dedupe_key_stable_across_resequence`)
- **OPS-OUTBOX-13.** The relay task is cancelled at shutdown and a crash of it is logged
  when it happens. Reservations logs a crash of any of its three lifespan tasks (the
  expiration loop, the purpose-classify loop, and the relay), and every relay logs a
  crash of its wake-up listener, through the ONE shared done callback `log_task_crash`:
  at ERROR, with `action` `background_task_crashed`, the task's name, and the exception
  class name only. At shutdown reservations cancels all three tasks before it awaits any,
  then awaits each in that order through `await_stopped_task`, which returns, never
  raises, what a task ended with: an `Exception`, or a `BaseException` subclass that
  asyncio does not treat as a process-exit signal, is logged (`action`
  `background_task_failed_at_shutdown`, class name only) and does not stop the rest, and
  the NATS connection, when there is one, is closed after; the lifespan raises none of
  them. A `KeyboardInterrupt` or `SystemExit` never reaches these steps: asyncio raises it
  out of the event loop as the task dies, `asyncio.run` then cancels the lifespan at its
  `yield`, so in that case none of the shutdown steps run (the NATS close included), and
  the signal leaves `asyncio.run`. A `GeneratorExit` thrown into the shutdown while it
  awaits a task (the lifespan closed while suspended there) propagates at once. When the
  lifespan itself is cancelled while it awaits a task, the remaining steps still run and
  the lifespan then ends cancelled. Execution's relay task keeps its own callback (issue #1549 leaves
  moving it to the shared one for later). Against a running stack, an event staged by
  reservations reaches its consumers. \
  Enforced in: `services/execution/app/main.py` (`start_outbox_relay`, `stop_outbox_relay`); `services/reservations/app/main.py` (`lifespan`); `services/common/herd_common/background_tasks.py` (`log_task_crash`, `await_stopped_task`, `TASK_CRASHED_ACTION`, `TASK_FAILED_AT_SHUTDOWN_ACTION`) \
  Pinned by: `services/execution/tests/test_main.py` (`test_start_outbox_relay_starts_task_and_stop_cancels`, `test_stop_outbox_relay_noop_when_never_started`); `services/common/tests/test_outbox.py` (`test_relay_cancelled_error_during_sleep_propagates`, `test_relay_logs_a_listener_that_dies_and_still_ends_cancelled`); `services/common/tests/test_background_tasks.py` (`test_a_task_that_raises_is_logged_when_it_ends_by_class_name_only`, `test_a_task_that_is_cancelled_or_returns_is_not_logged`, `test_await_stopped_task_returns_none_for_a_cancelled_or_returned_task`, `test_await_stopped_task_returns_a_dead_tasks_exception_and_logs_it_once`, `test_an_exception_thrown_into_the_caller_propagates_even_after_the_task_died`); `services/reservations/tests/test_main_task_lifecycle.py` (`test_a_task_that_dies_is_logged_at_once_and_shutdown_still_runs_every_step`, `test_shutdown_cancels_and_awaits_every_task_the_lifespan_starts`, `test_a_process_exit_signal_in_a_task_cancels_the_lifespan_at_its_yield`, `test_closing_the_lifespan_while_it_awaits_a_task_lets_generator_exit_through`, `test_a_lifespan_cancelled_during_shutdown_finishes_the_steps_then_ends_cancelled`); `tests/integration/test_outbox_durability.py` (`test_outbox_delivers_reservation_event_end_to_end`)
- **OPS-OUTBOX-14.** A row is marked published when JetStream acknowledges it, and is not
  republished; an event the broker loses afterwards (an ephemeral store recreated before a
  consumer pulled it) is not recovered by the relay. By decision; recorded in
  [OPERATIONS.md](../OPERATIONS.md) (JetStream durability). \
  Enforced in: `services/common/herd_common/outbox.py` (`_publish_pending`) \
  Pinned by: none

**Out of scope.** Which events each producer stages and when (`reservations.md`,
RES-EVENT-1 to RES-EVENT-3, and OPS-POLL-11 here); consumer behavior.

### 8.9 Shared JetStream rules

**What it does.** Every stream, every durable consumer, every retry delay, and every
in-progress heartbeat is declared through one shared module, so a configuration change
reaches a stack that keeps its broker state and no consumer drifts from the others.

**Surfaces.** `services/common/herd_common/jetstream.py`; the stream declarations in
`services/reservations/app/main.py` (`HERD_RESERVATIONS`) and
`services/execution/app/main.py` (`HERD_HEALTH`, `HERD_DLQ`); the `nats` service in
`docker-compose.yml` and `docker-compose.override.yml`; the DLQ commands in
[OPERATIONS.md](../OPERATIONS.md) (Inspecting the NATS DLQ).

**Rules.**

- **OPS-NATS-1.** Each service that uses NATS connects through `connect_nats`: the first
  connection is tried at most `NATS_INITIAL_CONNECT_ATTEMPTS` (5) more times, 2 seconds
  apart, and then raises, so a service whose broker is down at boot logs the failure and
  finishes starting without NATS (about 10 seconds for a refused connection). Once a
  connection is established the reconnect cap is unlimited, so a broker restart never
  closes it. A service that started without NATS does not connect later: it stays
  without the broker until it is restarted. \
  Enforced in: `services/common/herd_common/jetstream.py` (`connect_nats`, `NATS_INITIAL_CONNECT_ATTEMPTS`); `services/reservations/app/main.py` (`lifespan`); `services/execution/app/services/nats_consumer.py` (`start_nats_consumer`); `services/notifications/app/services/nats_consumer.py` (`start_nats_consumer`); `services/integration/app/services/nats_consumer.py` (`start_nats_consumer`) \
  Pinned by: `services/common/tests/test_nats_connect.py` (`test_initial_connect_to_a_down_broker_raises_instead_of_hanging`, `test_established_connection_reconnects_past_the_initial_bound`); `services/reservations/tests/test_main_task_lifecycle.py` (`test_lifespan_starts_without_nats_using_the_real_client`); `services/execution/tests/test_nats_connect_real_client.py` (`test_start_nats_consumer_returns_when_broker_is_down`)
- **OPS-NATS-2.** The owner of a stream declares it with `ensure_stream`: add, and on the
  server's stream-name-in-use error (code 10058) update to the same config; any other
  error propagates. \
  Enforced in: `services/common/herd_common/jetstream.py` (`ensure_stream`, `JS_STREAM_NAME_IN_USE`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_fresh_create_calls_add_stream_once_and_never_update_stream`, `test_identical_config_add_stream_succeeds_is_a_noop_for_update`, `test_stream_name_in_use_error_triggers_exactly_one_update_stream_with_same_config`, `test_bad_request_error_with_different_err_code_propagates`, `test_non_bad_request_error_propagates`)
- **OPS-NATS-3.** `max_age` comes from `NATS_STREAM_MAX_AGE_SECONDS`; 0 or none means no
  retention cap. \
  Enforced in: `services/common/herd_common/jetstream.py` (`ensure_stream`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_max_age_seconds_none_yields_config_with_no_max_age`, `test_max_age_seconds_zero_yields_config_with_no_max_age`)
- **OPS-NATS-4.** Execution owns `HERD_HEALTH` (`herd.health.*`) and declares it with
  `ensure_stream` once its consumer has connected (also when the consumer's durable
  setup then failed, since the connection is kept); a failed declaration is logged with
  the exception class name only and the service keeps running (issue #1418): `JetStream
  stream HERD_HEALTH could not be created or updated (<class>); the connection is kept,
  so health events publish if the stream exists, and while it is missing they wait in
  the outbox and the relay retries them every tick`. \
  Enforced in: `services/execution/app/main.py` (`_ensure_health_stream`, `_start_consumer_and_streams`) \
  Pinned by: `services/execution/tests/test_main.py` (`test_lifespan_starts_consumer_immediately_when_schema_ready`, `test_ensure_health_stream_failure_says_health_events_wait_in_the_outbox`); `services/execution/tests/test_nats_consumer_full.py` (`test_setup_failure_keeps_the_connection_for_the_stream_ensures`)
- **OPS-NATS-15.** Reservations owns `HERD_RESERVATIONS` (`herd.reservations.*`) and
  declares it with `ensure_stream` right after connecting; a failed declaration is logged
  and the service keeps running with the connection it has. \
  Enforced in: `services/reservations/app/main.py` (`lifespan`) \
  Pinned by: none
- **OPS-NATS-5.** A service that only consumes a stream checks it with `stream_info` and
  creates it, with no `max_age`, only on not-found; it never updates an existing stream's
  config, and any other error propagates. \
  Enforced in: `services/common/herd_common/jetstream.py` (`ensure_stream_exists`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_ensure_stream_exists_existing_stream_never_calls_add_stream`, `test_ensure_stream_exists_not_found_triggers_one_add_stream_with_no_max_age`, `test_ensure_stream_exists_other_stream_info_error_propagates`, `test_ensure_stream_exists_add_stream_failure_propagates`)
- **OPS-NATS-6.** Every durable is created or updated with `add_consumer` before it is
  bound, so a changed consumer config reaches a durable that already exists on a kept
  store; the helper names the durable and fills the filter subject only when none is set. \
  Enforced in: `services/common/herd_common/jetstream.py` (`ensure_consumer`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_ensure_consumer_sets_name_durable_name_filter_subject_and_calls_add_consumer`, `test_ensure_consumer_does_not_overwrite_an_already_set_filter_subject`, `test_ensure_consumer_tolerates_a_config_double_missing_filter_subject_attrs`); `services/execution/tests/test_nats_consumer_full.py` (`test_start_nats_consumer_success`); `services/notifications/tests/test_nats_consumer.py` (`test_start_nats_consumer_wires_both_subscriptions`); `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_start_nats_consumer_wires_both_subscriptions`)
- **OPS-NATS-7.** No durable carries `backoff`; against a running stack all five durables
  report the configured `ack_wait` and no `backoff`. By decision; issue #895. \
  Enforced in: `services/execution/app/services/nats_consumer.py` (`start_nats_consumer`) \
  Pinned by: `tests/integration/test_nats_consumer_configs_live.py` (`test_consumers_have_real_ack_wait_and_no_backoff`)
- **OPS-NATS-8.** A NAK's delay for delivery n is entry n of the schedule, clamped to the
  last entry; a missing or non-positive count takes the first entry, and an empty
  schedule raises. \
  Enforced in: `services/common/herd_common/jetstream.py` (`nak_delay`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_nak_delay_maps_each_delivery_to_its_schedule_entry`, `test_nak_delay_clamps_num_delivered_past_schedule_length`, `test_nak_delay_falls_back_to_first_entry_for_non_positive_or_missing`, `test_nak_delay_empty_schedule_raises`)
- **OPS-NATS-9.** `NATS_NAK_BACKOFF_SECONDS` is a comma-separated list of non-negative
  integers (default `1,5,15,60,120`); an empty list, an empty entry, a non-integer, or a
  negative entry refuses to load, naming the entry. \
  Enforced in: `services/common/herd_common/jetstream.py` (`parse_nak_backoff_schedule`); `services/execution/app/config.py` (`_validate_nak_backoff_schedule`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_parse_nak_backoff_schedule_from_comma_string`, `test_parse_nak_backoff_schedule_strips_whitespace`, `test_parse_nak_backoff_schedule_rejects_empty_string`, `test_parse_nak_backoff_schedule_rejects_empty_entry`, `test_parse_nak_backoff_schedule_rejects_non_integer_entry`, `test_parse_nak_backoff_schedule_rejects_negative_entry`)
- **OPS-NATS-10.** `NATS_ACK_WAIT_SECONDS` below 2 refuses to load, and the in-progress
  heartbeat runs at exactly half of it, by true division. \
  Enforced in: `services/common/herd_common/jetstream.py` (`validate_ack_wait_seconds`, `MIN_ACK_WAIT_SECONDS`, `heartbeat_interval`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_validate_ack_wait_accepts_the_minimum_and_above`, `test_validate_ack_wait_refuses_below_the_minimum_with_pinned_wording`, `test_heartbeat_interval_is_exactly_half_of_ack_wait`, `test_heartbeat_interval_never_floors_to_zero_at_the_minimum_ack_wait`)
- **OPS-NATS-11.** A fetched batch is processed in order while every unsettled message,
  the running one and any queued behind it, gets `in_progress` each interval; a message
  leaves the heartbeat when its handler returns or raises, a raising handler does not stop
  the batch, a failed heartbeat is ignored, and the heartbeat task is always cancelled
  and awaited, letting a shutdown cancel propagate. \
  Enforced in: `services/common/herd_common/jetstream.py` (`process_batch_with_heartbeat`, `keep_messages_alive`) \
  Pinned by: `services/common/tests/test_jetstream.py` (`test_keep_messages_alive_heartbeats_until_settled_or_cancelled`, `test_keep_messages_alive_swallows_in_progress_errors`, `test_process_batch_settles_in_order_and_survives_a_raising_message`, `test_process_batch_cancelled_mid_message_leaks_no_task`, `test_process_batch_cancel_while_stopping_heartbeat_still_propagates`)
- **OPS-NATS-12.** Every module that calls `pull_subscribe(` runs its batches through
  `process_batch_with_heartbeat`, keeps no heartbeat of its own, and takes its ack wait
  from settings and its heartbeat from `heartbeat_interval`. The repository guard
  refuses an own heartbeat by an AST scan: any call to `keep_messages_alive` under its
  own name, `_keep_messages_alive`, an import alias, or a module attribute, and any
  `.in_progress(` call (what a hand-rolled heartbeat loop must make); a mention in a
  comment or docstring is not a call and does not count (#1145). The same scan requires
  a call to the shared `process_batch_with_heartbeat` (own name, import alias, or module
  attribute; an import, a comment, or a local function of that name is not one), an
  interval that is `heartbeat_interval` of `settings.nats_ack_wait_seconds`, directly or
  through names their scope binds exactly once (a rebinding from a nested function
  through `global` or `nonlocal` counts as a second binding), at least one place in the
  module that sets `ack_wait`, and that same configured value at every `ack_wait`
  setting the scan reads (a call keyword, a key of a call's last `**{...}` dict literal,
  or an assignment to an `.ack_wait` attribute), with `ack_wait` present on every
  `ConsumerConfig` call; an own fraction, a literal, a cast, or a reassigned name fails.
  The scan follows names within one module and does not judge whether the code is
  reached; the guard's module docstring lists those limits and the settings of
  `ack_wait` it does not read (a tuple, `for`, or `with` target, a `**` dict that is not
  a call's last, a config built from a dict by `ConsumerConfig.from_response`) (#1158). \
  Enforced in: `services/common/herd_common/jetstream.py` (`process_batch_with_heartbeat`, `heartbeat_interval`) \
  Pinned by: `tests/unit/test_consumer_heartbeat_wiring.py` (`test_consumer_modules_are_discovered`, `test_no_pull_consumer_module_keeps_an_inline_heartbeat`, `test_every_pull_consumer_module_uses_the_shared_heartbeat`, `test_every_pull_consumer_module_takes_ack_wait_and_heartbeat_from_one_source`)
- **OPS-NATS-13.** Under `make prod` the broker stores JetStream state in the `nats-data`
  volume (`-sd /data`), so streams, unconsumed events, dead letters, and consumer
  positions survive a recreate; the dev and gate stacks point the store at an unmounted
  directory, so every recreate starts every stream empty. By decision; issue #620. \
  Enforced in: `docker-compose.yml` (`nats-data`); `docker-compose.override.yml` (`nats-ephemeral`) \
  Pinned by: none
- **OPS-NATS-14.** The `nats` image carries no CLI; operator commands run from a
  `natsio/nats-box` container on the stack network, and no tracked document runs the
  CLI inside the `nats` service. \
  Enforced in: `docs/OPERATIONS.md` (`nats-box`) \
  Pinned by: `tests/unit/test_docs_nats_cli_form.py` (`test_no_tracked_doc_runs_the_nats_cli_inside_the_nats_service`, `test_nats_service_image_is_the_cli_less_server_image`, `test_operations_dlq_section_uses_nats_box`)

**Out of scope.** Each consumer's own subjects, `max_deliver`, dead-letter routing, and
use of the schedule (`provisioning-and-wiring.md`, WIRE-CONSUME-1 to WIRE-CONSUME-15, and
`integration.md`); broker authentication, which does not exist (OPS-GUARD-3).

### 8.10 Device health polling

**What it does.** An admin who gives a device or its template a poll interval gets that
device polled in the background: the execution service logs in, asks for status, logs
out, records the result, backs off from a device that keeps failing, and announces when a
device goes bad or recovers. Devices under a live reservation can be polled on a
different cadence from idle ones.

**Surfaces.** `services/execution/app/services/health_scheduler.py`, started from
`services/execution/app/main.py`; the inventory registry route (`inventory.md`,
INV-POLL-3); the event in section 6.

**Rules.**

- **OPS-POLL-1.** The scheduler runs when `HEALTH_POLL_SCHEDULER_ENABLED` is on, one tick
  every `HEALTH_POLL_SCHEDULER_TICK_SECONDS`; a failed tick backs off by doubling to the
  larger of ten ticks and 300 seconds, a healthy tick resets it, and a crash of the task
  is logged. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`start_health_scheduler`, `run_health_scheduler_loop`) \
  Pinned by: `services/execution/tests/test_health_scheduler_registry.py` (`test_start_health_scheduler_disabled_is_noop`, `test_start_and_stop_health_scheduler_roundtrip`, `test_loop_tick_failure_backs_off`, `test_start_health_scheduler_surfaces_crash`, `test_loop_cancelled_mid_tick_exits`)
- **OPS-POLL-2.** The registry of devices to poll is read from inventory at most once per
  `HEALTH_POLL_REGISTRY_REFRESH_SECONDS`; no internal token, a transport error, a
  non-200, or a non-JSON body keeps the previous registry, and a row without a usable id
  or interval is skipped. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_fetch_registry`, `_refresh_registry_if_due`) \
  Pinned by: `services/execution/tests/test_health_scheduler_registry.py` (`test_fetch_registry_no_token_returns_none`, `test_fetch_registry_transport_error_returns_none`, `test_fetch_registry_non_200_returns_none`, `test_fetch_registry_malformed_json_returns_none`, `test_fetch_registry_parses_rows_and_skips_bad_ones`, `test_refresh_registry_skips_when_not_due`, `test_refresh_registry_fetch_failure_leaves_cache`)
- **OPS-POLL-3.** A refreshed registry seeds an `UNKNOWN` status row, due now, for every
  device that has none, and leaves existing rows as they are. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_seed_missing_status_rows`) \
  Pinned by: `services/execution/tests/test_health_scheduler_registry.py` (`test_refresh_registry_success_updates_cache_and_seeds`, `test_seed_missing_status_rows_inserts_new_and_skips_existing`, `test_seed_missing_status_rows_falls_back_to_per_row`)
- **OPS-POLL-4.** A tick considers at most `HEALTH_POLL_BATCH_SIZE` due rows, oldest due
  first, locked with `FOR UPDATE SKIP LOCKED`; a due row whose device left the registry
  is pushed one refresh period ahead and not polled. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_due_rows`, `run_tick`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_due_rows_returns_only_rows_in_the_past`); `services/execution/tests/test_health_scheduler_scale.py` (`test_run_tick_claims_at_most_batch_size`, `test_run_tick_skips_device_dropped_from_registry`); `services/execution/tests/test_health_scheduler_registry.py` (`test_loop_pushes_forward_device_dropped_from_registry`)
- **OPS-POLL-5.** A poll first claims its row with a conditional update that moves
  `next_poll_at` five minutes ahead only while the row is still due; a lost claim skips
  the poll, so concurrent ticks and replicas poll each device once. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_claim_row`, `CLAIM_WINDOW`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_claim_row_wins_when_still_due`, `test_claim_row_loses_when_already_claimed`); `services/execution/tests/test_health_scheduler_scale.py` (`test_sequential_ticks_do_not_double_poll`, `test_concurrent_ticks_claim_each_row_exactly_once`); `services/execution/tests/test_health_scheduler_registry.py` (`test_loop_skips_when_claim_lost`)
- **OPS-POLL-6.** At most `HEALTH_POLL_MAX_CONCURRENCY` polls run at once in a replica,
  the claim is taken only after a slot is free, and the tick waits for all its polls; a
  setting above the default thread pool size logs a warning at start. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`run_tick`, `_warn_if_concurrency_exceeds_pool`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_run_tick_bounds_concurrency_to_k`, `test_run_tick_default_concurrency_is_sequential`, `test_concurrency_over_thread_pool_warns`, `test_concurrency_at_pool_size_does_not_warn`)
- **OPS-POLL-7.** A poll reads the device and its template, then runs the driver's
  `login`, `status`, and `logout` as the nil user: a read failure or a failed `login` is
  `UNREACHABLE`, a failed `status` is `DEGRADED`, success is `HEALTHY`, and a failed or
  raising `logout` changes nothing. Success is judged by the driver result, not only the
  sandbox. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`fire_poll`, `SYSTEM_POLL_USER_ID`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_fire_poll_login_failure_marks_unreachable`, `test_fire_poll_status_success_marks_healthy`, `test_fire_poll_status_non_success_marks_degraded`, `test_fire_poll_logout_failure_does_not_mask_outcome`, `test_fire_poll_fetch_device_failure_marks_unreachable`, `test_fire_poll_driver_result_status_failure_marks_degraded`, `test_fire_poll_driver_result_login_failure_marks_unreachable`)
- **OPS-POLL-8.** A failed poll adds one to `consecutive_failures` and a successful one
  resets it to zero; the row records the status, the last run id, and the poll time, and
  a missing row is created. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`fire_poll`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_fire_poll_resets_failures_on_recovery`, `test_fire_poll_creates_row_if_missing`)
- **OPS-POLL-9.** The next poll is one interval away while the failure count is at or
  below `HEALTH_POLL_MAX_CONSECUTIVE_FAILURES`; past it the interval doubles per extra
  failure, capped at `HEALTH_POLL_BACKOFF_CAP_SECONDS`, plus up to half an interval of
  jitter. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_next_poll_at`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_next_poll_at_uses_base_interval_within_threshold`, `test_next_poll_at_uses_base_interval_at_exactly_threshold`, `test_next_poll_at_backoff_kicks_in_past_threshold`, `test_next_poll_at_backoff_capped`)
- **OPS-POLL-10.** Templates read by the poller are cached per template for
  `TEMPLATE_CACHE_TTL_SECONDS`; a failed read is not cached. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_fetch_template_cached`) \
  Pinned by: `services/execution/tests/test_health_scheduler_template_cache.py` (`test_two_calls_within_ttl_fetch_once`, `test_call_after_ttl_expiry_refetches`, `test_different_template_ids_are_cached_independently`, `test_failed_fetch_is_not_cached_as_permanent_negative`, `test_fire_poll_shares_template_cache_across_devices`)
- **OPS-POLL-11.** When the failure count rises from below the threshold to at or above it
  (`bad_news`), or falls from above zero to zero (`recovery`), the poll stages
  `herd.health.status_changed` in the same transaction as the status update; a device
  that stays above the threshold, a first failure below it, and a healthy repeat stage
  nothing, and lowering the threshold at runtime does not fire again. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_decide_transition`, `fire_poll`, `HEALTH_NATS_SUBJECT`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_enqueue_on_threshold_crossing`, `test_enqueue_shares_transaction_with_status_update`, `test_no_enqueue_below_threshold`, `test_no_re_enqueue_on_continued_failure`, `test_enqueue_on_recovery`, `test_no_enqueue_healthy_to_healthy`, `test_decide_transition_threshold_lowered_at_runtime`)
- **OPS-POLL-12.** `HEALTH_POLL_NOTIFY_ENABLED` off stages no health event while polling
  continues. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`fire_poll`) \
  Pinned by: `services/execution/tests/test_health_scheduler.py` (`test_notify_disabled_knob_suppresses_enqueue`)
- **OPS-POLL-13.** Each tick logs `health_tick` with `rows_due` (the full backlog, counted
  only when the batch is full), `rows_claimed`, `polls_fired`, and `polls_deferred`, at
  INFO when anything was due. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`run_tick`, `_due_count`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_tick_emits_structured_log`, `test_rows_due_skips_count_when_batch_not_full`, `test_rows_due_counts_backlog_when_batch_full`)
- **OPS-POLL-14.** A crashed poll is logged and does not stop the tick; its row becomes due
  again when the claim window ends. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`run_tick`) \
  Pinned by: `services/execution/tests/test_health_scheduler_registry.py` (`test_loop_swallows_fire_poll_crash`)
- **OPS-TIER-1.** A corroborated `reservation.created` moves its devices to `in_use`; a
  terminal event moves them to `idle`; `reservation.updated` moves `device_ids` to
  `in_use` and `removed_device_ids` to `idle`; any other event changes nothing. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`apply_reservation_event_tiers`, `TIER_IN_USE_EVENTS`, `TIER_IDLE_EVENTS`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_reservation_created_moves_devices_in_use`, `test_terminal_events_move_devices_idle`, `test_reservation_updated_splits_kept_and_removed`, `test_unrelated_event_changes_nothing`, `test_handle_reservation_event_applies_tiers`)
- **OPS-TIER-2.** The interval for a poll is `HEALTH_POLL_IN_USE_INTERVAL_SECONDS` for an
  `in_use` row and `HEALTH_POLL_IDLE_INTERVAL_SECONDS` for any other row when that
  setting is above 0, else the registry interval. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`_effective_interval`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_effective_interval_defaults_resolve_registry_interval`, `test_effective_interval_per_tier_overrides`, `test_effective_interval_single_override`, `test_run_tick_fires_with_tier_resolved_interval`)
- **OPS-TIER-3.** Moving to `in_use` with an in-use interval set pulls `next_poll_at`
  earlier, never later, and creates a missing row on the `in_use` tier (re-applying the
  tier if the registry seeder inserted the row first); moving to `idle` changes only the
  tier. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`apply_tier_transition`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_in_use_transition_shortens_next_poll`, `test_in_use_transition_never_delays_a_due_row`, `test_in_use_transition_with_default_config_leaves_schedule`, `test_idle_transition_sets_tier_only`, `test_in_use_transition_inserts_missing_row`, `test_in_use_transition_survives_seeder_insert_race`)
- **OPS-TIER-4.** The tier is stored on the row, so it survives a restart, and a
  redelivered event writes the same tier again. \
  Enforced in: `services/execution/app/services/health_scheduler.py` (`apply_tier_transition`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_tier_survives_restart_via_persistence`, `test_reservation_event_redelivery_is_idempotent`)
- **OPS-TIER-5.** The defaults reproduce the pre-tier cadence: batch 10, concurrency 1,
  both tier overrides 0. \
  Enforced in: `services/execution/app/config.py` (`health_poll_batch_size`, `health_poll_max_concurrency`, `health_poll_in_use_interval_seconds`, `health_poll_idle_interval_seconds`) \
  Pinned by: `services/execution/tests/test_health_scheduler_scale.py` (`test_fleet_scale_defaults_match_pre_24_behavior`)

**Out of scope.** The poll interval and its floor (`inventory.md`, INV-POLL-1 to
INV-POLL-4); the on-demand device check (8.11); the driver runs each poll records
(`device-configuration.md`); who is notified of a health event (`integration.md`).

### 8.11 Device health snapshot

**What it does.** The device page shows a colored health badge with the last poll time,
and an admin can list every device's health. Another service can also ask execution to
check one device now, through the same login, status, and logout sequence a poll runs.

**Surfaces.** `services/execution/app/routers/health.py`;
`services/execution/app/routers/executions.py` (`device_check`);
`frontend/src/components/inventory/DeviceHealthBadge.tsx` and
`frontend/src/api/health.ts`.

**Rules.**

- **OPS-HEALTH-1.** `GET /device-health/{device_id}` answers an admin with the stored
  snapshot for any device id. A non-admin gets the stored snapshot only for a device
  inside their device-group visibility, asked once of inventory
  `GET /device-groups/visible-devices` with the caller's own token and 10 seconds through
  execution's one visibility helper; any other device answers byte for byte as an id
  with no snapshot (OPS-HEALTH-2), so the answer does not tell a hidden polled device
  from an unknown id. No token, a transport error, a non-200, a body that is not JSON,
  or a body that is not `{"device_ids": [<str>, ...]}` is 503
  `Could not verify device visibility; nothing was returned. Retry the request.` \
  Enforced in: `services/execution/app/routers/health.py` (`get_device_health`); `services/execution/app/services/device_visibility.py` (`resolve_caller_visibility`, `fetch_visible_device_ids`) \
  Pinned by: `services/execution/tests/test_health_endpoints.py` (`test_get_health_returns_persisted_row`, `test_get_health_available_to_non_admin`, `test_hidden_polled_device_health_answers_exactly_like_an_unknown_device`, `test_get_health_visible_set_empty_hides_every_snapshot`, `test_get_health_fails_closed_when_visibility_unanswerable`, `test_get_health_non_admin_without_token_fails_closed`, `test_get_health_admin_sees_every_row_without_a_lookup`); `tests/integration/test_execution_device_scope.py` (`test_health_read_of_a_visible_device_answers_under_its_id`, `test_health_read_of_a_hidden_device_answers_like_an_unknown_id`)
- **OPS-HEALTH-2.** A device with no snapshot answers 200 with a synthesized `UNKNOWN`
  record (no poll time, zero failures), never 404. \
  Enforced in: `services/execution/app/routers/health.py` (`get_device_health`) \
  Pinned by: `services/execution/tests/test_health_endpoints.py` (`test_get_health_unknown_device_returns_synthetic_200`)
- **OPS-HEALTH-3.** `GET /device-health` is admin only, pages with `skip` and `limit` (1
  to 500, default 50), filters by `last_status`, and orders by last poll time, newest
  first, never-polled last; rows that tie (one poll instant, or never polled, which
  every unpolled device shares) are ordered by `device_id` ascending, the table's
  primary key, so pages never overlap or skip (issue #1589). \
  Enforced in: `services/execution/app/routers/health.py` (`list_device_health`) \
  Pinned by: `services/execution/tests/test_health_endpoints.py` (`test_list_health_admin_only`, `test_list_health_pagination_and_filter`); `services/execution/tests/test_list_order_tiebreak.py` (`test_device_health_list_breaks_ties_by_device_id`, `test_device_health_order_by_reads_nulls_last_on_postgres`)
- **OPS-HEALTH-4.** An unknown `last_status` filter answers 422 `Unknown last_status;
  allowed: UNKNOWN, HEALTHY, DEGRADED, UNREACHABLE` only when it matches no row. The
  detail never holds the submitted value (it read `Invalid last_status: <value>` before
  issue #1458's sweep). \
  Enforced in: `services/execution/app/routers/health.py` (`list_device_health`, `HEALTH_STATUSES`); `services/common/herd_common/error_details.py` (`unknown_value_detail`) \
  Pinned by: `services/execution/tests/test_health_endpoints.py` (`test_list_health_invalid_status_filter_returns_422`)
- **OPS-HEALTH-5.** The badge shows Healthy, Degraded, Unreachable, or Unknown, with the
  last poll time (or Never polled) as its tooltip, and a placeholder while loading. \
  Enforced in: `frontend/src/components/inventory/DeviceHealthBadge.tsx` (`DeviceHealthBadge`); `frontend/src/api/health.ts` (`useDeviceHealth`) \
  Pinned by: `frontend/src/test/components/DeviceHealthBadge.test.tsx` (`renders Healthy in green when status is HEALTHY`, `renders Unknown in gray when device hasn't been polled`, `renders a placeholder while loading`, `renders 'Never polled' tooltip when last_polled_at is null`); `frontend/src/test/api/health.test.tsx` (`useDeviceHealth fetches a snapshot for a device`)
- **OPS-HEALTH-6.** `POST /device-check` takes a body of `device_id` and `user_id` and
  is guarded by the internal token only: 500 `Internal API token not configured` when
  execution has no token, 403 `Invalid internal token` when the header is missing or
  wrong. It reads no user JWT and applies no visibility or grant check; it acts on the
  device id given and records the body's `user_id` on every run it writes. \
  Enforced in: `services/execution/app/routers/executions.py` (`device_check`, `_require_internal_token`) \
  Pinned by: `services/execution/tests/test_api_endpoints.py` (`test_device_check_requires_internal_token`); `services/execution/tests/test_router_endpoints.py` (`test_require_internal_token_rejects_missing`, `test_require_internal_token_errors_when_not_configured`)
- **OPS-HEALTH-7.** The device check reads the device and its template through
  inventory's internal routes, the same reads as `device-configuration.md` (CFG-EXEC-4):
  a 404 answers 404 `Device <id> not found` or `Template <id> not found`, and any other
  failure, a 200 whose body is not a JSON object included, answers 503 `Failed to fetch
  device: <reason>` (or template) with a HERD-authored reason (`upstream service answered
  HTTP <status>`, `upstream service unreachable (<ClassName>)`, `upstream service answered
  with a malformed body`, or the exception's class name), never the exception text, which
  goes to the log message only. A device with no driver answers 409
  `{"error": "device_has_no_driver", "message"}` before any run is written
  (`device-configuration.md`, CFG-GATE-4). \
  Enforced in: `services/execution/app/services/execution_service.py` (`fetch_device`, `fetch_template`, `_fetch_inventory_internal`, `_inventory_failure_text`, `_assert_action_permitted`) \
  Pinned by: `services/execution/tests/test_router_endpoints.py` (`test_fetch_device_404_raises_404`, `test_fetch_device_other_error_raises_503`, `test_fetch_template_404_raises_404`, `test_fetch_template_other_error_raises_503`, `test_fetch_failure_detail_never_carries_foreign_text`); `services/execution/tests/test_configure_capability_gate.py` (`test_assert_action_permitted_raises_409_for_no_driver_regardless_of_action`); `tests/integration/test_device_check_internal_fetch.py` (`test_device_check_uses_internal_inventory_routes`)
- **OPS-HEALTH-8.** The check runs `login` first; when the login run is not `SUCCESS` it
  answers 200 with `status` `FAILED`, the login run's id, and the login run's `error`,
  and runs neither `status` nor `logout`. \
  Enforced in: `services/execution/app/routers/executions.py` (`device_check`) \
  Pinned by: `services/execution/tests/test_api_endpoints.py` (`test_device_check_login_failure`); `services/execution/tests/test_router_direct.py` (`test_device_check_login_failure_short_circuits`); `services/execution/tests/test_router_endpoints.py` (`test_device_check_login_failure_short_circuits`)
- **OPS-HEALTH-9.** After a successful login the check runs `status` and then `logout`
  and answers 200 with the status run's id, `status`, `output`, and `error`; the logout
  run's outcome is not reported. \
  Enforced in: `services/execution/app/routers/executions.py` (`device_check`) \
  Pinned by: `services/execution/tests/test_router_direct.py` (`test_device_check_status_success_path`); `services/execution/tests/test_api_endpoints.py` (`test_device_check_success`); `services/execution/tests/test_router_endpoints.py` (`test_device_check_success`)
- **OPS-HEALTH-10.** The check writes only execution run rows, one per action it ran,
  with no reservation; it neither reads nor writes the device's health status row, so
  the badge and the poll schedule do not change, and it stages no event. \
  Enforced in: `services/execution/app/routers/executions.py` (`device_check`) \
  Pinned by: none

**Out of scope.** Health history: each poll's driver runs are the history
(`device-configuration.md`).

### 8.12 Secret store

**What it does.** An admin stores a credential (a password, key, or token) once, by
name; HERD keeps it encrypted at rest and hands its plaintext only to an admin, to a user
an admin granted `manage` on it, and to other HERD services that need it to reach a
device or hypervisor.

**Surfaces.** `services/secrets/app/routers/secrets.py`,
`services/secrets/app/routers/internal.py`, `services/secrets/app/services/crypto.py`,
`services/secrets/app/services/keyring.py`, and
`services/secrets/app/services/inventory_guard.py`; the
hypervisor page's secret selector (`inventory.md`); key rotation in
[OPERATIONS.md](../OPERATIONS.md) (Secrets-service key rotation). Grants are
`identity-and-access.md`'s ACL rules with resource type `secret`.

**Rules.**

- **OPS-SECRET-1.** `POST /secrets` is admin only and takes a `name` of 1 to 255
  characters, a `type` of `api_key`, `ssh_key`, `password`, `token`, or `generic`
  (default `generic`), an optional `description` up to 1024 characters, and a non-empty
  `data` object of string values; it answers 201 with metadata only. A `data` key or
  value holding a lone surrogate is refused with the shared 422, one `string_unicode`
  item at `["body"]` (OPS-VALID-6), and nothing is stored: before issue #1456 the plain `dict[str, str]`
  accepted one, and every reveal of the stored secret then answered 500 (a value) or
  returned a changed key (U+FFFD in place of the surrogate). Other non-ASCII text, a
  character outside the BMP, and a NUL in a value are stored and returned unchanged. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`create_secret`); `services/secrets/app/schemas/secret.py` (`SecretCreate`, `SecretResponse`); `services/common/herd_common/validation_errors.py` (`RefuseLoneSurrogates`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_admin_create_returns_metadata_only`, `test_empty_data_is_422`, `test_non_admin_cannot_create`); `services/secrets/tests/test_unencodable_data.py` (`test_a_create_holding_a_lone_surrogate_is_422_and_stores_nothing`, `test_honest_data_sent_escaped_round_trips_exactly`); `tests/integration/test_secrets_flow.py` (`test_a_lone_surrogate_in_data_is_refused_and_nothing_is_stored`)
- **OPS-SECRET-2.** A taken name is 409 `A secret with this name already exists`. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`create_secret`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_duplicate_name_is_409`); `services/secrets/tests/test_routers_direct.py` (`test_create_secret_direct_duplicate_name_is_409`)
- **OPS-SECRET-3.** `data` is stored as AES-GCM ciphertext under the active data key with
  the secret id and key version as associated data, so altered ciphertext, a row swapped
  onto another secret, or a changed key version fails to decrypt; the stored row holds no
  plaintext. \
  Enforced in: `services/secrets/app/services/crypto.py` (`encrypt_value`, `decrypt_value`, `_value_aad`) \
  Pinned by: `services/secrets/tests/test_crypto.py` (`test_value_round_trip`, `test_tampered_ciphertext_fails_auth_tag`, `test_row_swap_fails_via_aad`, `test_key_version_swap_fails_via_aad`); `services/secrets/tests/test_api.py` (`test_ciphertext_in_db_has_no_plaintext`)
- **OPS-SECRET-4.** No metadata response and no log line carries plaintext. \
  Enforced in: `services/secrets/app/schemas/secret.py` (`SecretResponse`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_admin_create_returns_metadata_only`, `test_plaintext_never_hits_the_logs`)
- **OPS-SECRET-5.** `GET /secrets` lists every secret, by name, for an admin; for anyone
  else only the secrets the ACL service reports a `view` or `manage` grant on, and none
  when the ACL service fails or answers anything but a readable 200. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`list_secrets`, `_granted_secret_ids`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_admin_list_sees_all`, `test_non_admin_list_is_acl_filtered`); `services/secrets/tests/test_routers_direct.py` (`test_granted_secret_ids_transport_error_returns_empty_set`, `test_granted_secret_ids_non_200_returns_empty_set`, `test_granted_secret_ids_malformed_body_returns_empty_set`)
- **OPS-SECRET-6.** `GET /secrets/{secret_id}` answers metadata to an admin or a holder of
  `view` or `manage`; anyone else gets the same 404 `Secret not found` as an unknown id. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`get_secret`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_no_grant_get_is_404_not_403`); `services/secrets/tests/test_routers_direct.py` (`test_get_secret_direct_non_admin_with_grant_sees_metadata`, `test_get_secret_direct_not_found`)
- **OPS-SECRET-7.** `GET /secrets/{secret_id}/value` answers `{id, name, data}` in
  plaintext to an admin or a holder of `manage`. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`reveal_secret`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_admin_reveal_round_trips`, `test_manage_grant_reveals`); `tests/integration/test_secrets_flow.py` (`test_manage_grant_lets_a_member_reveal`)
- **OPS-SECRET-8.** A holder of `view` only gets 403 `manage permission required` on
  reveal; a caller with no grant gets 404 `Secret not found`. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`reveal_secret`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_view_grant_sees_metadata_but_reveal_is_403`); `services/secrets/tests/test_routers_direct.py` (`test_reveal_secret_direct_no_grant_is_404`, `test_reveal_secret_direct_view_only_is_403`)
- **OPS-SECRET-9.** The internal reveal by id or by exact name answers the plaintext to a
  caller with the internal token, and 404 `Secret not found` for an unknown one. \
  Enforced in: `services/secrets/app/routers/internal.py` (`internal_reveal_by_id`, `internal_reveal_by_name`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_internal_reveal_by_id`, `test_internal_reveal_by_name`, `test_internal_unknown_secret_is_404`); `tests/integration/test_secrets_flow.py` (`test_internal_token_retrieval`)
- **OPS-SECRET-10.** A missing, empty, or wrong internal token is 403 `Invalid internal
  token`, checked before any lookup. \
  Enforced in: `services/secrets/app/routers/internal.py` (`_check_internal_token`) \
  Pinned by: `services/secrets/tests/test_internal_router_direct.py` (`test_check_internal_token_wrong_token_raises_403`, `test_check_internal_token_empty_token_raises_403`, `test_internal_reveal_by_id_direct_wrong_token_raises_before_lookup`); `services/secrets/tests/test_api.py` (`test_internal_wrong_token_is_403`)
- **OPS-SECRET-11.** `PUT /secrets/{secret_id}` is admin only and replaces the type and
  description when given; a given `data` replaces the whole object, re-encrypted under the
  active key, and without it the ciphertext is untouched. The name cannot change. A
  `data` key or value holding a lone surrogate is refused as on create (OPS-SECRET-1)
  and the stored data is kept. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`update_secret`); `services/secrets/app/schemas/secret.py` (`SecretUpdate`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_update_data_reencrypts`); `services/secrets/tests/test_routers_direct.py` (`test_update_secret_direct_reencrypts_and_updates_metadata`, `test_update_secret_direct_no_data_leaves_ciphertext_untouched`, `test_update_secret_direct_not_found`); `services/secrets/tests/test_unencodable_data.py` (`test_an_update_holding_a_lone_surrogate_is_422_and_keeps_the_old_data`)
- **OPS-SECRET-12.** `DELETE /secrets/{secret_id}` is admin only, answers 204, and 404 for
  an unknown id. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`delete_secret`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_delete_then_404`); `services/secrets/tests/test_routers_direct.py` (`test_delete_secret_direct_not_found`)
- **OPS-SECRET-13.** A delete is refused with 409 `{"error": "secret_in_use",
  "hypervisor_ids", "hypervisor_names"}` while inventory reports a hypervisor that
  references the secret; there is no force flag. By decision; issue #456. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`delete_secret`); `services/secrets/app/services/inventory_guard.py` (`find_hypervisors_referencing_secret`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_delete_refused_while_hypervisor_references_secret`); `services/secrets/tests/test_routers_direct.py` (`test_delete_secret_direct_refused_while_referenced`)
- **OPS-SECRET-14.** An unreachable inventory, a non-200 answer, or a 200 whose body is
  not a JSON list of objects refuses the delete with 503 and deletes nothing; the
  unreadable 200 uses the non-200 wording. \
  Enforced in: `services/secrets/app/services/inventory_guard.py` (`find_hypervisors_referencing_secret`, `UPSTREAM_ERROR_DETAIL`) \
  Pinned by: `services/secrets/tests/test_inventory_guard.py` (`test_transport_error_fails_closed_503`, `test_upstream_error_fails_closed_503`, `test_malformed_200_fails_closed_503`); `services/secrets/tests/test_api.py` (`test_delete_fails_closed_when_inventory_unreachable`, `test_delete_fails_closed_on_malformed_inventory_200`)
- **OPS-SECRET-15.** The service refuses to start unless `SECRETS_KEK` is base64 for
  exactly 32 bytes, naming the variable; on an empty key table it creates key version 1. \
  Enforced in: `services/secrets/app/services/crypto.py` (`load_kek`); `services/secrets/app/services/keyring.py` (`bootstrap_keyring`); `services/secrets/app/main.py` (`lifespan`) \
  Pinned by: `services/secrets/tests/test_crypto.py` (`test_load_kek_empty_is_a_boot_error`, `test_load_kek_invalid_base64`, `test_load_kek_wrong_length`, `test_load_kek_names_the_variable`); `services/secrets/tests/test_keyring.py` (`test_first_boot_creates_version_1`, `test_reboot_recovers_the_same_dek`, `test_missing_kek_refuses_to_boot`)
- **OPS-SECRET-16.** At start, a key version that does not unwrap under `SECRETS_KEK` is
  unwrapped under `SECRETS_KEK_PREVIOUS` and re-wrapped under the current key; one that
  unwraps under neither refuses the start. \
  Enforced in: `services/secrets/app/services/keyring.py` (`bootstrap_keyring`) \
  Pinned by: `services/secrets/tests/test_keyring.py` (`test_wrong_kek_refuses_to_boot`, `test_kek_rotation_rewraps_and_sticks`); `services/secrets/tests/test_crypto.py` (`test_dek_unwrap_wrong_kek_fails`)
- **OPS-SECRET-17.** `POST /keys/rotate` is admin only: in one transaction it adds key
  version `max + 1`, re-encrypts every secret to it, and retires every earlier version
  without deleting it, then answers `{new_version, reencrypted}`. The key table is the
  source of truth and each process's keyring is a cache of it: a key version a process
  has not seen (another replica's rotation) is read and unwrapped on first use, and every
  write encrypts under the newest unretired version in the table, so a rotation binds
  every replica with no restart. A version that is missing or does not unwrap under this
  process's `SECRETS_KEK` answers 503 `Secret key material is unavailable to this service`
  on reveal, internal reveal, create, update, and rotate, logged with action
  `key_version_unavailable`. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`rotate_keys`, `_encrypt_into`, `_decrypt`); `services/secrets/app/routers/internal.py` (`_reveal`); `services/secrets/app/services/keyring.py` (`rotate_dek`, `load_dek`, `current`, `key_unavailable_http`) \
  Pinned by: `services/secrets/tests/test_keyring.py` (`test_dek_rotation_reencrypts_and_retires`, `test_dek_rotation_survives_reboot`, `test_peer_replica_loads_a_rotated_version_on_first_use`, `test_peer_replica_encrypts_new_data_under_the_rotated_version`, `test_load_dek_unknown_version_is_unavailable`, `test_load_dek_under_a_different_kek_is_unavailable`, `test_stale_replica_rotation_picks_the_next_version`); `services/secrets/tests/test_api.py` (`test_rotate_endpoint`, `test_rotate_requires_admin`, `test_reveal_on_a_replica_that_missed_the_rotation`, `test_write_on_a_replica_that_missed_the_rotation_uses_the_new_version`, `test_unloadable_key_version_is_503_with_a_fixed_detail`); `tests/integration/test_secrets_flow.py` (`test_rotation_preserves_plaintext`)
- **OPS-SECRET-18.** Every rotation takes one transaction-scoped Postgres advisory lock
  (`herd-secrets-dek-rotation`) before it reads the key table, so two rotations, in one
  process or two replicas, run one after the other and the second moves to the next
  version. If the new version is taken anyway, the rotation rolls back and answers 409
  `Another key rotation committed first; nothing was changed. Retry.`; it never answers 500. \
  Enforced in: `services/secrets/app/services/keyring.py` (`rotate_dek`, `DEK_ROTATION_LOCK_KEY`); `services/secrets/app/routers/secrets.py` (`rotate_keys`) \
  Pinned by: `services/secrets/tests/test_keyring.py` (`test_rotation_takes_the_lock_before_reading_the_key_table`, `test_concurrent_rotation_conflict_is_refused_and_changes_nothing`); `services/secrets/tests/test_api.py` (`test_rotation_conflict_is_409_not_500`)
- **OPS-SECRET-19.** A secret stored before OPS-SECRET-1 refused a lone surrogate can
  hold one in a `data` key or value. Its reveal, the user reveal and both internal reveals,
  fails closed: 409 `Stored secret data is not valid UTF-8 text; an admin must replace it
  with PUT /secrets/{secret_id}`, logged at error with the secret's id and nothing from
  its data, never a 500 and never the data with the surrogate replaced (a credential is
  returned as stored or not at all). Metadata, the list, rotation, a PUT of new `data`
  (which restores reveals), and delete work as for any secret. Inventory answers the 409
  at hypervisor registration with a 422 (`inventory.md`), and execution's secret fetch
  treats it as permanent, like a 404, so the message is not retried. By decision (issue
  #1456): returning the value with U+FFFD and a flag would hand a consumer a credential
  that differs from the stored one. \
  Enforced in: `services/secrets/app/services/keyring.py` (`deserialize_data`, `SecretDataUnreadableError`, `secret_data_unreadable_http`, `SECRET_DATA_UNREADABLE_DETAIL`); `services/secrets/app/routers/secrets.py` (`_decrypt`); `services/secrets/app/routers/internal.py` (`_reveal`) \
  Pinned by: `services/secrets/tests/test_unencodable_data.py` (`test_deserialize_refuses_data_holding_a_lone_surrogate`, `test_deserialize_returns_honest_data_unchanged`, `test_a_stored_lone_surrogate_is_a_fixed_409_on_every_reveal`, `test_a_stored_lone_surrogate_leaves_metadata_rotation_and_replacement_working`); `services/execution/tests/test_nats_consumer.py` (`test_fetch_secret_value_returns_none_on_an_unreadable_secret_409`)

**Out of scope.** Granting `view` and `manage` (`identity-and-access.md`); hypervisor
registration and its secret check (`inventory.md`); how execution uses a hypervisor
credential (`dynamic-resources.md`).

### 8.13 User preferences

**What it does.** Each user's saved list filters, page sizes, and other per-user settings
are stored on the server, so they follow the user between browsers.

**Surfaces.** `services/user-profile/app/routers/preferences.py`,
`services/user-profile/app/services/preferences_service.py`; the browser side is
`frontend/src/stores/preferencesStore.ts`, specified with the pages that use it
(`inventory.md`, `topology.md`, `reservations.md`).

**Rules.**

- **OPS-PREF-1.** Every route acts on the caller's own row, keyed by the token's subject;
  a missing or non-UUID subject is 401. `GET /preferences` creates an empty row on first
  access, and two first accesses at once converge on one row. \
  Enforced in: `services/user-profile/app/routers/preferences.py` (`get_preferences_endpoint`); `services/user-profile/app/services/preferences_service.py` (`get_or_create`) \
  Pinned by: `services/user-profile/tests/test_preferences.py` (`test_get_preferences_auto_creates_empty`, `test_get_preferences_idempotent`, `test_user_isolation`); `services/user-profile/tests/test_preferences_edge.py` (`test_get_preferences_invalid_uuid_in_sub_returns_401`, `test_get_or_create_recovers_from_lost_insert_race`)
- **OPS-PREF-2.** `PUT /preferences` replaces all three objects, an omitted one becoming
  empty; unknown body fields are ignored. \
  Enforced in: `services/user-profile/app/routers/preferences.py` (`put_preferences_endpoint`); `services/user-profile/app/services/preferences_service.py` (`replace`) \
  Pinned by: `services/user-profile/tests/test_preferences.py` (`test_put_preferences_replaces_all`, `test_put_preferences_clobbers_existing`, `test_put_preferences_defaults`); `services/user-profile/tests/test_preferences_edge.py` (`test_put_unknown_field_is_ignored`)
- **OPS-PREF-3.** `PATCH /preferences` merges each given object one level deep into the
  stored one; an omitted or null object is left alone, an empty object changes nothing in
  it, and no PATCH can remove a key (only `PUT` and `DELETE` can). \
  Enforced in: `services/user-profile/app/services/preferences_service.py` (`patch`, `_merge`) \
  Pinned by: `services/user-profile/tests/test_preferences.py` (`test_patch_merges_page_sizes_without_clobbering_filters`, `test_patch_shallow_merge_overwrites_same_key`, `test_patch_empty_body_is_noop`); `services/user-profile/tests/test_preferences_edge.py` (`test_patch_explicit_null_does_not_clear`, `test_patch_partial_updates_only_named_keys`, `test_patch_empty_dict_for_key_replaces_with_empty`)
- **OPS-PREF-4.** Each object holds at most 200 keys, `saved_filters` and `extras` at most
  64 KB serialized, and every page size is an integer from 1 to 500 (a boolean is
  refused); a request over a cap is 422. \
  Enforced in: `services/user-profile/app/schemas/preferences.py` (`_validate_page_sizes`, `_validate_blob`, `_MAX_KEYS`, `_MAX_PAGE_SIZE`, `_MAX_BLOB_BYTES`) \
  Pinned by: `services/user-profile/tests/test_preferences.py` (`test_put_preferences_rejects_invalid_page_sizes`, `test_put_preferences_accepts_valid_page_size`, `test_put_preferences_rejects_oversized_blob`); `services/user-profile/tests/test_preferences_edge.py` (`test_put_with_non_dict_saved_filters_returns_422`)
- **OPS-PREF-5.** The caps apply to the merged result of a PATCH too: a merge over a cap
  is 422 `merged <reason>` and leaves the row unchanged. \
  Enforced in: `services/user-profile/app/services/preferences_service.py` (`patch`) \
  Pinned by: `services/user-profile/tests/test_preferences_edge.py` (`test_patch_past_blob_cap_returns_422_and_leaves_row_unchanged`, `test_patch_merge_past_key_cap_returns_422`, `test_patch_merge_past_page_sizes_key_cap_returns_422`, `test_patch_rejection_leaves_other_fields_untouched`, `test_patch_overwriting_existing_keys_stays_within_cap`)
- **OPS-PREF-6.** `DELETE /preferences` empties all three objects and answers the empty
  row. \
  Enforced in: `services/user-profile/app/services/preferences_service.py` (`reset`) \
  Pinned by: `services/user-profile/tests/test_preferences.py` (`test_delete_resets_to_defaults`, `test_delete_without_existing_row`)
- **OPS-PREF-7.** The internal read takes a `user_id` (422 when missing or not a UUID),
  needs the internal token (401 `Invalid internal token` when missing, empty, or wrong),
  and creates an empty row when none exists. \
  Enforced in: `services/user-profile/app/routers/preferences.py` (`get_preferences_internal`, `_require_internal_token`) \
  Pinned by: `services/user-profile/tests/test_preferences.py` (`test_internal_endpoint_requires_token`, `test_internal_endpoint_rejects_wrong_token`, `test_internal_endpoint_returns_prefs_and_auto_creates`); `services/user-profile/tests/test_preferences_edge.py` (`test_internal_endpoint_missing_user_id_returns_422`, `test_internal_endpoint_blank_token_value_rejected`)

**Out of scope.** Which keys each page stores and the browser's debounce and load merge
(the page owners' documents); notification channel preferences (`integration.md`).

### 8.14 Reporting and analytics

**What it does.** An admin picks a time window and sees how many hours each user, device,
topology type, day, group, and lab purpose used, how busy every device in the fleet was,
which devices sat idle, and downloads any table as CSV.

**Surfaces.** `GET /reports/utilization` and `GET /reports/utilization.csv` in
`services/reservations/app/routers/reservations.py`; the builder in
`services/reservations/app/services/reporting_service.py`;
`frontend/src/pages/ReportingPage.tsx` (route `/reporting`, behind the admin route guard)
and `frontend/src/api/reporting.ts`. The purpose categories and suggestions it counts are
`reservations.md` (section 8.11).

**Rules.**

- **OPS-REPORT-1.** Both report routes are admin only; `start` and `end` are required, an
  `end` not after `start` is 422, and a window longer than `UTILIZATION_MAX_SPAN_DAYS`
  (default 366; 0 disables) is 422 naming the limit. A bound whose instant has no UTC
  equivalent (an offset near year 1 or year 9999, such as `0001-01-01T00:00:00+05:00`)
  is 422 `<start or end> must fall between 0001-01-01T00:00:00Z and
  9999-12-31T23:59:59.999999Z once converted to UTC`, checked `start` first; no value of
  `start` or `end` answers 5xx (issue #1523). \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`); `services/reservations/app/routers/reservations.py` (`_as_utc`, `get_utilization_report`, `get_utilization_report_csv`) \
  Pinned by: `services/reservations/tests/test_rbac_denial.py` (`test_non_admin_denied`); `services/reservations/tests/test_reporting_service.py` (`test_build_report_rejects_inverted_window`, `test_build_report_span_over_max_raises`, `test_build_report_span_at_max_succeeds`, `test_build_report_span_guard_disabled_when_zero`); `services/reservations/tests/test_coverage_gaps.py` (`test_csv_report_inverted_window_returns_422`); `services/reservations/tests/test_report_utc_days.py` (`test_report_routes_answer_422_for_a_bound_with_no_utc_equivalent`, `test_report_routes_never_answer_5xx_for_extreme_bounds_seeded_sweep`); `tests/integration/test_reporting.py` (`test_utilization_report_non_admin_forbidden`)
- **OPS-REPORT-2.** A reservation counts for the hours it overlaps the window, clipped to
  the window; one with no overlap is skipped. The report query loads only reservations
  that end after the window's start and start before its end, in a status the user,
  device, or fleet section counts; a zero-length reservation inside the window is loaded
  and skipped. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_build_report_clamps_window`, `test_build_report_skips_zero_hours`); `services/reservations/tests/test_reporting_edges.py` (`test_build_report_clips_hours_to_window`, `test_a_row_with_no_overlap_is_skipped`, `test_the_report_query_loads_only_rows_that_overlap_the_window`, `test_the_report_query_loads_only_the_statuses_a_section_counts`)
- **OPS-REPORT-3.** Without `status`, the user, device, topology type, day, group, and
  purpose sections count only `COMPLETED` reservations and the fleet section counts
  `ACTIVE` and `COMPLETED`; a given `status` list applies to every section. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report`, `get_utilization_report_csv`, `FLEET_DEFAULT_STATUS_FILTER`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_fleet_counts_active_but_legacy_default_does_not`, `test_explicit_filter_applies_to_fleet_too`, `test_report_route_fleet_defaults_count_active`); `services/reservations/tests/test_reporting_service.py` (`test_build_report_filters_status`); `services/reservations/tests/test_utilization_report_routes.py` (`test_each_section_counts_the_rule_s_statuses_on_both_routes`)
- **OPS-REPORT-4.** The day section splits each reservation's hours at UTC midnight,
  labels each slice with its UTC date, and counts a reservation once per day it touches.
  `start` and `end` may carry any UTC offset, and a value without one is UTC (never the
  host's zone): both routes convert the bounds to UTC before anything reads them, and
  the split converts on its own, so its slices are positive, contiguous, and sum to the
  overlap, the day hours sum to `total_hours` within the four-decimal rounding of each
  value (each day bucket and the total are rounded on their own), and the report echoes
  the window in UTC (issue #1523). The split also holds on the first and the last
  representable UTC day: a slice on 9999-12-31 runs to the window's end, and a day
  before year 1000 is labeled with four digits (`0001-01-01`). \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`_split_hours_per_day`, `to_utc_instant`, `build_utilization_report`); `services/common/herd_common/timeutil.py` (`to_utc_instant`); `services/reservations/app/routers/reservations.py` (`_as_utc`, `get_utilization_report`, `get_utilization_report_csv`) \
  Pinned by: `services/reservations/tests/test_reporting_edges.py` (`test_split_hours_across_midnight`, `test_split_hours_three_days_full_middle`, `test_build_report_day_buckets_count_unique_reservations_per_day`); `services/reservations/tests/test_reporting_service.py` (`test_build_report_aggregates_by_day_across_midnight`); `services/reservations/tests/test_report_utc_days.py` (`test_split_hours_negative_offset_start_late_in_local_day`, `test_split_hours_positive_offset_labels_the_utc_dates`, `test_split_hours_mixed_offsets_crossing_several_utc_midnights`, `test_split_hours_start_exactly_at_utc_midnight_in_an_offset`, `test_split_hours_naive_bounds_are_utc_under_a_minus_eight_host_zone`, `test_split_hours_last_representable_day_has_no_next_midnight`, `test_split_hours_labels_a_year_before_1000_with_four_digits`, `test_split_hours_naive_start_with_an_offset_end`, `test_split_hours_property_any_offsets_any_window`, `test_build_report_by_day_sums_to_total_for_any_offset_window`, `test_build_report_takes_a_naive_and_an_offset_bound_and_echoes_utc`, `test_report_route_by_day_is_split_at_utc_midnight_for_any_offset`, `test_report_route_hands_utc_bounds_downstream`, `test_report_routes_take_a_naive_bound_as_utc_not_the_host_zone`, `test_report_routes_answer_a_window_at_the_edges_of_the_range`); `tests/integration/test_reporting.py` (`test_utilization_report_by_day_sums_to_total_with_an_offset_start`)
- **OPS-REPORT-5.** The group section rolls each user's hours into every group the user
  belongs to (a user in two groups counts in both) and a user in none into `Ungrouped`;
  when auth cannot answer, every user is `Ungrouped` and the report still answers 200. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_user_groups_map`, `rollup_by_group`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_rollup_by_group_buckets_users_into_groups`, `test_rollup_by_group_counts_multi_group_user_against_each_group`, `test_fetch_user_groups_map_uses_single_batch_call`, `test_fetch_user_groups_map_fails_soft_on_non_200`, `test_fetch_user_groups_map_fails_soft_on_unreachable`)
- **OPS-REPORT-6.** The purpose sections count device-hours; a reservation with a
  confirmed category counts under it, one with neither a category nor a suggestion
  counts as `unclassified`, and one with only an AI suggestion counts only in
  `by_purpose_suggested` under the suggestion's top category, never in the confirmed
  totals. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`, `UNCLASSIFIED_PURPOSE`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_build_report_aggregates_by_purpose`, `test_build_report_by_purpose_suggested_split`)
- **OPS-REPORT-7.** With `include_transit` on (the default) the device sections also
  count every device on the reservation's fork wiring that it did not reserve, with the
  reservation's hours and confirmed category; a reserved device on a path counts once,
  as reserved, and a suggestion-only reservation adds transit hours but no transit
  purpose rows. In a 200 answer from cabling, an entry whose reservation id is not a UUID
  string is ignored, a device id string that is not a UUID is ignored while the entry's
  other device ids still count, and a null device list adds no transit devices. A device
  list that is not a list (a number, a string, a boolean, an object) adds no transit
  devices either, and a device id that is not a string (a number, a boolean, null, an
  object, a list) is ignored while the entry's other device ids still count; these
  wrongly typed values are logged once per report at WARNING with `action`
  `transit_gear_entries_skipped` and the two counts, never the body (issue #1553). The
  report is still produced. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`, `_fetch_transit_devices`) \
  Pinned by: `services/reservations/tests/test_transit_gear_rollup.py` (`test_transit_device_inherits_category_and_hours`, `test_reserved_and_transit_dedupes_to_reserved_only`, `test_duplicate_device_ids_in_fork_response_count_once`, `test_mixed_reserved_in_one_transit_in_another`, `test_reservation_absent_from_cabling_map_has_no_transit`, `test_ai_suggested_only_reservation_contributes_no_device_purpose_transit`, `test_transit_devices_of_every_chunk_are_merged`); `services/reservations/tests/test_utilization_report_routes.py` (`test_transit_devices_come_from_one_internal_cabling_call_for_the_counted_rows`, `test_valid_fork_devices_count_around_entries_that_are_not_uuids`, `test_wrongly_typed_entries_are_skipped_and_the_report_is_still_produced`); `services/reservations/tests/test_transit_gear_rollup.py` (`test_a_device_list_of_the_wrong_type_is_skipped_and_logged_once`, `test_device_ids_of_the_wrong_type_are_skipped_and_the_rest_count`, `test_wrongly_typed_entries_across_chunks_log_one_line_per_report`, `test_the_pinned_leniency_logs_nothing`)
- **OPS-REPORT-8.** Transit devices are read from cabling in chunks of 500 reservations;
  a transport error, a non-200, or a 200 whose body is not the declared shape (a body
  that does not parse: not JSON, not UTF-8, or nested deeper than the decoder's recursion
  limit; or one that is not an object holding a `devices` object: a list, a string, a
  boolean, a number, null, an object without `devices`, or `devices` that is not an
  object) from any chunk
  fails the report closed with 503 `{"error": "transit_gear_unavailable"}`, and
  `include_transit=false` makes no cabling call. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`_fetch_transit_devices`, `_TRANSIT_BATCH_CHUNK_SIZE`, `TransitGearUnavailable`) \
  Pinned by: `services/reservations/tests/test_transit_gear_rollup.py` (`test_chunking_splits_into_500_and_1`, `test_transport_failure_raises_transit_gear_unavailable`, `test_non_200_raises_transit_gear_unavailable`, `test_include_transit_false_skips_the_cabling_call`, `test_transit_devices_of_every_chunk_are_merged`, `test_a_failure_from_any_chunk_raises_transit_gear_unavailable`, `test_a_200_that_is_not_an_object_with_a_devices_object_is_unavailable`, `test_a_200_that_is_not_json_is_unavailable_without_its_text`, `test_a_wrongly_shaped_body_in_any_chunk_is_unavailable`); `services/reservations/tests/test_utilization_report_routes.py` (`test_transit_devices_come_from_one_internal_cabling_call_for_the_counted_rows`, `test_a_cabling_failure_is_503_transit_gear_unavailable_on_both_routes`, `test_a_200_of_the_wrong_shape_is_503_transit_gear_unavailable_on_both_routes`, `test_a_cabling_outage_fails_every_csv_section_closed`, `test_include_transit_false_makes_no_cabling_call_on_both_routes`)
- **OPS-REPORT-9.** The routes map that failure to 503 `{"error":
  "transit_gear_unavailable"}`. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report`, `get_utilization_report_csv`) \
  Pinned by: `services/reservations/tests/test_reservations.py` (`test_utilization_report_cabling_unreachable_is_503`, `test_utilization_report_cabling_non_200_is_503`, `test_utilization_report_csv_cabling_unreachable_is_503`, `test_utilization_report_csv_cabling_non_200_is_503`); `services/reservations/tests/test_utilization_report_routes.py` (`test_a_cabling_failure_is_503_transit_gear_unavailable_on_both_routes`, `test_a_200_of_the_wrong_shape_is_503_transit_gear_unavailable_on_both_routes`, `test_a_cabling_outage_fails_every_csv_section_closed`)
- **OPS-REPORT-10.** The fleet section lists every device inventory returns (paged by 500,
  with the caller's token, stopping at 100,000), each with its hours over the whole
  window as `utilization_pct`, unclamped; devices with no hours are the idle count, and a
  deleted device stays only in the device section. The fleet section lists its devices
  by `utilization_pct` (rounded to two decimals), highest first, then by `name` in code
  point order; devices equal in both keep inventory's order. The device fetch stops after
  a page shorter than 500, or, when inventory's `total` is a JSON integer (a number
  written without a fraction or exponent; a boolean reads as 1 or 0), after the page
  that brings the devices held to at least `total`; any other `total` is ignored. So a
  fleet whose size is an exact multiple of 500, reported in `total`, takes no extra
  request. The fetch asks for at most 200 pages (100,000 devices); when the 200th page is
  full and does not reach a JSON integer `total`, the fleet is null as if inventory
  could not answer. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_fleet_devices`, `_build_fleet_section`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_fleet_section_math_and_idle_devices`, `test_fleet_pct_is_not_clamped_at_100`, `test_device_missing_from_inventory_stays_in_legacy_section_only`, `test_empty_inventory_yields_empty_fleet_section`, `test_fleet_devices_are_listed_busiest_first_then_by_name`, `test_fleet_order_compares_the_rounded_pct_and_keeps_inventory_order_on_a_full_tie`, `test_fetch_fleet_devices_paginates_past_500`, `test_fetch_fleet_devices_lists_every_device_inventory_returns`, `test_fetch_fleet_devices_takes_no_extra_request_at_a_multiple_of_500_in_total`, `test_fetch_fleet_devices_stops_once_the_devices_held_reach_an_integer_total`, `test_fetch_fleet_devices_stops_at_100_000_devices`, `test_the_200th_page_nulls_the_fleet_only_when_full_and_short_of_an_integer_total`, `test_a_fleet_past_the_200_page_cap_is_null_like_an_unreachable_inventory`); `tests/integration/test_reporting.py` (`test_fleet_section_counts_active_and_lists_idle_devices`)
- **OPS-REPORT-11.** When inventory cannot answer, the JSON report answers 200 with
  `fleet` null. Inventory cannot answer also when a page is a 200 whose body does not
  parse (not JSON, not UTF-8, or nested deeper than the decoder's recursion limit) or
  is not an object holding an `items` list; entries inside a well-formed list stay
  lenient (OPS-REPORT-10) (issue #1577). \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_fleet_devices`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_fetch_fleet_devices_none_on_non_200`, `test_fetch_fleet_devices_none_on_unreachable`, `test_report_route_fleet_null_when_inventory_unreachable`, `test_an_inventory_page_the_report_cannot_use_is_like_an_unreachable_inventory`)
- **OPS-REPORT-12.** `execution_run_count` is the number of driver runs started in the
  window, read from execution with the caller's token, and null when execution cannot
  answer, a 200 whose body does not parse or is not an object included (issue #1577). \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_execution_run_count`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_fetch_execution_run_count_returns_total`, `test_fetch_execution_run_count_swallows_http_errors`, `test_fetch_execution_run_count_returns_none_on_non_200`); `services/reservations/tests/test_fleet_report.py` (`test_an_execution_answer_the_report_cannot_use_is_a_null_run_count`)
- **OPS-REPORT-13.** The CSV route serves one `section` (`user`, `device`, `fleet`,
  `purpose`, `user_purpose`, `device_purpose`, `purpose_suggested`; anything else is
  422) as `text/csv` with an attachment name dated with the UTC dates of `start` and
  `end`; hours have four decimals. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report_csv`); `services/reservations/app/services/reporting_service.py` (`report_to_csv`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_report_to_csv_user_section`, `test_report_to_csv_device_section_carries_transit_columns`, `test_report_to_csv_purpose_suggested_section`, `test_report_to_csv_rejects_unknown_section`); `services/reservations/tests/test_report_utc_days.py` (`test_csv_route_reproduction_matches_json_and_names_utc_dates`); `tests/integration/test_reporting.py` (`test_utilization_report_csv_download`, `test_utilization_report_csv_rejects_unknown_section`)
- **OPS-REPORT-14.** The `fleet` CSV is 503 `Inventory service is unreachable` when
  inventory cannot answer (OPS-REPORT-11, a page that cannot be read included, issue
  #1577); the other sections make no inventory call. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report_csv`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_csv_route_fleet_503_when_inventory_unreachable`, `test_csv_route_other_sections_skip_inventory_fetch`, `test_csv_route_fleet_section`, `test_an_inventory_page_the_report_cannot_use_is_like_an_unreachable_inventory`)
- **OPS-REPORT-15.** Free-text CSV cells (owner names, device names, and the browser's
  template names) are neutralized against spreadsheet formulas; numbers and enumerations
  are not. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`report_to_csv`); `frontend/src/pages/ReportingPage.tsx` (`csvSafeCell`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_report_to_csv_user_section_neutralizes_formula_trigger_in_owner_name`); `services/reservations/tests/test_fleet_report.py` (`test_report_to_csv_fleet_section_neutralizes_formula_trigger_in_device_name`); `frontend/src/test/pages/ReportingPage.test.tsx` (`neutralizes a by-template CSV row whose template name opens as a formula (issue #910)`)
- **OPS-REPORT-16.** The page shows headline numbers, the per-user, per-device, and
  per-template tables (the template rollup built in the browser), the fleet section with
  an idle-only toggle, a notice when the fleet is null, and an error strip when the
  report fails; presets request a fresh 7 or 30 day window and an end before the start is
  refused. \
  Enforced in: `frontend/src/pages/ReportingPage.tsx` (`ReportingPage`) \
  Pinned by: `frontend/src/test/pages/ReportingPage.test.tsx` (`populates the headline stats and the per-user and per-device tables`, `rolls up by-device hours into a by-template table`, `filters the fleet table to idle devices with the toggle`, `shows an unavailable notice when the fleet section is null`, `shows an error strip when the report request fails`, `switches to the 7-day and 30-day presets, requesting a fresh window each time`, `warns when a custom range has an end before its start`); `tests/e2e/test_reporting_page.py` (`test_reporting_page_shows_headline_cards`, `test_reporting_range_preset_buttons`)
- **OPS-REPORT-17.** The purpose section draws confirmed and suggested bars distinctly,
  shows transit columns, hides itself for a backend without `by_purpose`, and offers a
  CSV per section. \
  Enforced in: `frontend/src/pages/ReportingPage.tsx` (`ReportingPage`) \
  Pinned by: `frontend/src/test/pages/ReportingPagePurpose.test.tsx` (`renders suggested rows as additional, distinctly labeled bars with a legend`, `shows the Transit column, the mixed-row percentage, and the transit-only tag`, `hides the whole section cleanly when by_purpose is absent (older backend)`, `clicking each purpose CSV button downloads with that section's own name`)

**Out of scope.** Setting or classifying a purpose (`reservations.md`, `ai-features.md`).

### 8.15 In-app help link and author credit

**What it does.** The `?` icon in the header opens the published user manual in a new
tab. A one-line author credit, `Designed and built by Lane Nelms`, sits under the
sign-in, Register, config sign-in, and config change-password cards and in the About
page's Frontend card; the name links to the author's LinkedIn profile in a new tab.

**Surfaces.** `frontend/src/lib/links.ts`, `frontend/src/components/layout/HeaderHelpLink.tsx`,
`frontend/src/components/layout/AppLayout.tsx` (AppLayout renders the link),
`frontend/src/components/layout/AuthorCredit.tsx` (rendered by `LoginPage.tsx`,
`RegisterPage.tsx`, `ConfigPage.tsx`, and `admin/AboutPage.tsx` under
`frontend/src/pages/`).

**Rules.**

- **OPS-HELP-1.** The header links to `MANUAL_URL`, the published manual address, defined
  once. \
  Enforced in: `frontend/src/lib/links.ts` (`MANUAL_URL`); `frontend/src/components/layout/HeaderHelpLink.tsx` (`MANUAL_URL`) \
  Pinned by: `frontend/src/test/components/AppLayout.test.tsx` (`renders a Settings link and a help link to the published manual`); `frontend/src/test/components/HeaderHelpLink.test.tsx` (`is a link named Help that points at the published manual`)
- **OPS-CREDIT-1.** The author credit's wording and profile address are defined once,
  in `links.ts`, and every surface renders them through `AuthorCredit`. The link opens
  in a new tab with `rel="noopener noreferrer"`, names itself `Lane Nelms on LinkedIn
  (opens in a new tab)`, and follows every sign-in control in tab order: it sits in a
  footer after the card, never before the first field. On About it closes the Frontend
  card, after the Version, Build, and Build date list. \
  Enforced in: `frontend/src/lib/links.ts` (`AUTHOR_CREDIT_PREFIX`, `AUTHOR_NAME`, `AUTHOR_LINKEDIN_URL`); `frontend/src/components/layout/AuthorCredit.tsx` (`AuthorCredit`); `frontend/src/pages/LoginPage.tsx` (`AuthorCredit`); `frontend/src/pages/RegisterPage.tsx` (`AuthorCredit`); `frontend/src/pages/ConfigPage.tsx` (`AuthorCredit`); `frontend/src/pages/admin/AboutPage.tsx` (`AuthorCredit`) \
  Pinned by: `frontend/src/test/pages/LoginPage.test.tsx` (`renders under the card in local mode and follows every form control in tab order`, `renders while the sign-in options are pending, after the configuration link`, `renders with single sign-on only, after the Sign in with button`, `renders with the Administrator sign-in toggle, after the opened form's submit`, `renders with the LDAP answer, whose form is the local one`); `frontend/src/test/pages/RegisterPage.test.tsx` (`renders under the card and follows every form control in tab order`); `frontend/src/test/pages/ConfigPage.test.tsx` (`renders under the config sign-in card, after its controls in tab order`, `renders under the change-password card`); `frontend/src/test/pages/AboutPage.test.tsx` (`shows the author credit inside the Frontend card, after its Version, Build, and Build date`); `tests/e2e/test_login.py` (`test_login_page_has_author_credit`); `tests/e2e/test_register_and_roles.py` (`test_register_page_has_author_credit`); `tests/e2e/test_config.py` (`test_config_login_card_has_author_credit`)

**Out of scope.** The manual's content (`docs/manual/`).

### 8.16 Compose and image guards

**What it does.** Static tests keep the deployment files honest: every setting can be set
from `.env`, nothing but the gateway listens beyond the host, the gateway serves a
certificate the checkout generated, and the images run the locked dependency set.

**Surfaces.** `docker-compose.yml`, `docker-compose.override.yml`, the service
Dockerfiles, `frontend/nginx.conf`, the Makefile's `certs` target with `scripts/make_certs.sh` and
`infra/traefik/dynamic.yml`, and the unit tests under `tests/unit/` named below; the CI
backend job (`.github/workflows/ci.yml`).

**Rules.**

- **OPS-GUARD-1.** Every `Settings` field of every service is passed by the base compose
  file or is listed as an exemption with a reason. Passed means the whole value under
  the field's key is exactly `${VAR}` or `${VAR:-default}`, the default (when there is
  one) equal to the code's except for `CORS_ORIGINS`, whose compose default differs by
  decision, where `VAR` is the field's own name or, for `SECRET_KEY`,
  `ALGORITHM`, and auth's two token lifetimes, the renamed variable the guard records;
  `DATABASE_URL` is the one field built from several variables and must interpolate
  all three `POSTGRES_*` variables. Another variable, an escaped `$${VAR}`, text
  around the substitution, or any other substitution form (`${VAR-x}`, `${VAR:?x}`,
  `${VAR:+x}`, a bare `$VAR`) is not passed. A stale exemption or renamed-variable
  entry fails, and no field declares an alias. \
  Enforced in: `docker-compose.yml` (`environment`) \
  Pinned by: `tests/unit/test_compose_settings_wiring.py` (`test_service_names_matches_discovered_services`, `test_no_settings_field_declares_an_alias`, `test_every_settings_field_is_wired_or_exempt`, `test_no_stale_exemptions`, `test_no_stale_default_mismatch_exemptions`, `test_wired_compose_defaults_match_settings_defaults`, `test_every_active_ai_env_example_key_is_wired_to_ai_orchestrator`, `test_no_stale_renamed_wiring`)
- **OPS-GUARD-2.** The base compose file publishes Traefik's 80 and 443 to every
  interface and every other port, the Traefik dashboard included, on loopback only; the
  dev override adds no second dashboard binding and publishes its own ports on loopback
  unless listed with a reason. \
  Enforced in: `docker-compose.yml` (`127.0.0.1`); `docker-compose.override.yml` (`127.0.0.1`) \
  Pinned by: `tests/unit/test_compose_ports.py` (`test_base_compose_publishes_every_port_loopback_only_except_traefik_http_https`, `test_base_compose_publishes_traefik_dashboard_on_loopback_only`, `test_dev_override_adds_no_second_dashboard_binding`, `test_dev_override_publishes_every_port_loopback_only`)
- **OPS-GUARD-3.** NATS has no authentication; its client and monitoring ports are bound
  to loopback, and operators reach it from a container on the stack network or over an
  SSH forward. By decision; issue #708 and [OPERATIONS.md](../OPERATIONS.md) (Remote
  access to NATS and Postgres). \
  Enforced in: `docker-compose.yml` (`4222`) \
  Pinned by: `tests/unit/test_compose_ports.py` (`test_base_compose_publishes_every_port_loopback_only_except_traefik_http_https`)
- **OPS-GUARD-4.** The CI backend job builds the ai-orchestrator image and fails when the
  packages installed in it differ from the locked export. \
  Enforced in: `scripts/check_image_matches_lock.py` (`main`) \
  Pinned by: `tests/unit/test_check_image_matches_lock.py` (`test_main_matching_sets_pass`, `test_main_version_mismatch_fails_with_exact_message`, `test_main_missing_from_image_fails_with_exact_message`, `test_main_unexpected_in_image_fails_with_exact_message`)
- **OPS-GUARD-5.** The gateway serves a certificate the checkout generated, never
  Traefik's built-in one, to clients with and without SNI (issue #1239). No certificate
  or key is tracked; `make certs` writes `server.key` and a self-signed
  `server-chain.crt` valid for 3650 days, with the subject alternative names
  `DNS:localhost`, `IP:127.0.0.1`, the `HERD_GATEWAY_BIND` address unless it is unset or
  `0.0.0.0`, and each `CERT_SANS` entry (an entry that is not `DNS:<name>` or
  `IP:<address>` is refused and nothing is written), only when the key is absent; with
  both files present it runs no openssl and changes nothing, with the key present and
  the certificate missing it issues a certificate for that key, and only `CERT_FORCE`
  regenerates the key. `make dev`, `make prod` (so `make up`), and the gate's stack
  start depend on it after their ports guard, and `infra/traefik/dynamic.yml` names the
  same two files under `tls.certificates` and as the default store's
  `defaultCertificate`. Every GitHub workflow job that starts the stack with a bare
  `docker compose up` (ci.yml's integration job, nightly.yml's full-stack job) runs a
  `make certs` step before it. \
  Enforced in: `Makefile` (`certs`, `_master-stack-up`); `scripts/make_certs.sh` (`CERT_SANS`, `CERT_FORCE`, `HERD_CERT_BIND`); `infra/traefik/dynamic.yml` (`defaultCertificate`); `.github/workflows/ci.yml` (`make certs`); `.github/workflows/nightly.yml` (`make certs`) \
  Pinned by: `tests/unit/test_make_certs_wiring.py` (`test_stack_start_targets_depend_on_certs_after_their_ports_guard`, `test_start_targets_reach_make_certs_through_real_make`, `test_present_key_and_certificate_run_no_openssl_and_change_nothing`, `test_missing_certificate_is_issued_for_the_existing_key`, `test_fresh_checkout_gets_a_ten_year_certificate_naming_the_bind_address`, `test_every_interface_bind_names_only_localhost`, `test_cert_sans_entries_are_added_once_each`, `test_malformed_cert_sans_entry_is_refused_and_writes_nothing`, `test_cert_force_regenerates_both_and_says_so`, `test_dynamic_config_serves_the_pair_to_sni_and_no_sni_clients`, `test_no_certificate_or_key_is_tracked`, `test_every_workflow_stack_start_follows_a_make_certs_step`, `test_no_other_workflow_starts_the_stack`)

- **OPS-GUARD-6.** In `docker-compose.yml` and `docker-compose.override.yml` together,
  the services whose mounts reach the Docker socket are exactly the restarter and
  Traefik. A host path reaches it when, normalized, it is `/var/run/docker.sock`,
  `/run/docker.sock`, or a directory above one of them by whole path components; the
  mount may be a bind, a named volume whose `driver_opts.device` is such a path, or a
  top-level `configs` or `secrets` file the service uses, and no such top-level entry
  may exist at all. No service uses `volumes_from` or `extends`, or a `network_mode` of
  `service:` or `container:`. Traefik's socket mount names the socket itself. The
  restarter mounts exactly the socket at `/var/run/docker.sock`, is on the `herd-ctl`
  network only, publishes no port, carries no `traefik.*` label, and sets none of
  `user`, `devices`, `privileged`, `cap_add`, `pid`, `ipc`, `network_mode`, `configs`,
  or `secrets`; only config and the restarter join `herd-ctl`, which is
  `internal: true`, not external, not renamed, and not aliased by another network's
  `name`; the restarter runs with `read_only: true`, a `cap_drop` containing `ALL`,
  and `no-new-privileges` (any of its three spellings), which the override does not
  change; its `HERD_COMPOSE_PROJECT` is one `${COMPOSE_PROJECT_NAME:?...}`
  substitution; and the services other than config that mount `herd-config` equal
  `RESTART_SERVICES`, each read-only at `/etc/herd`. Traefik's other mounts and keys
  are out of this guard's reach by decision; issue #1424 tracks its socket. \
  Enforced in: `docker-compose.yml` (`restarter`, `herd-ctl`); `docker-compose.override.yml` (`config`) \
  Pinned by: `tests/unit/test_restarter_compose_guard.py` (`test_socket_holders_are_exactly_restarter_and_traefik`, `test_restarter_is_on_herd_ctl_only_with_no_ports_or_gateway_route`, `test_only_config_and_restarter_join_the_internal_herd_ctl`, `test_restarter_runs_read_only_without_capabilities`, `test_restarter_project_is_the_compose_project_name`, `test_restart_allowlist_equals_the_services_that_read_config_json`, `test_reaches_socket_compares_whole_components`, `test_each_check_fails_on_a_broken_file`, `test_no_check_fails_on_an_honest_change`)
- **OPS-GUARD-7.** Every `services/*/Dockerfile` ships a final stage that runs as a
  non-root user: the last `USER` of the final stage, or, when it has none, of the
  earlier stage its `FROM` names, followed through every such link; a stage built
  from an image starts as root, and `root`, any all-zero number (`0`, `00`, `0000`),
  an empty user, and a user spelled through a variable count as root. The restarter's
  `infra/restarter/Dockerfile` is the one exemption, by path with its reason, and must
  still run as root. \
  Enforced in: `services/config/Dockerfile` (`appuser`); `infra/restarter/Dockerfile` (`root`) \
  Pinned by: `tests/unit/test_dockerfile_users.py` (`test_every_service_dockerfile_is_scanned`, `test_the_image_runs_as_a_non_root_user_unless_exempt`, `test_every_exemption_names_a_scanned_file`, `test_reads_root`, `test_reads_non_root`)
- **OPS-GUARD-8.** An `/api/` path that no service router claims (its service stopped or
  unhealthy, so Traefik dropped the route, or a prefix no service owns) reaches the
  frontend's catch-all router, and the frontend's nginx answers it 503 with
  `application/json` and `{"detail": "This part of HERD is not answering right now. Try
  again in a moment."}`, never the web app's `index.html` with a 200, for any method and
  for a path ending like a static asset (`location ^~ /api/`, issue #1320). Page routes
  outside `/api/` are still the web app. \
  Enforced in: `frontend/nginx.conf` (`location ^~ /api/`) \
  Pinned by: `tests/unit/test_frontend_nginx_api_fallback.py` (`test_api_block_answers_503_json_with_the_fixed_detail`, `test_api_block_is_prefix_priority_so_the_asset_regex_cannot_claim_it`, `test_api_block_does_not_fall_back_to_the_spa`); `tests/integration/test_gateway_unrouted_api.py` (`test_an_unrouted_api_path_is_a_json_503_not_the_web_app`, `test_an_unrouted_api_path_ending_like_a_static_asset_is_still_the_json_503`, `test_a_page_route_still_serves_the_web_app`)

**Out of scope.** The build-identifier wiring guard (OPS-BUILD-2, OPS-BUILD-3); the NATS
CLI documentation guard (OPS-NATS-14).

### 8.17 Request validation errors

**What it does.** When a request does not have the shape a route expects (a field
missing, a value of the wrong type, a body that is not JSON), every HERD service answers
422 with a list that says where the problem is and what is wrong, without a field
holding what was sent (OPS-VALID-3 lists the text a message can still quote). So such
a refused request cannot copy a password, a secret's plaintext, a webhook signing
secret, or a device's credential fields into the answer, a proxy log, or a browser
toast, and no body that fails validation turns the 422 into a 500 (OPS-VALID-2,
OPS-VALID-5). A body holding text that cannot be encoded as UTF-8 (a lone surrogate,
written as an escape such as `\ud800` with no pair) is refused the same way in every
service, whatever field holds it, so no such text is stored, forwarded, or echoed
(OPS-VALID-6). A body larger than the service's limit (`MAX_REQUEST_BODY_BYTES`, 256
KiB by default; a route that takes a large JSON body, a canvas or a device
configuration, adds 4 MiB, an upload route adds the files it accepts, and a route
another service forwards a body to adds an envelope) is refused with 413 before it is
parsed, scanned, or authenticated, in every service, config included (OPS-VALID-9 to
OPS-VALID-12); a body one service admitted is not refused for its size by the
service it forwards it to (OPS-VALID-12).

**Surfaces.** `services/common/herd_common/validation_errors.py`, the ONE handler,
body check, and body limit, which each service's `app/main.py` installs; the config
service's standalone copy, `services/config/app/validation_errors.py` (OPS-CONFIG-30),
and its limit reader `services/config/app/request_limits.py`; the wiring guards
`tests/unit/test_validation_errors_wiring.py` and `tests/unit/test_request_body_limit_wiring.py`;
`services/common/herd_common/error_details.py`, the ONE detail for a value outside a
fixed set (OPS-VALID-7).

**Rules.**

- **OPS-VALID-1.** No 422 a request-validation failure produces, in any service, carries
  pydantic's `input` or `ctx` (issue #1455): the body is `{"detail": [...]}` and each
  item is exactly `{type, loc, msg}`, the shape each service's OpenAPI `ValidationError`
  component lists. So no such 422 holds a submitted value as such. Before #1455 only auth
  (IAM-HASH-9 in `identity-and-access.md`) and config (OPS-CONFIG-30) did this; every
  other service echoed `input`, which for a missing field is the whole body: a secrets
  create without `name` returned the plaintext `data`, and a webhook registration its
  `secret`. Because `input` is dropped, a refused body holding a lone surrogate is a 422
  too: the echo of it was a 500, since Starlette cannot encode a surrogate as UTF-8. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`render_validation_errors`, `validation_error_handler`, `restrict_validation_error_schema`, `install_validation_errors`, `RENDERED_KEYS`) \
  Pinned by: `services/common/tests/test_validation_errors.py` (`test_the_handler_keeps_type_loc_msg_and_drops_input_ctx_and_everything_else`, `test_render_takes_any_iterable_of_errors_and_an_error_without_a_loc`, `test_restrict_leaves_a_document_without_the_component_alone`, `test_install_registers_the_handler_for_request_validation_errors`, `test_install_makes_the_openapi_document_advertise_exactly_the_rendered_keys`, `test_an_installed_app_never_echoes_the_body`); `services/acl/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/ai-orchestrator/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/cabling/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/execution/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/integration/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/inventory/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/notifications/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/reservations/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/secrets/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/user-profile/tests/test_validation_errors.py` (`test_the_shared_handler_is_installed`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_a_refused_body_never_echoes_the_submitted_value`); `services/auth/tests/test_validation_errors.py` (`test_the_handler_is_installed_for_request_validation_errors`, `test_the_openapi_document_advertises_exactly_the_rendered_keys`, `test_every_rendered_error_has_exactly_the_advertised_keys`); `tests/integration/test_secrets_flow.py` (`test_a_refused_create_never_echoes_the_plaintext`)
- **OPS-VALID-2.** Every lone surrogate in a string in `loc` or `msg` is replaced with
  U+FFFD, so an error this handler renders always encodes as UTF-8. A constrained
  string (a length bound, a pattern) refuses a surrogate as `string_unicode` with a
  fixed message, but a plain `str` and the keys and values of a `dict[str, str]`
  accept one, so a key of a free-form mapping could carry one into `loc`. Since
  OPS-VALID-6 no JSON request body reaches the schema holding one in a service that
  installs the body check, so the replacement is defense in depth there; it is the rule in the
  config service, which installs its copy without the check. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`_encodable`, `render_validation_errors`) \
  Pinned by: `services/common/tests/test_validation_errors.py` (`test_the_handler_replaces_a_lone_surrogate_in_loc_and_msg_and_keeps_integers`, `test_the_handler_keeps_honest_non_ascii_text_unchanged`)
- **OPS-VALID-3.** It is not a promise that no submitted text appears, by decision (the
  module docstring of `herd_common.validation_errors` records the limits). `loc` names
  the keys the caller sent where a value failed or a key was refused: an unknown key on
  an `extra="forbid"` body, and a key of a free-form mapping, such as a key inside a
  secret's `data` (never its value). `msg` is the validator's own text: pydantic's names
  the permitted values of an enum or literal, never the input, but quotes the character
  at fault in a UUID, an email's invalid character, and the tag of a discriminated
  union; three of the services' own validators quote input, none of them a
  secret-bearing field: inventory's dropdown default and options, integration's unknown
  event types, and user-profile's `page_sizes` key names. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`render_validation_errors`) \
  Pinned by: `services/common/tests/test_validation_errors.py` (`test_an_installed_app_still_names_a_refused_key_in_loc`); `services/secrets/tests/test_validation_errors.py` (`test_loc_still_names_a_data_key_but_never_its_value_by_documented_limit`); `services/inventory/tests/test_validation_errors.py` (`test_msg_can_still_quote_a_dropdown_default_by_documented_limit`); `services/auth/tests/test_password_input_validation.py` (`test_msg_and_loc_can_still_quote_submitted_text`)
- **OPS-VALID-4.** Every service app installs the handler: each `app/main.py` passes
  every app it builds to `install_validation_errors` from `herd_common.validation_errors`
  in a module-level statement whose only other argument is the body limit,
  `max_body_bytes=settings.max_request_body_bytes` (OPS-VALID-9), which also installs
  the body check (OPS-VALID-6), except the config service, which installs its standalone
  copy because it must not import `herd_common`, passes
  `max_body_bytes=max_request_body_bytes()` from `app/request_limits.py`, and passes
  exactly `refuse_lone_surrogates=False`
  (by decision: it answers a lone surrogate in a password itself, OPS-CONFIG-8, and its
  settings save refuses one, OPS-CONFIG-31); the copy's code, docstrings aside, is
  identical to the shared module's. No module under a service's `app/` builds another
  FastAPI app, registers its own `RequestValidationError` handler, or replaces an app's
  `openapi`. The guard finds the services from the `services/` directory, so a new
  service is checked with no edit; its limits are in its module docstring. \
  Enforced in: `services/acl/app/main.py` (`install_validation_errors`); `services/ai-orchestrator/app/main.py` (`install_validation_errors`); `services/auth/app/main.py` (`install_validation_errors`); `services/cabling/app/main.py` (`install_validation_errors`); `services/execution/app/main.py` (`install_validation_errors`); `services/integration/app/main.py` (`install_validation_errors`); `services/inventory/app/main.py` (`install_validation_errors`); `services/notifications/app/main.py` (`install_validation_errors`); `services/reservations/app/main.py` (`install_validation_errors`); `services/secrets/app/main.py` (`install_validation_errors`); `services/user-profile/app/main.py` (`install_validation_errors`); `services/config/app/main.py` (`install_validation_errors`); `tests/unit/test_validation_errors_wiring.py` (`service_dirs`, `service_problems`, `install_problems`, `foreign_registrations`, `STANDALONE_COPY`, `BODY_CHECK_EXEMPT`) \
  Pinned by: `tests/unit/test_validation_errors_wiring.py` (`test_every_service_app_installs_the_shared_handler`, `test_no_other_module_builds_an_app_or_registers_another_handler`, `test_services_are_discovered_from_the_directory`, `test_fixture_mains_that_pass`, `test_fixture_mains_that_fail`, `test_config_must_install_its_copy_not_the_shared_module`, `test_config_passes_the_limit_its_environment_reader_returns`, `test_a_new_service_without_the_line_fails`, `test_only_an_exempt_service_may_opt_out_of_the_body_check`, `test_an_exempt_service_may_opt_out_only_with_exactly_that_keyword`, `test_only_config_may_opt_out_by_service_name`); `services/config/tests/test_validation_errors.py` (`test_the_code_is_identical_to_the_shared_module`, `test_the_code_comparison_sees_a_one_character_change`, `test_config_installs_its_copy_without_the_body_check`)

- **OPS-VALID-5.** A service's own validator never puts a submitted string into its
  error message as is; it quotes it with `repr` (or a list's `str`, which is the `repr`
  of each item), which writes a lone surrogate, and any control character, as its
  escape. Pydantic-core cannot build an error whose message holds a lone surrogate: a
  `ValueError` or `AssertionError` raised with one becomes a UnicodeEncodeError, not a
  validation error, so the handler never runs and the route answers 500. Before this
  rule, `POST /templates` on inventory with a dropdown default or option of `"Z\ud800"`
  was such a 500 (#1455 review); the message now reads `Default 'x' is not in options:
  'a', 'b'`, the options newly quoted. No guard scans for the rule; the module
  docstring of `herd_common.validation_errors` states it for validator authors. Since
  OPS-VALID-6 a request body holding a lone surrogate is refused before any validator
  runs, so through a route these three answer the body check's one `string_unicode`
  item at `["body"]`; the
  rule still holds for a validator run on any other data, and each one's `repr`
  quoting is pinned on the schema directly. \
  Enforced in: `services/inventory/app/schemas/template.py` (`validate_options_and_default`); `services/integration/app/schemas/webhook.py` (`_validate_events`); `services/user-profile/app/schemas/preferences.py` (`_validate_page_sizes`) \
  Pinned by: `services/common/tests/test_validation_errors.py` (`test_a_validator_message_holding_a_lone_surrogate_is_a_500_the_handler_cannot_see`, `test_a_validator_message_quoting_with_repr_is_a_clean_422`); `services/inventory/tests/test_validation_errors.py` (`test_a_lone_surrogate_the_dropdown_message_quotes_is_a_422_not_a_500`); `services/integration/tests/test_validation_errors.py` (`test_a_lone_surrogate_in_an_unknown_event_type_is_a_422_not_a_500`); `services/user-profile/tests/test_validation_errors.py` (`test_a_lone_surrogate_in_a_page_sizes_key_is_a_422_not_a_500`)

- **OPS-VALID-6.** Every service but config refuses a JSON request body that holds a
  lone surrogate (issues #1456, #1458, #1459): a plain `str` field, the keys and values
  of a `dict[str, str]`, and a value typed `Any` accept one, and every later step that
  must encode it as UTF-8 failed with a 500 (a JSON response, an httpx call to another
  service, a database write, a NATS payload). `install_validation_errors` adds
  `RefuseLoneSurrogates`, an ASGI middleware placed innermost. It looks only at a body
  FastAPI parses as JSON: content type `application/json` or an `application/*+json`
  subtype, decided with FastAPI's own header parsing; with `strict_content_type` on, as
  everywhere in HERD (a wiring guard fails when a service's source sets the option at all; it is an AST scan and does not see `setattr` or `**kwargs`), FastAPI does
  not parse a body that has no content type, so neither does the check. Any other body
  (multipart, a form, `text/plain`, octet-stream, or none) is passed through unread and
  unbuffered; a query string, path, urlencoded form, multipart field, header, and
  cookie cannot carry a lone surrogate anyway (each is percent-decoded with
  replacement, decoded strictly, or decoded as Latin-1). The first time a route reads a
  JSON body, the whole body is read and scanned as raw text, never parsed (OPS-VALID-8);
  if the scan finds a lone surrogate the read is refused with 422 and exactly one item,
  `{"detail": [{"type": "string_unicode", "loc": ["body"], "msg": "Input should be a
  valid string, unable to parse raw data as a unicode string"}]}` (138 bytes),
  pydantic's own type and message. By decision (round 4 of the #1456 review) the answer
  never names the field that holds the surrogate, and never holds the value; this holds
  for every route, so a field pydantic would refuse itself (a constrained string, which
  answered `string_unicode` at the field before) is answered at `["body"]` too, since
  the check runs first. Other rules that pin a surrogate answer point here. For a body
  that is valid JSON the scan refuses exactly when any member, also one a later
  duplicate key replaces, holds a lone surrogate once parsed; a valid surrogate pair
  written as two escapes is one code point and is accepted, as is all other text, NUL
  included. A body that is not valid JSON but holds a surrogate written as raw bytes, or
  an escape that would be one, is refused the same way instead of answered with the
  route's own JSON error (`json_invalid`); both are client errors. The check runs when
  the route reads its body, so an unknown route (404), a method not allowed (405), and a
  route that takes no body answer as before. Every route with a body reads it before its
  dependencies and its own validation run, so the refusal comes before authentication,
  before any role gate, and before the route's own checks of the body, to any caller
  with or without a token: the same order FastAPI uses for a body that is not JSON, and
  an answer that says nothing about the caller or any account. Other rules that state
  an order of checks point here for this exception. A JSON document a route parses
  itself from an uploaded file, a CSV cell, a stored secret, or config's settings save is
  checked by that code with `holds_lone_surrogate` on the parsed value (`inventory.md`,
  `topology.md`, OPS-SECRET-19, OPS-CONFIG-31). The only HERD route that reads a raw body
  itself, integration's test-stack webhook sink `POST /echo` (refused through the
  gateway), keeps only an event id from it and needs no check of its own. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`RefuseLoneSurrogates`, `_parses_as_json`, `_raw_text_holds_a_lone_surrogate`, `LONE_SURROGATE_BODY_ERROR`, `install_validation_errors`, `STRING_UNICODE_MSG`); `tests/unit/test_validation_errors_wiring.py` (`strict_content_type_settings`) \
  Pinned by: `services/common/tests/test_validation_errors.py` (`test_the_raw_scan`, `test_the_message_is_pydantics_own_for_a_constrained_string`, `test_a_lone_surrogate_anywhere_in_a_json_body_is_the_one_item`, `test_the_answer_is_these_exact_bytes`, `test_every_json_content_type_is_scanned`, `test_a_body_fastapi_does_not_parse_as_json_is_not_scanned`, `test_a_body_not_parsed_as_json_is_handed_on_as_it_arrives_unread`, `test_without_the_check_those_bodies_reach_the_route`, `test_honest_text_is_accepted`, `test_a_body_that_is_not_json_but_holds_a_surrogate_escape_is_the_one_item`, `test_a_route_that_reads_no_body_and_an_unknown_route_are_unchanged`, `test_the_refusal_comes_before_a_dependency_as_a_malformed_body_does`, `test_a_multipart_body_is_passed_through_and_cannot_carry_a_lone_surrogate`, `test_a_query_path_header_cookie_and_urlencoded_form_cannot_carry_one`, `test_a_body_split_across_messages_is_checked_whole`, `test_a_read_after_the_response_started_is_not_refused`, `test_the_refusal_survives_a_base_http_middleware_in_either_order`, `test_install_adds_the_check_unless_told_not_to`); `tests/unit/test_validation_errors_wiring.py` (`test_no_service_turns_off_strict_content_type`, `test_the_strict_content_type_scan_both_ways`); `services/auth/tests/test_federated_identity_routes.py` (`test_a_lone_surrogate_in_a_subject_is_the_shared_422_before_the_role_gate`); `services/auth/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_login_body_is_refused`); `services/acl/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/ai-orchestrator/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/cabling/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/execution/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/integration/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/inventory/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/notifications/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/reservations/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/secrets/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`); `services/user-profile/tests/test_validation_errors.py` (`test_the_body_check_is_installed`, `test_a_lone_surrogate_anywhere_in_a_body_is_refused`)
- **OPS-VALID-7.** A detail a route builds itself for a value outside a fixed set names
  the field and the allowed values, which are the service's own data, and never the
  submitted value: `Unknown <field>; allowed: <a>, <b>`, built by the ONE helper
  `unknown_value_detail` (issues #1458, #1459). An echo copied whatever a caller sent,
  of any length, into the answer, a proxy log, and a toast. Its users are reservations'
  `purpose_category` (RES-PURPOSE-1) and execution's `last_status` filter
  (OPS-HEALTH-4); execution's internal execute names its one allowed action in fixed text
  (CFG-EXEC-6). A detail may still quote an identifier the caller needs to see (a name in
  a 409, an id that was not found, a field key it sent); since OPS-VALID-6 such text can
  no longer hold a lone surrogate. \
  Enforced in: `services/common/herd_common/error_details.py` (`unknown_value_detail`) \
  Pinned by: `services/common/tests/test_error_details.py` (`test_the_detail_names_the_field_and_the_allowed_values_in_order`, `test_the_allowed_values_may_be_any_iterable`, `test_an_empty_allowed_set_still_names_the_field`)
- **OPS-VALID-8.** The check's whole cost is one scan of the raw bytes of a JSON body;
  it never parses one, so a body it refuses costs the scan alone, and the route never
  parses it. A body with no `\u` and no byte 0xED passes after two substring searches.
  Otherwise the scan replaces each escaped backslash pair (so every backslash left
  starts an escape) and lower-cases the text, at most two copies of the body made in C,
  then searches for a high-surrogate escape not followed by a low one (a search with a
  literal prefix) and, with none, counts low and high surrogate escapes: a low one is
  lone exactly when there are more lows than highs. A raw UTF-8 surrogate is one more
  search. Every pass runs in C; time is linear in the body's length and memory at most
  about two copies of the UTF-8 text, whatever its number of elements or escapes (a
  UTF-16 or UTF-32 body is decoded first, so the scan alone peaked at 4.0 times the
  body for a UTF-16 body holding a character outside the BMP). It was
  fuzzed against `json.loads` (2,600,000 bodies from two generators, zero mismatches on
  valid JSON: refused exactly when any member, also one a later duplicate key replaces,
  holds a lone surrogate). Measured through a FastAPI app whose route sits behind a
  dependency that answers 401, pinned to one core, against the same app without the
  check, at 1, 4, and 16 MB (observations, not a bound): a structural body refused for a
  trailing lone surrogate costs 0.01 to 0.10 times what the same request costs without
  the check, which parses it (chains of 900 nested lists 2.5 ms against 70 ms at 1 MB,
  21 ms against 1,445 ms and 16 MB of memory against 787 MB at 16 MB), and so does a
  structural body that is not JSON (0.02 to 0.07 times; a body that is not JSON and is
  made of escapes, such as repeated `\ud`, costs 17 to 33 times, about 7 ms per MB,
  because the parser stops at its first bad escape); a `text/plain` body costs the same as without
  the check. The scan's own worst case is a string made of escapes, which `json.loads`
  decodes at 1 to 3 ms per MB: escaped surrogate pairs, accepted, add 7.3 to 7.8 ms per
  MB, 4.9 to 7.6 times the request without the check (25 ms against 143 ms at 16 MB),
  the worst ratio measured; other escape runs add 0.1 to 8.4 ms per MB (1.0 to 5.6
  times). The check runs on the event loop, like FastAPI's own parse, so while it runs
  the service answers nothing else. Its cost grows with the body's length; what the
  service pays without it, the parse, grows with it too, and only a bound on the body's
  size limits either: since OPS-VALID-9 the scan never sees more than the limit. Before round 4 of
  the review the check parsed every body that was not multipart, `text/plain`
  included (a 16 MB chain cost 1,693 ms and 802 MB against 1.8 ms and 17 MB), and
  searched it for locations. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`_raw_text_holds_a_lone_surrogate`, `_escapes_hold_a_lone_surrogate`, `_HIGH_NOT_PAIRED`, `_LOW_ESCAPES`, `_HIGH_ESCAPES`, `holds_lone_surrogate`) \
  Pinned by: `services/common/tests/test_validation_errors.py` (`test_a_structural_body_costs_a_small_part_of_its_parse`, `test_an_escape_heavy_body_costs_linear_work_in_two_copies`, `test_a_utf16_body_costs_its_decode_and_the_escape_passes`, `test_a_body_with_no_surrogate_marker_is_passed_after_two_searches`, `test_through_an_app_a_refused_structural_body_costs_less_than_without_the_check`, `test_holds_lone_surrogate_costs_about_one_serialization`, `test_holds_lone_surrogate`, `test_holds_lone_surrogate_on_a_value_too_deep_to_serialize`, `test_holds_lone_surrogate_answers_at_every_depth_where_serializing_stops`, `test_the_walk_finds_a_lone_surrogate_wherever_it_sits`); `services/inventory/tests/test_lone_surrogate_cost.py` (`test_a_deep_and_wide_json_import_file_costs_about_one_parse`, `test_a_deep_and_wide_csv_json_cell_costs_about_one_parse`); `services/cabling/tests/test_lone_surrogate_cost.py` (`test_a_deep_and_wide_json_import_file_costs_about_one_parse`); `services/secrets/tests/test_lone_surrogate_cost.py` (`test_deserializing_deep_and_wide_data_costs_about_one_parse`); `services/config/tests/test_lone_surrogate_cost.py` (`test_the_save_check_on_deep_and_wide_values_costs_about_one_parse`)

- **OPS-VALID-9.** Every service, config included, refuses a request body larger than
  its route's limit with 413 (issue #1466). Before this rule nothing bounded a body:
  the gateway has no body limit, uvicorn has none, and FastAPI reads and parses a whole
  JSON body before a route's dependencies run, so a client with no token made acl parse
  a 9.8 MB list (58 MB of memory, 0.23 s of the event loop) before its 401.
  `install_validation_errors` adds `LimitRequestBody`, a pure ASGI middleware, just
  outside `RefuseLoneSurrogates` (in config, which installs without that check, it is
  the innermost middleware), with the service's `MAX_REQUEST_BODY_BYTES`, default
  262144 (256 KiB since issue #1484; 4194304 before it, when every route took what a
  canvas needs, so every unauthenticated route parsed up to 4 MiB): the Settings
  field `max_request_body_bytes` of `HerdBaseSettings`, or
  in config, which has no Settings model, `app/request_limits.py`. Both read the value
  with `max_body_bytes_from_text`: ASCII decimal digits, surrounding whitespace
  ignored, at least `MIN_MAX_REQUEST_BODY_BYTES` (65536); a sign, a decimal point, an
  exponent, or an underscore is refused. A service refuses to start on a value it
  refuses (a number in `config.json` is held to the same floor); config logs a warning
  and uses the default instead, as it does for its login limits, and reads only its
  environment, so a key written into `config.json` by hand does not reach it. The
  limit is judged at the first read of the body, after routing. A `Content-Length`
  declaring more than the limit is refused before one byte of the body is read; the
  digits are compared exactly, whatever their number. Otherwise each `http.request`
  message is counted as it is handed on, unchanged, and the read that takes the count
  past the limit is refused instead of handed on, so a chunked body, or one longer than
  its `Content-Length`, never reaches the route past the limit. A body of exactly the
  limit is accepted. A `Content-Length` that is not a whole number (empty, negative,
  signed, decimal, text) declares nothing, and the count alone bounds that body; when
  there are several, each is judged. The answer is `{"detail": [{"type":
  "body_too_large", "loc": ["body"], "msg": "Request body is larger than <limit>
  bytes"}]}`: the one-item shape of OPS-VALID-6's refusal, naming the limit and never
  anything of the body. It does not close the connection (the first version did, and a
  client still writing its body, httpx or the gateway, then met a connection reset
  instead of the 413). What is left of the body is read and discarded by uvicorn, in
  both its h11 and its httptools protocol, which drop body data that arrives once the
  response is complete, and the connection then serves the next request. So memory and
  parse work stay bounded (refusing a 64 MiB chunked body at a 4 MiB limit peaked at 5.8 MiB of Python
  allocations in the process, client included, the same as for 16 MiB), but the bytes
  still cross the network and cost the reading: this check bounds memory and parsing,
  not bandwidth. Every route with a body (JSON or a form) reads it before its
  dependencies, so the refusal comes before authentication and any role gate, to any
  caller with or without a token, and the surrogate check reads through the limit, so a
  body over it is refused before it is scanned or parsed. A route that never reads its
  body, an unknown route (404), and a method not allowed (405) answer as before, and the
  route reads nothing of the body, with one exception, a known limit: a route that
  answers with a streaming response without reading its body. Starlette's disconnect
  listener then reads the body. Nothing is refused once the response has started, and
  because the listener's first read can come while that response's start is still
  scheduled, the check yields to the event loop once before it refuses: if the response
  has started by then, the read answers `http.disconnect` instead, as does every read
  after it, which the listener takes as the client leaving. In a HERD service, built
  with the request logger (a BaseHTTPMiddleware) around the app, the start is still
  held in the logger's stream at that point, so: a body whose `Content-Length` is over
  the limit is refused, the response never starts, and Starlette raises `RuntimeError:
  No response returned.`, a 500 to the client (or a reset when the server cannot send
  it); a chunked body is read until the count passes the limit after the start, and the
  stream is cut short. In both cases nothing of the body reaches the route and no byte
  past the limit is handed on, but the route does not answer as before. So a route that
  streams takes its body as a parameter, which FastAPI reads before the route runs, or
  is listed by decision, with a reason, in the wiring guard's `STREAMING_WITHOUT_BODY`
  (none is; issue #1483 chose this guard over changing the limit). The guard finds a
  streaming route (a class named `StreamingResponse` or `EventSourceResponse` in the
  MRO of its `response_class`, of its endpoint's return annotation, or of a call its
  endpoint returns, directly or through a local name assigned once) in every built app
  and fails on one with no body parameter that is not listed; it does not see a stream
  built in another function, returned from a dependency, or sent through the ASGI
  `send`. No HERD route is affected: the one streaming route, the reservation
  assistant, takes its JSON body as a parameter. A client that sends `Expect: 100-continue` with a `Content-Length` over
  the limit gets the 413 without a `100 Continue`, since the check refuses before the
  first read that would send it; such a client must not send the body, and the server
  then waits for that body on the connection, so a client that reused the connection
  for its next request would have that request read and discarded as the refused
  body. HTTP clients close such a connection instead of reusing it, so this is a
  limit, not a defect. A websocket and the lifespan pass through untouched. Each refusal logs one
  INFO line (`action` `request_body_too_large`, the limit, and `declared` or
  `received`), never any of the body. The one raw read of a body in HERD, integration's
  test-stack webhook sink `POST /echo`, takes the default. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`LimitRequestBody`, `declares_more_than`, `route_body_limit`, `body_too_large_error`, `BODY_TOO_LARGE_TYPE`, `DEFAULT_MAX_REQUEST_BODY_BYTES`, `MIN_MAX_REQUEST_BODY_BYTES`, `max_body_bytes_from_text`, `install_validation_errors`, `_log_refusal`); `services/common/herd_common/base_settings.py` (`HerdBaseSettings`, `MAX_REQUEST_BODY_BYTES_RULE`); `services/config/app/request_limits.py` (`max_request_body_bytes`); `tests/unit/test_request_body_limit_wiring.py` (`built_app`, `MIDDLEWARE`, `streaming_reasons`, `STREAMING_WITHOUT_BODY`) \
  Pinned by: `services/common/tests/test_request_body_limit.py` (`test_declares_more_than`, `test_declares_more_than_is_exact_for_any_limit`, `test_the_one_item_names_the_limit_and_nothing_else`, `test_the_default_and_the_allowances_are_pinned`, `test_the_setting_is_read_as_decimal_digits_at_least_the_floor`, `test_a_declared_length_over_the_limit_is_refused_before_anything_is_read`, `test_a_body_of_exactly_the_limit_is_handed_on`, `test_a_chunked_body_is_refused_at_the_read_that_crosses_the_limit`, `test_a_body_longer_than_its_content_length_is_refused_by_the_count`, `test_an_invalid_content_length_leaves_the_count_to_bound_the_body`, `test_after_the_response_started_nothing_is_refused_and_the_reading_stops`, `test_after_the_response_started_a_declared_length_is_not_refused`, `test_a_read_racing_a_response_start_is_not_refused`, `test_a_streaming_route_that_never_reads_its_body_answers_with_no_middleware_around`, `test_known_limit_a_streaming_route_that_never_reads_its_body_fails_behind_the_logger`, `test_known_limit_the_same_route_with_a_chunked_body_over_the_limit_is_cut_short`, `test_a_disconnect_and_later_reads_pass_through`, `test_a_websocket_or_lifespan_scope_is_passed_through_untouched`, `test_a_request_whose_body_is_never_read_judges_nothing`, `test_a_refusal_logs_the_limit_and_the_reason_never_the_body`, `test_the_refusal_is_these_exact_bytes`, `test_over_the_limit_is_413_before_authentication`, `test_a_body_over_the_limit_is_never_parsed`, `test_a_body_over_the_limit_is_never_scanned_for_a_lone_surrogate`, `test_a_chunked_body_over_the_limit_is_refused_before_the_surrogate_check_buffers_it`, `test_a_chunked_body_over_the_limit_is_413_through_the_app`, `test_every_content_type_is_bounded`, `test_a_route_that_reads_no_body_an_unknown_route_and_a_stream_are_unchanged`, `test_the_limit_holds_without_the_surrogate_check`, `test_install_puts_the_limit_just_outside_the_surrogate_check`, `test_install_refuses_a_limit_that_is_not_a_positive_int`, `test_install_requires_the_limit`, `test_the_refusal_survives_a_base_http_middleware_in_either_order`); `services/common/tests/test_request_body_limit_socket.py` (`test_every_refusal_reaches_the_client_and_the_connection_serves_the_next_request`, `test_a_body_at_the_limit_still_reaches_the_route`, `test_refusing_a_64_mib_chunked_body_holds_no_more_than_the_limit`); `services/common/tests/test_base_settings.py` (`test_the_body_limit_defaults_to_256_kibibytes`, `test_the_body_limit_reads_decimal_digits_from_env_and_numbers_from_the_file`, `test_the_body_limit_refuses_what_config_also_refuses`, `test_the_body_limit_floor_holds_for_a_number_in_the_config_file`); `services/common/tests/test_validation_errors.py` (`test_install_adds_the_check_unless_told_not_to`); `services/acl/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/ai-orchestrator/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/auth/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/cabling/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/execution/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/integration/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/inventory/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/notifications/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/reservations/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/secrets/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/user-profile/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_just_outside_the_body_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422`, `test_without_credentials_the_limit_answers_before_authentication`); `services/config/tests/test_request_body_limit.py` (`test_the_body_limit_is_installed_innermost_without_the_surrogate_check`, `test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed`, `test_a_chunked_body_over_the_limit_is_413`, `test_without_a_session_the_limit_answers_before_the_session_check`, `test_the_reader_takes_decimal_digits_at_least_the_floor`, `test_a_value_the_services_refuse_is_the_default_with_a_warning`, `test_the_reader_is_the_copy_of_the_shared_reading`); `tests/unit/test_request_body_limit_wiring.py` (`test_every_service_is_found`, `test_the_installed_middleware_is_exactly_the_declared_list`, `test_the_middleware_table_names_every_service`, `test_the_limit_is_the_services_setting`, `test_compose_passes_the_setting_with_the_shared_default`, `test_every_streaming_route_reads_its_body_first_or_is_listed`, `test_the_streaming_exemptions_name_only_existing_services_with_reasons`, `test_the_scan_sees_the_one_streaming_route_herd_has`, `test_a_streaming_route_with_no_body_parameter_is_flagged`, `test_an_honest_route_is_not_flagged`, `test_a_flagged_route_passes_only_while_it_is_listed`); `tests/integration/test_request_body_limit.py` (`test_a_body_over_the_limit_is_413_through_the_gateway`, `test_a_chunked_body_over_the_limit_is_413_through_the_gateway`, `test_a_body_at_the_limit_reaches_the_parser_through_the_gateway`, `test_the_connection_serves_a_request_after_each_refusal`)
- **OPS-VALID-10.** A route whose body may hold more than the default declares it: its
  endpoint is marked with `extends_body_limit(extra_bytes)`, the ONE way to raise a
  route's limit, and its limit is `MAX_REQUEST_BODY_BYTES + extra_bytes()`, where
  `extra_bytes` is read at each request. The extra bytes add to the setting, so an
  operator who raises `MAX_REQUEST_BODY_BYTES` raises every marked route by as much (a
  large-JSON route takes the setting plus 4 MiB, never less than either). It is used
  for three reasons only. An upload route adds the files it bounds itself, read from
  the route's own setting so the two cannot drift, and the default covers the form's
  other fields. A route that legitimately takes a large JSON body adds
  `LARGE_JSON_BODY_BYTES` (4194304, issue #1484): a canvas, a device configuration, an
  AI commit that builds a canvas, and a bulk connection create, the routes whose
  largest legitimate body was measured over the default (or has no bound in its schema
  and can pass it in honest use). A route another HERD service forwards a body to adds
  `FORWARD_ENVELOPE_BYTES` (262144), what the sender adds to the body it admitted
  (OPS-VALID-12), on top of `LARGE_JSON_BODY_BYTES` when the sender admitted that body
  through a large-JSON route. Every route that takes a form (a `File` or `Form`
  parameter) carries the mark unless it is named, with a reason, in the guard's
  `FORM_EXEMPT` (none is: no HERD route takes a small form). A guard builds every
  service's app and fails on an unmarked form route, on a marked route outside this
  table or a row whose route is not marked, on extra bytes that are not the sum of the
  route's rows or do not follow its setting, and on a JSON route whose schema admits a
  body over the shipped default without a mark. That estimate (`schema_body_estimate`,
  read from the OpenAPI document) is an upper bound on the compact body a standard
  encoder writes, assuming canonical UUIDs and timestamps, 6 bytes a character of a
  free string, and 24 a number; it does not count whitespace a client adds. A schema
  that bounds nothing (a free-form mapping, a model that keeps extra keys, a string
  or list with no bound) has no estimate, and the guard fails on such an unmarked
  route unless its `UNBOUNDED_AT_THE_DEFAULT` table names it or it is measured, as below. The routes,
  with the default totals:

  | Route | Service | Extra bytes | Limit with the defaults |
  |---|---|---|---|
  | `POST /drivers`, `PUT /drivers/{driver_id}/file` | inventory | `DRIVER_MAX_SIZE_BYTES` (10485760) | 10747904 |
  | `POST /devices/import`, `POST /templates/import` | inventory | `IMPORT_UPLOAD_BYTES`, 10485760 fixed in `app/routers/bulk.py`; `_read_capped` applies the same number to the file itself (INV-BULK-23) | 10747904 |
  | `POST /topologies/import` | cabling | `IMPORT_UPLOAD_BYTES`, 10485760 fixed in `app/routes/bulk.py`; `_read_capped` applies the same number to the file itself (TOPO-BULK-20) | 10747904 |
  | `POST /generate` | ai-orchestrator | `UPLOAD_MAX_FILES` times `UPLOAD_MAX_FILE_BYTES` (5 times 5242880), the aggregate `_read_uploads` enforces | 26476544 |
  | `POST /internal/validate-package` | execution | the base64 length of `VALIDATE_PACKAGE_MAX_BYTES` (13981016 for 10485760), the JSON `package_b64` the validator decodes | 14243160 |
  | `PUT /topologies/{topology_id}`, `POST /templates`, `PUT /templates/{template_id}` | cabling | `LARGE_JSON_BODY_BYTES`: a canvas. In the shape the topology editor's Save sends (UUID node and edge ids, the seven persisted device keys plus `label` and `topologyType`, React Flow's `measured`, `selected`, and `dragging`, and each link's port ids, port names, `portsCabled`, `pathValid`, and `pathHopCount`) a device node is about 520 bytes and a link about 460: 1,000 devices and 4,000 links are 2361613 bytes, a 250-device reservation's canvas with 1,000 links is 590443, already over the default, and this limit holds 1,887 devices with four links each (1,776 within the 4 MiB allowance alone) | 4456448 |
  | `POST /connections/bulk` | cabling | `LARGE_JSON_BODY_BYTES`: 200 items at the schema's bounds, 2,723 bytes each of ASCII text, 544811 bytes in all (3104811 at the guard's 6 bytes a character) | 4456448 |
  | `PUT /{reservation_id}/fork/canvas`, `POST /{reservation_id}/fork/save` | reservations | `LARGE_JSON_BODY_BYTES`: the fork's canvas | 4456448 |
  | `POST /commit` | ai-orchestrator | `LARGE_JSON_BODY_BYTES`: a whole topology's devices with their configurations, links, and elements, from which the commit builds a canvas (no bound in its schema) | 4456448 |
  | `POST /devices/{device_id}/config-versions` | inventory | `LARGE_JSON_BODY_BYTES`: a device configuration (no bound in its schema), which may be a whole running configuration | 4456448 |
  | `PUT /internal/forks/{reservation_id}/canvas`, `POST /internal/forks/{reservation_id}/save` | cabling | `LARGE_JSON_BODY_BYTES` plus `FORWARD_ENVELOPE_BYTES`: the fork canvas reservations admitted | 4718592 |
  | `POST /execute`, `POST /execute/internal` | execution | `LARGE_JSON_BODY_BYTES` plus `FORWARD_ENVELOPE_BYTES`: a configuration inventory's config-versions route or ai-orchestrator's commit admitted | 4718592 |
  | `POST /` | reservations | `FORWARD_ENVELOPE_BYTES` | 524288 |
  | `PATCH /preferences` | user-profile | `FORWARD_ENVELOPE_BYTES` | 524288 |

  The routes measured to fit the default stay unmarked: `POST /pathfind/batch`
  (cabling), whose largest body, 2,000 pairs of two device ids, is 236011 bytes as
  compact JSON (244011 with Python's default separators, 288019 indented by two
  spaces, so a pretty-printing client near the bound gets the 413); and user-profile's `PUT /preferences` and `PATCH /preferences`, whose
  largest is 139319 bytes (two blobs at their 64 KiB cap as the validator serializes
  them, and 200 page sizes under 32-character keys; the schema does not bound a page
  size key's length). Every other unmarked JSON route whose schema bounds nothing
  stays at the default by decision, and the guard's `UNBOUNDED_AT_THE_DEFAULT` table
  is that decision: it names each such route with why its legitimate body is small
  (among them inventory's device and template create and update, whose `field_data`
  and field sections are a form's values, and the secrets create and update, where
  256 KiB holds a full public CA bundle in PEM), and the guard fails on an unmarked
  route with no bound that the table does not name and on a row whose route is gone,
  marked, or bounded now. Raising `MAX_REQUEST_BODY_BYTES` is the remedy for a larger
  body. Where an upload route bounds its files itself (every
  upload row but the imports), a file just over that bound still gets the route's own
  answer, its status and wording, since the body that holds it is under the route's
  limit; a body past the files and the default is the 413 naming the route's limit.
  The routes that take the envelope are also called from outside HERD (the browser,
  the facade's callers), so the envelope raises what any caller may send them by 256
  KiB. A large-JSON route parses up to its limit before authentication runs, like any
  route; so the default bounds what an unauthenticated caller can make every other
  route parse. An operator who set `MAX_REQUEST_BODY_BYTES=4194304` explicitly keeps 4 MiB on every route, gaining nothing, and the large-JSON routes then take 8 MiB and the routes they forward to 8.25 MiB; remove an explicit 4194304 to take the new default. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`extends_body_limit`, `route_body_limit`, `EXTRA_BODY_BYTES_ATTRIBUTE`, `FORWARD_ENVELOPE_BYTES`, `LARGE_JSON_BODY_BYTES`); `services/cabling/app/routes/topologies.py` (`update_topology`); `services/cabling/app/routes/templates.py` (`create_template`, `update_template`); `services/cabling/app/routes/connections.py` (`create_connections_bulk_endpoint`); `services/ai-orchestrator/app/routes/commit.py` (`commit`); `services/inventory/app/routers/device_configs.py` (`create_config_version`); `services/inventory/app/routers/drivers.py` (`create_new_driver`, `replace_driver_file_endpoint`); `services/inventory/app/routers/bulk.py` (`IMPORT_UPLOAD_BYTES`, `import_devices_endpoint`, `import_templates_endpoint`); `services/cabling/app/routes/bulk.py` (`IMPORT_UPLOAD_BYTES`, `import_topologies_endpoint`); `services/ai-orchestrator/app/routes/generate.py` (`generate`); `services/execution/app/routers/validation.py` (`base64_length`, `validate_package_endpoint`); `services/cabling/app/routes/forks.py` (`update_fork_canvas_internal`, `save_fork_internal`); `services/execution/app/routers/executions.py` (`manual_execute`, `internal_execute`); `services/reservations/app/routers/reservations.py` (`create_new_reservation`, `update_reservation_fork_canvas`, `save_reservation_fork`); `services/user-profile/app/routers/preferences.py` (`patch_preferences_endpoint`); `tests/unit/test_request_body_limit_wiring.py` (`UPLOAD_ROUTES`, `JSON_UPLOAD_ROUTES`, `LARGE_JSON_ROUTES`, `FORWARD_ROUTES`, `FORM_EXEMPT`, `MEASURED_UNDER_THE_DEFAULT`, `UNBOUNDED_AT_THE_DEFAULT`, `marked_route_problems`, `schema_body_estimate`, `unbounded_problems`) \
  Pinned by: `services/common/tests/test_request_body_limit.py` (`test_route_body_limit`, `test_route_body_limit_of_a_large_json_route_adds_the_allowance_to_any_setting`, `test_extends_body_limit_returns_the_endpoint_unchanged`, `test_a_marked_route_takes_its_own_limit`, `test_an_upload_route_takes_its_own_limit_and_another_form_does_not`); `services/inventory/tests/test_request_body_limit.py` (`test_an_upload_route_takes_its_own_limit`, `test_the_upload_routes_follow_the_driver_setting_and_the_import_bound`, `test_a_large_json_route_takes_the_allowance`, `test_the_larger_routes_are_exactly_the_uploads_and_the_config_version_create`); `services/reservations/tests/test_request_body_limit.py` (`test_a_large_json_route_takes_the_allowance`); `services/user-profile/tests/test_request_body_limit.py` (`test_the_largest_preferences_body_fits_the_default_and_is_valid`); `services/common/tests/test_request_body_limit_socket.py` (`test_a_large_json_route_refuses_one_byte_over_its_own_limit_and_keeps_the_connection`, `test_a_body_at_a_large_json_routes_limit_reaches_it`); `services/cabling/tests/test_request_body_limit.py` (`test_the_import_route_takes_its_own_limit`, `test_the_larger_routes_are_exactly_the_import_the_large_json_routes_and_the_fork_forwards`, `test_a_large_json_route_takes_the_allowance`, `test_a_generated_large_canvas_is_over_the_default_and_under_the_allowance`, `test_the_largest_pathfind_batch_fits_the_default_and_is_not_marked`, `test_the_largest_bulk_connection_create_is_over_the_default`); `services/ai-orchestrator/tests/test_request_body_limit.py` (`test_the_generate_route_takes_its_own_limit`, `test_the_larger_routes_are_exactly_generate_and_commit`, `test_a_large_json_route_takes_the_allowance`); `services/execution/tests/test_request_body_limit.py` (`test_base64_length_is_the_padded_length`, `test_the_package_validator_takes_its_own_limit`); `services/cabling/tests/test_request_body_limit.py` (`test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413`); `services/execution/tests/test_request_body_limit.py` (`test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413`); `services/reservations/tests/test_request_body_limit.py` (`test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413`); `services/user-profile/tests/test_request_body_limit.py` (`test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413`); `tests/unit/test_request_body_limit_wiring.py` (`test_every_marked_route_is_in_the_tables_and_every_form_is_marked`, `test_the_mark_check_fails_both_ways`, `test_the_route_tables_name_only_existing_services_and_routes`, `test_each_measured_route_fits_the_default_and_is_not_a_large_json_route`, `test_every_bounded_json_route_over_the_default_is_marked`, `test_the_estimate_matches_each_bounded_measure`, `test_schema_body_estimate`, `test_the_estimate_is_the_compact_json_of_the_largest_body`, `test_every_unbounded_unmarked_json_route_is_listed_with_a_reason`, `test_the_unbounded_table_names_existing_services_with_reasons`, `test_the_unbounded_check_fails_both_ways`, `test_the_specification_names_every_marked_route`); `tests/integration/test_request_body_limit.py` (`test_a_canvas_save_at_the_large_json_limit_succeeds_and_one_byte_more_is_413`, `test_a_large_json_route_reads_a_body_over_the_default_to_authentication`, `test_a_fork_canvas_over_the_default_is_saved_through_reservations_to_cabling`)
- **OPS-VALID-11.** The limit's cost is constant per message, on the event loop like
  everything else in the request: per request two small closures; at the first read
  one pass over the headers and the route lookup; per `http.request` message one
  addition and one comparison; one extra pass of the event loop before a refusal. It
  never copies, joins, or scans the body: the messages it hands on are the ones it
  received. Measured pinned to one core through an ASGI client against the same app
  without it (observations, not a bound): 4.9 microseconds more per small JSON request
  (1.02 times), 20 more for a 1 MB body in one message, 84 more for a 1 MB body in 256
  messages (about 0.3 a message). A declared body over the limit costs no read by the
  app: a 9 MB structural body to a route behind a 401 dependency costs 0.4 ms refused
  against 300 ms parsed. A chunked body over the limit costs the app reading up to the
  limit and one message past it; after the 413 the server still reads and discards the
  rest (OPS-VALID-9), which over loopback cost about 0.09 s of CPU for 64 MiB. \
  Enforced in: `services/common/herd_common/validation_errors.py` (`LimitRequestBody`) \
  Pinned by: `services/common/tests/test_request_body_limit.py` (`test_messages_are_handed_on_as_received_never_copied_or_joined`, `test_handing_on_a_body_allocates_nothing_that_grows_with_it`, `test_the_cost_per_message_does_not_grow_with_its_size`, `test_through_an_app_an_over_limit_body_costs_less_than_parsing_it`); `services/common/tests/test_request_body_limit_socket.py` (`test_refusing_a_64_mib_chunked_body_holds_no_more_than_the_limit`)
- **OPS-VALID-12.** A body one HERD service admitted is not refused for its size by the
  service it forwards it to, by decision of the #1466 review. Every outbound call that
  sends a body (40 call sites in `app/` and `herd_common`, found by a source scan) is
  classified in the guard's `FORWARDS` table, and a new one fails the guard until it
  is. The scan finds a call with a `json=` or non-`None` `json_body=` keyword; an
  httpx method, `client.request`, `client.stream`, `client.build_request`, or
  `httpx.Request` with a body keyword or a `**` splat; and a call to one of HERD's own
  forwarders (`call_service`, `_cabling_fork_call`, `_execution_wiring_call`, the
  facade's `_forward`) with a body by keyword or by position. Each call site is keyed
  by its function and its receiver (the method and the URL or path as written, with an
  ordinal for a repeat), never by its line, so a second body call in a classified
  function fails until it is classified and moving a call does not. It does not see a
  body sent through a forwarder not yet named in the guard, a client method of another
  name or a non-httpx client, or a call made through `getattr` or a function held
  under another name; the receiver in a key is the text the code wrote, not the URL it
  resolves to. Each forward that takes the envelope names its source, the route that
  admitted what travels (or none, when the sender builds a body of the receiver's own
  schema from fields it bounded), and the receiver must add at least what its source
  adds plus `FORWARD_ENVELOPE_BYTES`, so a body a large-JSON route admitted is
  forwarded to a route with the large-JSON allowance plus the envelope (issue #1484):

  | Sender | Receiver | What travels | How it fits |
  |---|---|---|---|
  | reservations fork canvas PUT and fork save | cabling `PUT /internal/forks/{reservation_id}/canvas`, `POST /internal/forks/{reservation_id}/save` | the canvas the user sent, admitted by a large-JSON route; the save adds `created_by` and at most 250 member device ids (about 10 KB) | large-JSON allowance plus envelope |
  | inventory immediate apply; ai-orchestrator commit with `apply_configs` | execution `POST /execute` | a stored or submitted device configuration inside `method_kwargs`, admitted by inventory's config-versions route or the commit, both large-JSON routes, plus ids (under 1 KB) | large-JSON allowance plus envelope |
  | inventory apply scheduler | execution `POST /execute/internal` | a stored device configuration inside `method_kwargs`, admitted by inventory's config-versions route, plus ids | large-JSON allowance plus envelope |
  | the `/api/v1` facade; ai-orchestrator commit | reservations `POST /` | a reservation create body: the facade's admitted body with defaults and normalized datetimes (about 150 bytes more at most), or one the commit builds from its own bounded fields | envelope |
  | notifications preference PUT | user-profile `PATCH /preferences` | the update merged with the stored preferences (a blob of at most 64 KiB in user-profile) | envelope |
  | ai-orchestrator commit | cabling `PUT /topologies/{topology_id}` | a canvas built from the commit, larger than the commit itself | capped: measured before anything is written (`canvas_put_bytes`) against cabling's limit for that large-JSON route, the setting plus `LARGE_JSON_BODY_BYTES` (`canvas_put_limit`); over it the commit is a 413 naming both numbers, and nothing is created |
  | cabling topology import | inventory `POST /devices/resolve-by-name` | the device names an import file holds | capped: batches of at most 32 KiB (`name_batches`); a name longer than a device name may be, or holding a NUL character, is not sent |
  | reservations utilization report | auth `POST /groups/users/groups` | every user of the report | capped: batches of 500 ids, auth's own cap (before, more than 500 users answered 422 and the report showed every user Ungrouped) |
  | ai-orchestrator recipe drafting | execution `POST /internal/validate-package` | a drafted package as base64 | an upload route (OPS-VALID-10) |
  | every other call: id lists of at most 500, a 2,000-character purpose, a status, a name, a pathfinding pair | auth, inventory, cabling, acl, reservations, ai-orchestrator | bounded by schemas and caps | under `MIN_MAX_REQUEST_BODY_BYTES`, so no setting can make it fail |

  Two bodies are bounded by something other than a schema: the assistant's
  configuration proposal to inventory, by the model's output tokens (`AI_MAX_TOKENS`),
  and a dynamic device's `field_data` from execution to inventory, by what the
  hypervisor driver returns. The rest go to services outside HERD (webhooks, chat, the
  identity provider) or to the restarter. Should a receiving service still answer 413
  (a different `MAX_REQUEST_BODY_BYTES` there, or a crafted body that grows when it is
  parsed and written again: a number such as `1e15` is written `1000000000000000.0`,
  up to 3.8 times its text), the sender answers a 4xx, never a 503 that says the other
  service is down: reservations relays cabling's 413 and its item as they are, the
  facade relays reservations' status and detail, ai-orchestrator's commit answers
  cabling's or reservations' status, inventory's apply records `execution answered HTTP
  413`, and notifications answers 413 `The merged notification preferences are too
  large to store`. \
  Enforced in: `tests/unit/test_request_body_limit_wiring.py` (`FORWARDS`, `body_calls`, `outbound_body_calls`, `envelope_shortfalls`); `services/ai-orchestrator/app/services/committer.py` (`canvas_put_bytes`, `canvas_put_limit`, `CANVAS_TOO_LARGE_DETAIL`, `commit_proposal`); `services/cabling/app/services/device_resolver.py` (`name_batches`, `RESOLVE_BATCH_BYTES`, `DEVICE_NAME_MAX_LENGTH`); `services/reservations/app/services/reporting_service.py` (`fetch_user_groups_map`, `USER_GROUPS_BATCH`); `services/notifications/app/routers/notifications.py` (`PREFERENCES_TOO_LARGE_DETAIL`) \
  Pinned by: `tests/unit/test_request_body_limit_wiring.py` (`test_every_outbound_body_is_classified_and_every_entry_exists`, `test_each_classified_call_is_covered`, `test_every_forward_route_has_a_sender`, `test_the_envelope_check_fails_both_ways`, `test_every_envelope_source_is_a_route_of_its_service`, `test_the_body_call_scan_both_ways`, `test_the_real_module_has_no_unclassified_body_call`, `test_each_bypass_the_review_found_is_an_unclassified_call`, `test_moving_a_call_without_changing_it_keeps_its_key`, `test_the_receiver_is_the_method_and_the_url_as_written`, `test_a_repeated_call_gets_an_ordinal`, `test_a_transport_call_without_a_body_is_not_a_body_call`); `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_a_canvas_over_the_limit_is_refused_before_anything_is_written`, `test_a_canvas_at_or_under_the_limit_goes_on_to_cabling`, `test_a_canvas_one_byte_over_the_limit_is_refused`, `test_the_canvas_limit_is_the_setting_plus_the_large_json_allowance`, `test_canvas_put_bytes_is_the_httpx_encoding`); `services/cabling/tests/test_route_handlers_direct.py` (`test_device_resolver_batches_stay_under_the_batch_bytes`, `test_device_resolver_drops_a_name_no_device_can_have`, `test_device_resolver_sends_every_batch_and_merges_the_answers`, `test_device_resolver_batch_edge_is_exact`); `services/reservations/tests/test_reporting_edges.py` (`test_fetch_user_groups_map_sends_batches_of_auth_s_own_cap`, `test_fetch_user_groups_map_one_failed_batch_fails_soft_for_all`); `services/reservations/tests/test_fork_endpoints.py` (`test_a_cabling_413_is_relayed_as_413_with_its_detail`); `services/notifications/tests/test_router.py` (`test_put_preferences_relays_a_user_profile_413_as_413`)
**Out of scope.** An error body a route builds itself (`HTTPException(detail=...)`) is
not rendered by this handler; each such detail is its area's rule (OPS-VALID-7 for a
value outside a fixed set). What auth's and config's password fields refuse is
IAM-REG-2, IAM-LOGIN-5 (`identity-and-access.md`), and OPS-CONFIG-8.

## 9. Errors

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 401 | `Invalid password` | config login with a wrong password, or with an unreadable auth file | OPS-CONFIG-6, OPS-CONFIG-7 |
| 429 | `Too many failed login attempts; try again later`, with `Retry-After` | config login while its source, or every source, is waiting after failed attempts | OPS-CONFIG-22 |
| 401 | `Session expired`, `Invalid session token` | a config route with an expired or bad session token | OPS-CONFIG-17 |
| 403 | `Change the config password before modifying or applying configuration` | config save or apply before the password is rotated | OPS-CONFIG-9 |
| 422 | `Password must be at least 8 characters`, `Password must be at most 32 characters`, `Password must not contain a NUL character`, `Password must be valid UTF-8 text` | a new config password outside 8 to 32 characters, holding a NUL, or holding a lone surrogate (judged in that order) | OPS-CONFIG-8 |
| 422 | `{"errors": ["<KEY> is required", ...]}` | a config save missing a required field, including a masked secret with no source | OPS-CONFIG-11, OPS-CONFIG-12 |
| 422 | `{"errors": ["Settings must be valid UTF-8 text"]}` | a config save whose values hold a lone surrogate in a key or value (checked first) | OPS-CONFIG-31 |
| 422 | `{"errors": ["Unknown settings: <KEY>, ..."]}` | a config save naming a key outside `CONFIG_SCHEMA` | OPS-CONFIG-12 |
| 422 | `{"errors": ["AUTH_SECRET_KEY is unset, blank, or still a placeholder; ...", "AUTH_ALGORITHM must be one of HS256, HS384, HS512"]}` (either or both) | a config save with a missing, blank, or placeholder `AUTH_SECRET_KEY`, or a set `AUTH_ALGORITHM` outside the three | OPS-CONFIG-23 |
| 400 | `No configuration to apply` | config apply with no `config.json` | OPS-CONFIG-15 |
| 200 | `{"restarted": [...], "errors": [...]}` | config apply; failures are entries in `errors` | OPS-CONFIG-13, OPS-CONFIG-16, OPS-CONFIG-24 |
| 503 | `The config data directory is not writable; the config service log names the one-time fix` | a config write (login's first auth file, change-password, save) the data directory refuses | OPS-CONFIG-29 |
| 503 | `The config password store is unusable; the config service log names the cause` | a config change-password, from a session issued earlier, while the auth file is unreadable or not a JSON object, or while there is no auth file and `CONFIG_ADMIN_PASSWORD` holds a value OPS-CONFIG-8's rule refuses | OPS-CONFIG-7, OPS-CONFIG-5 |
| 422 | validation list, each item exactly `{type, loc, msg}` | a config login, change-password, or save body that is not the documented shape (a field missing or of the wrong type, a body that is not a JSON object) | OPS-CONFIG-30 |
| 422 | validation list, each item exactly `{type, loc, msg}` | any request that does not have a route's documented shape, in every service; a body holding a lone surrogate anywhere, in every service but config (`string_unicode`, one item at `["body"]`, before authentication) | OPS-VALID-1, OPS-VALID-2, OPS-VALID-5, OPS-VALID-6 |
| 409 | `A restart is already in progress` | a restarter `POST /restart` while another runs | OPS-CONFIG-27 |
| 404 | `Not found` | a restarter request to any path but `/restart` and `/health` | OPS-CONFIG-25 |
| 405 | `Method not allowed`, with `Allow` | a restarter request to `/restart` with a method other than POST, or to `/health` with one other than GET | OPS-CONFIG-25 |
| 400 | `Transfer-Encoding is not accepted`, `Content-Length must be one whole number` | a restarter request with a chunked body or a Content-Length that is not one whole number | OPS-CONFIG-25 |
| 413 | `Request body too large` | a restarter request body over 4096 bytes | OPS-CONFIG-25 |
| 413 | `{"detail": [{"type": "body_too_large", "loc": ["body"], "msg": "Request body is larger than <limit> bytes"}]}`; the connection stays open | any request whose body is read and is over its route's limit (`MAX_REQUEST_BODY_BYTES`, plus the files an upload route accepts, the large-JSON allowance of a large-JSON route, or the envelope of a forward route), in every service, before authentication | OPS-VALID-9, OPS-VALID-10 |
| 413 | the receiving service's own 413 detail, relayed (reservations' fork routes, the `/api/v1` facade), or `The merged notification preferences are too large to store` (notifications), or `The topology this commit builds is <n> bytes, over the <limit>-byte request body limit; ...` (ai-orchestrator commit) | a body forwarded to another service that refuses it for its size, or a commit whose canvas would be | OPS-VALID-12 |
| 403 | `Admin or superadmin role required` | a non-admin lists health snapshots, reads a report, or writes a secret | OPS-HEALTH-3, OPS-REPORT-1, OPS-SECRET-1 |
| 422 | `Unknown last_status; allowed: UNKNOWN, HEALTHY, DEGRADED, UNREACHABLE` | an unknown health filter that matches nothing | OPS-HEALTH-4 |
| 500 | `Internal API token not configured` | a device check while execution has no internal token | OPS-HEALTH-6 |
| 403 | `Invalid internal token` | a device check without the right token | OPS-HEALTH-6 |
| 404 | `Device <id> not found`, `Template <id> not found` | a device check on a device or template inventory does not know | OPS-HEALTH-7 |
| 503 | `Could not verify device visibility; nothing was returned. Retry the request.` | a non-admin health read whose visibility lookup cannot be answered | OPS-HEALTH-1 |
| 503 | `Failed to fetch device: <reason>`, `Failed to fetch template: <reason>` (HERD-authored reason: an upstream status, `unreachable (<ClassName>)`, a malformed body, or a class name) | a device check when inventory cannot answer, or answers a body that is not a JSON object | OPS-HEALTH-7 |
| 409 | `{"error": "device_has_no_driver", "message"}` | a device check on a device with no driver | OPS-HEALTH-7 |
| 422 | validation list | a secret body outside the schema, an empty `data`, a lone surrogate in a `data` key or value | OPS-SECRET-1, OPS-SECRET-11 |
| 409 | `A secret with this name already exists` | a taken secret name | OPS-SECRET-2 |
| 404 | `Secret not found` | an unknown secret id or name, or a non-admin with no grant | OPS-SECRET-6, OPS-SECRET-8, OPS-SECRET-9, OPS-SECRET-12 |
| 403 | `manage permission required` | reveal by a holder of `view` only | OPS-SECRET-8 |
| 403 | `Invalid internal token` | a secrets internal route without the right token | OPS-SECRET-10 |
| 409 | `{"error": "secret_in_use", "hypervisor_ids", "hypervisor_names"}` | deleting a secret a hypervisor references | OPS-SECRET-13 |
| 503 | `inventory service unreachable while checking secret references`, `inventory service returned an error while checking secret references` | the delete guard cannot ask inventory, or cannot read its 200 | OPS-SECRET-14 |
| 409 | `Another key rotation committed first; nothing was changed. Retry.` | two rotations at once that the lock did not serialize | OPS-SECRET-18 |
| 409 | `Stored secret data is not valid UTF-8 text; an admin must replace it with PUT /secrets/{secret_id}` | a reveal or internal reveal of a secret stored with a lone surrogate before OPS-SECRET-1 refused one | OPS-SECRET-19 |
| 503 | `Secret key material is unavailable to this service` | a key version that is missing or does not unwrap under this process's key | OPS-SECRET-17 |
| 401 | `Invalid subject in token` | a preferences route with a missing or non-UUID subject | OPS-PREF-1 |
| 422 | validation list, or `merged <reason>` | a preferences body or merged result over a cap | OPS-PREF-4, OPS-PREF-5 |
| 401 | `Invalid internal token` | the internal preferences read without the right token | OPS-PREF-7 |
| 422 | `window_end must be after window_start`, `Utilization window cannot exceed <N> days (...)`, `<start or end> must fall between 0001-01-01T00:00:00Z and 9999-12-31T23:59:59.999999Z once converted to UTC`, a validation list | a bad report window, a bound with no UTC equivalent, or an unknown CSV section | OPS-REPORT-1, OPS-REPORT-13 |
| 503 | `{"error": "transit_gear_unavailable"}` | cabling cannot list transit devices, or answers 200 with a body that is not an object holding a `devices` object | OPS-REPORT-8, OPS-REPORT-9 |
| 503 | `Inventory service is unreachable` | the fleet CSV when inventory cannot answer | OPS-REPORT-14 |

Startup refusals (no HTTP answer; the container exits or waits):

| Outcome | When | Rule |
|---|---|---|
| secrets exits with `SECRETS_KEK is not set`, `... is not valid base64`, or `... must decode to exactly 32 bytes` | missing or malformed key material | OPS-SECRET-15 |
| secrets exits naming the key version | a stored key that unwraps under neither key | OPS-SECRET-16 |
| a service logs that NATS is unavailable and starts without it | the NATS broker is unreachable at boot | OPS-NATS-1 |
| a service refuses to load its settings, naming the value | a bad `NATS_NAK_BACKOFF_SECONDS` entry or `NATS_ACK_WAIT_SECONDS` below 2 | OPS-NATS-9, OPS-NATS-10 |

## 10. Interactions with other services

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out (every producer) | NATS | outbox relay publish, 10 s per message | deliver staged events | Fail safe: the row stays unpublished and is retried (OPS-OUTBOX-6, OPS-OUTBOX-7) |
| Out (reservations, execution) | Postgres | `LISTEN herd_outbox_<schema>` on a dedicated connection | wake the relay | Logged and retried each tick; the tick still drains (OPS-OUTBOX-11) |
| Out (every NATS user) | NATS | connect at startup | streams, consumers, relay | Startup finishes without NATS after a bounded first connect; no later connect until a restart (OPS-NATS-1) |
| Out (execution) | inventory | `GET /devices/health-config` (internal token, 10 s) | poll registry | Fail safe: the previous registry is kept (OPS-POLL-2) |
| Out (execution) | inventory | device and template reads per poll | what to poll and with which driver | The poll records `UNREACHABLE` (OPS-POLL-7) |
| Out (execution) | the device's driver | `login`, `status`, `logout` in the sandbox | health check | Recorded as the poll's status (OPS-POLL-7) |
| Out (execution, device check) | inventory | device and template reads (internal token, 10 s) | the device check | Fail closed: 404 or 503 (OPS-HEALTH-7) |
| Out (execution, health read) | inventory | `GET /device-groups/visible-devices` with the caller's token (10 s) | a non-admin's single-device health read | Fail closed: 503 (OPS-HEALTH-1) |
| Out (execution, device check) | the device's driver | `login`, then `status` and `logout` in the sandbox | the device check | Answered 200 with the failed run's status and error (OPS-HEALTH-8, OPS-HEALTH-9) |
| Out (secrets) | acl | `GET /resources` with the caller's token (5 s); the shared grant check | which secrets a user may see or reveal | Fail closed: nothing listed, 404 or 403 (OPS-SECRET-5) |
| Out (secrets) | inventory | `GET /hypervisors/by-secret/{id}/internal` (internal token, 5 s) | delete guard | Fail closed: 503, a malformed 200 included (OPS-SECRET-14) |
| Out (reservations, report) | cabling | `POST /internal/forks/devices/batch` (internal token, 10 s, chunks of 500) | transit devices | Fail closed, a 200 of the wrong shape included: 503 (OPS-REPORT-8); a wrongly typed entry is skipped and logged (OPS-REPORT-7) |
| Out (reservations, report) | inventory | `GET /devices` with the caller's token (10 s, paged by 500) | fleet section | Fail open: `fleet` null, or 503 for the fleet CSV (OPS-REPORT-11, OPS-REPORT-14) |
| Out (reservations, report) | auth | `POST /groups/users/groups` with the caller's token (5 s) | group section | Fail open: every user `Ungrouped` (OPS-REPORT-5) |
| Out (reservations, report) | execution | `GET /runs` with the caller's token (5 s) | run count | Fail open: null (OPS-REPORT-12) |
| Out (config) | restarter | `POST /restart` on `herd-ctl` (720 s) | Save and Restart | Reported as one `errors` entry with a fixed sentence; the restarter's body is passed through only in the expected shape (OPS-CONFIG-24) |
| Out (restarter) | Docker | the container list (10 s) and one restart per container (60 s each) over the host socket | Save and Restart | Reported in `errors` by exception class; nothing restarted when the project is unset (OPS-CONFIG-13, OPS-CONFIG-16, OPS-CONFIG-28) |
| In | every service | reads `config.json` at import | settings | A missing or corrupt file falls back to the environment (OPS-SET-3, OPS-SET-4) |

## 11. Configuration

[ENV_VARS.md](../ENV_VARS.md) has the full list and the precedence ladder.

| Setting | Default | Effect |
|---|---|---|
| `LOG_LEVEL` (every service but config) | `INFO` | Root log level (OPS-LOG-8) |
| `MAX_REQUEST_BODY_BYTES` (every service, config included) | `262144` | Request body limit, decimal digits, at least 65536; a large-JSON route adds 4 MiB, an upload route its files, a forward route the envelope (OPS-VALID-9, OPS-VALID-10) |
| `HERD_BUILD`, `HERD_BUILD_DATE` | `dev`, empty | Build identity reported by `/version` (OPS-VER-3) |
| `HERD_CONFIG_FILE` | `/etc/herd/config.json` | Where services read the config file (OPS-SET-7) |
| `HERD_CONFIG_DATA_DIR` (config) | `/data/herd-config` | Where the config service keeps `config.json`, the marker, and the auth file |
| `CONFIG_ADMIN_PASSWORD` (config) | empty | The config-page password; empty means a generated one (OPS-CONFIG-5) |
| `CONFIG_SESSION_SECRET` (config) | empty | Session signing key; empty means a random per-process key (OPS-CONFIG-17) |
| `CONFIG_LOGIN_MAX_ATTEMPTS` (config) | `20` | Failed config logins across all sources, within the lockout window, that make every source wait (OPS-CONFIG-22) |
| `CONFIG_LOGIN_LOCKOUT_SECONDS` (config) | `300` | The cross-source window and wait (OPS-CONFIG-22) |
| `HERD_COMPOSE_PROJECT` (restarter) | set by compose from `COMPOSE_PROJECT_NAME`; not an `.env` setting | The compose project whose services a restart may touch; unset or blank refuses (OPS-CONFIG-13) |
| `OUTBOX_RELAY_TICK_SECONDS` (reservations, execution) | `5.0` | Relay base cadence (OPS-OUTBOX-7) |
| `OUTBOX_BATCH_SIZE` | `100` | Rows per relay pass (OPS-OUTBOX-4) |
| `OUTBOX_RETENTION_SECONDS` | `604800` | Age after which published rows are pruned (OPS-OUTBOX-8) |
| `OUTBOX_WAKE_ON_WRITE` | `true` | Wake the relay on commit through Postgres notifications (OPS-OUTBOX-9) |
| `NATS_URL` | `nats://nats:4222` | Broker address |
| `NATS_STREAM_MAX_AGE_SECONDS` (reservations, execution) | `604800` | Retention of the owned streams; 0 means none (OPS-NATS-3) |
| `NATS_NAK_BACKOFF_SECONDS` (execution, notifications, integration) | `1,5,15,60,120` | NAK delay schedule; the dev override pins `0,1,1,1,1` (OPS-NATS-9) |
| `NATS_ACK_WAIT_SECONDS` (execution, notifications, integration) | `30` | Consumer ack wait; heartbeat at half; minimum 2 (OPS-NATS-10) |
| `HEALTH_POLL_SCHEDULER_ENABLED` (execution) | `true` | Run the scheduler (OPS-POLL-1) |
| `HEALTH_POLL_SCHEDULER_TICK_SECONDS` | `30` | Tick length; the poll floor (`inventory.md`, INV-POLL-2) |
| `HEALTH_POLL_REGISTRY_REFRESH_SECONDS` | `300` | Registry refresh period (OPS-POLL-2) |
| `HEALTH_POLL_MAX_CONSECUTIVE_FAILURES` | `3` | Backoff and `bad_news` threshold (OPS-POLL-9, OPS-POLL-11) |
| `HEALTH_POLL_BACKOFF_CAP_SECONDS` | `3600` | Longest backed-off interval (OPS-POLL-9) |
| `HEALTH_POLL_NOTIFY_ENABLED` | `true` | Stage health events (OPS-POLL-12) |
| `HEALTH_POLL_BATCH_SIZE` | `10` | Due rows per tick (OPS-POLL-4) |
| `HEALTH_POLL_MAX_CONCURRENCY` | `1` | Polls at once per replica (OPS-POLL-6) |
| `HEALTH_POLL_IN_USE_INTERVAL_SECONDS`, `HEALTH_POLL_IDLE_INTERVAL_SECONDS` | `0`, `0` | Tier overrides; 0 means none (OPS-TIER-2) |
| `TEMPLATE_CACHE_TTL_SECONDS` (execution) | `300` | Poller template cache (OPS-POLL-10) |
| `EXECUTION_POLLER_ONLY` (execution) | `false` | Serve no API routes on this replica (OPS-LIVE-2) |
| `SECRETS_KEK` (secrets) | none; required | Key-encryption key, base64 of 32 bytes (OPS-SECRET-15) |
| `SECRETS_KEK_PREVIOUS` (secrets) | empty | Previous key during a key rotation (OPS-SECRET-16) |
| `UTILIZATION_MAX_SPAN_DAYS` (reservations) | `366` | Longest report window; 0 disables (OPS-REPORT-1) |

Fixed in code, not configurable: the 10 s outbox publish timeout and the hourly prune;
the five-minute poll claim window; the 5 s and 60 s schema-gate poll and warning
periods; the 30-minute config session; the restarter's allowlist, its 30 s stop
timeout, its 10 s list and 60 s restart deadlines, and the config client's 720 s wait; the per-source config login schedule (two free
failures, then 1 s doubling to 60 s, a source forgotten after 15 quiet minutes, at most
10,000 sources kept); the 500-reservation transit chunk and the
100,000-device fleet stop; the preference caps of 200 keys, 64 KB, and page sizes of 1
to 500; the 10 MiB import file bound of the inventory and cabling import routes
(OPS-VALID-10).

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/common/tests/` (`test_logging.py`, `test_config_loader.py`, `test_base_settings.py`, `test_schema_init.py`, `test_consumer_schema_gate.py`, `test_outbox.py`, `test_jetstream.py`, `test_version.py`, `test_validation_errors.py`, `test_request_body_limit.py`, `test_error_details.py`); `services/execution/tests/test_health_scheduler*.py`; `services/config/tests/`; `services/secrets/tests/test_crypto.py`, `test_keyring.py`; `services/reservations/tests/test_reporting_*.py`, `test_fleet_report.py`, `test_transit_gear_rollup.py`; `infra/restarter/tests/` (the restarter against a fake Docker daemon on a Unix socket, and its HTTP surface in process); the `tests/unit/` guards (`test_validation_errors_wiring.py`, `test_request_body_limit_wiring.py`, `test_restarter_compose_guard.py`, `test_dockerfile_users.py`, `test_compose_settings_wiring.py`, `test_compose_ports.py`, `test_build_args_wiring.py`, `test_consumer_heartbeat_wiring.py`, `test_docs_nats_cli_form.py`, `test_check_image_matches_lock.py`); the frontend tests named in sections 8.3, 8.6, 8.11, 8.14, and 8.15 | SQLite in memory; the scheduler's concurrency is driven within one process |
| Functional (through the service API) | each service's `test_version.py`, `test_validation_errors.py`, and `test_request_body_limit.py`; `services/execution/tests/test_health_endpoints.py`; the device check tests in `services/execution/tests/test_api_endpoints.py`, `test_router_endpoints.py`, and `test_router_direct.py`; `services/config/tests/test_config.py`, `test_config_passwords.py`; `services/secrets/tests/test_api.py`, `test_unencodable_data.py`; `services/user-profile/tests/test_preferences*.py`; the report route tests in `test_fleet_report.py` | httpx against the app |
| Integration (running stack) | `tests/integration/test_restarter_isolation.py` (no socket in config, config not root, the restarter answering on `herd-ctl` and unresolvable from `herd-net`; it never restarts; the absence of a gateway route is pinned statically by OPS-GUARD-6, since Traefik could not reach `herd-ctl` even with labels), `tests/integration/test_device_check_internal_fetch.py`, `test_outbox_durability.py`, `test_nats_consumer_configs_live.py`, `test_health_alerting_flow.py`, `test_secrets_flow.py`, `test_reporting.py`, `test_dlq_and_idempotency.py`, `test_request_body_limit.py` (the 413 through the gateway, without signing in, a canvas save at the large-JSON limit and one byte over it, and a 1 MiB fork canvas saved through reservations to cabling, signed in); the live-Postgres suite `services/common/tests/test_outbox_wake_live_pg.py` | No running-stack integration test sends a restart (the nightly browser step does), covers a scheduler poll end to end (the alerting test publishes its own events), or covers a JetStream store surviving a recreate |
| Stress and load | `tests/load/locustfile.py` ([LOAD_TESTING.md](../LOAD_TESTING.md)) | The load test drives reservations, inventory, ACL, export, validation, notifications, and bulk cabling, and `OversizedBodySender` sends bodies over the limit to one route of every service beside them (OPS-VALID-9) while `LargeCanvasSaver` saves a canvas over the default to a large-JSON route (OPS-VALID-10); none of its users polls health, reads reports, or touches secrets or the config service. Save and Restart cannot be load tested, since a restart bounces the stack under test; its concurrency property (one pass at a time under twenty concurrent calls) is pinned at the unit level (OPS-CONFIG-27). Scheduler fleet scale is covered only by the unit bounds in section 8.10 |
| Browser end-to-end | `tests/e2e/test_about_page_playwright.py`, `tests/e2e/test_config_playwright.py` (Save and Restart runs only with `HERD_E2E_RESTART=1`, which nightly.yml sets in its last test step), `tests/e2e/test_config.py`, `tests/e2e/test_reporting_page.py`, `tests/e2e/test_large_canvas_save_playwright.py` (the editor's Save of a canvas over the default, OPS-VALID-10); the author credit's placement under its card (OPS-CREDIT-1) in `tests/e2e/test_login.py`, `tests/e2e/test_register_and_roles.py`, and `tests/e2e/test_config.py` | No browser test covers the health badge or the help link |

Not run for this document: nothing was checked against a running stack. The
integration, live-Postgres, load, and browser suites were read, not run; `tests/unit/`
was run. The startup behavior with an unreachable broker (OPS-NATS-1) was reproduced on
the host with the installed NATS client against a closed local port. The restarter rules
(OPS-CONFIG-13 to OPS-CONFIG-16, OPS-CONFIG-24 to OPS-CONFIG-29) were written at
`86765c9e` from their unit and functional suites, which were run; their integration test
and the nightly browser step were not run against a stack. The body limit rules
(OPS-VALID-9 to OPS-VALID-12) were written from their unit and functional suites, the
real-socket suite against uvicorn on an ephemeral port, and
the `tests/unit/` guard, which were run; `tests/integration/test_request_body_limit.py`
and the `OversizedBodySender` load user were written against their neighbours and not
run against a stack; so were the large canvas save and the 1 MiB fork canvas save added to that integration test, the
`LargeCanvasSaver` load user, and the browser test of a large canvas save, for issue
#1484, whose unit and functional suites were run. OPS-CREDIT-1 was added on
2026-11-20 with its unit tests run; its three browser tests were written, not run.

## 13. Known limits and gaps

### Open defects

None at present. (#1577 is resolved by OPS-REPORT-11, OPS-REPORT-12, and OPS-REPORT-14.)

### Limits by decision

- `/health` is a liveness answer only (OPS-LIVE-1); there is no readiness route and no
  built-in metrics stack. Recorded in [OPERATIONS.md](../OPERATIONS.md) (Healthchecks
  and monitoring: "There is no built-in Prometheus/Grafana stack").
- A managed schema gets new tables only from `make migrate`, never at boot (OPS-SCHEMA-3).
  Recorded in issue #419 and [OPERATIONS.md](../OPERATIONS.md) (Upgrade path).
- The dev and gate stacks keep no JetStream state across a recreate, and the relay does
  not republish an event the broker acknowledged (OPS-NATS-13, OPS-OUTBOX-14). Recorded
  in issue #620 and [OPERATIONS.md](../OPERATIONS.md) (JetStream durability).
- No durable carries `backoff`; delays come from the NAK schedule (OPS-NATS-7). Recorded
  in issue #895.
- The integration service's OpenAPI version is the `/api/v1` contract version, and its
  `/version` route is left out of that document (OPS-VER-4). Recorded in issue #846.
- The config service carries its own copy of the version helper and imports nothing
  from `herd_common` (OPS-VER-5). Recorded in issue #846 and the module docstring of
  `services/config/app/version.py`.
- Secret deletion has no force flag; the referencing hypervisor must be re-pointed first
  (OPS-SECRET-13). Recorded in issue #456.
- A lost secret key-encryption key makes stored secrets unrecoverable (OPS-SECRET-15).
  Recorded in [OPERATIONS.md](../OPERATIONS.md) (Secrets-service key rotation).
- The fleet section's utilization is not clamped at 100 percent (OPS-REPORT-10).
  Recorded in the docstring of `_build_fleet_section`.
- A user in several groups counts in each group's line (OPS-REPORT-5). Recorded in the
  comment in `rollup_by_group`.
- Config login attempt counts live in one config process: a restart clears them,
  replicas count separately, and a cross-source wait applies to the operator too
  (OPS-CONFIG-22). Recorded in the module docstring of
  `services/config/app/login_limits.py`.
- The restarter runs as root inside its container, because the host's Docker socket
  is root-owned, and Traefik still holds the full socket for discovery; the restarter
  is hardened instead (OPS-GUARD-6, OPS-GUARD-7). Recorded in
  [ADR 0017](../design/0017-config-restart-without-the-docker-socket.md); issue #1424
  tracks Traefik.
- The config service's restart set is an allowlist (OPS-CONFIG-14): a new service is
  not restarted until it mounts the config file, when the guard makes the list follow.
  Recorded in [ADR 0017](../design/0017-config-restart-without-the-docker-socket.md).
- The config session ends on every config-service restart unless
  `CONFIG_SESSION_SECRET` is set (OPS-CONFIG-17). Recorded in issue #246 and the
  docstring of `_load_session_secret`.
- The config service installs the shared 422 handler without the body check for a
  lone surrogate (OPS-VALID-4, OPS-VALID-6): its password routes keep their own answers
  (OPS-CONFIG-8) and its settings save refuses one itself (OPS-CONFIG-31). Recorded in
  the module docstrings of `herd_common.validation_errors` and
  `services/config/app/validation_errors.py`.
- A secret stored with a lone surrogate before OPS-SECRET-1 refused one answers a 409 on
  every reveal until an admin replaces its data; there is no sweep that lists such
  secrets (OPS-SECRET-19). Recorded in issue #1456.
- The body limit lives in each service, not the gateway (issue #1466, the owner's
  option (b)): Traefik still forwards a body of any size, and each service refuses it
  at its first read (OPS-VALID-9). A route that never reads its body is not refused,
  since the route reads nothing of it. The limit bounds memory and parsing, not
  bandwidth: the server reads and discards what is left of a refused body, so a
  client may still send any number of bytes over a connection. Recorded in the module
  docstring of `herd_common.validation_errors`.
- A route that streams its answer without reading its body, given a body over the
  limit, does not answer as before: behind the request logger a declared body ends in
  a 500 (`RuntimeError: No response returned.`) and a chunked one cuts the stream
  short; nothing of the body reaches the route (OPS-VALID-9). No HERD route streams
  without a body parameter, and the wiring guard fails on one that is not listed by
  decision (issue #1483 kept the limit and added the guard). Recorded in the tests
  named for it in `services/common/tests/test_request_body_limit.py` and in issue
  #1483.
- A client that sends `Expect: 100-continue` with an over-limit `Content-Length` gets
  the 413 without a `100 Continue`; reusing that connection without sending the body
  would have its next request read as the refused body (OPS-VALID-9). Recorded in
  OPS-VALID-9.
- A crafted body that grows when it is parsed and written again (numbers in exponent
  form) can still be refused by the service it is forwarded to, which answers 413 and
  the sender relays a 4xx (OPS-VALID-12); the envelope covers what the senders add, not
  that growth. Recorded in issue #1466.
- A bulk import's file work runs off the event loop (INV-BULK-27, TOPO-BULK-23), except
  each single C call within it (the whole-file JSON decode and the surrogate scan's
  serialization, from about 20 ms to about 130 ms and from about 35 ms to about 75 ms for a 10 MiB file on one core, more the smaller and more numerous its items; on Python 3.13 and later a JSON export's render is one such call as well), but its row
  loop does not. Each row's schema checks and writes on inventory, and each topology's
  canvas rewrite and validation on cabling, run on the event loop between that row's
  queries, so a single row as large as the file holds the loop for that row's work, as
  an interactive save of that size does. Rows rejected before any query (an unknown
  template or a missing name on inventory; a missing name or unresolved device names on
  cabling) run with no await between them, and so does building and serializing the
  report, so a file of many such rows holds the loop for all of them. The longest stall
  measured on one core, the same before and after issue #1417: about 1.1 s on
  inventory; on cabling about 1.7 s for rows with no name imported by a non-admin,
  about 0.8 s for rows with unresolved device names, and about 0.59 s for one canvas as
  large as the file. Recorded in INV-BULK-27 and TOPO-BULK-23.
- A multipart upload that the body limit refuses part way through its read (sent
  chunked, or declaring less than it sends) leaves the temporary file Starlette spooled
  it to open until the object is freed, not when the 413 is sent: Starlette's parser
  closes its files only when the parse fails with its own error. The file holds at
  most the route's limit. Recorded in the tests named
  `test_an_import_body_over_its_limit_is_413_however_it_is_sent` in
  `services/inventory/tests/test_request_body_limit.py` and
  `services/cabling/tests/test_request_body_limit.py`.

### Rules with no test

- OPS-BUILD-1: the Makefile's build identifier and date.
- OPS-SET-9: settings read once at import.
- OPS-OUTBOX-14: the relay does not republish an acknowledged event.
- OPS-NATS-13: JetStream store durability under `make prod`.
- OPS-HEALTH-10: the device check writes no health status row and stages no event.
- OPS-NATS-15: reservations' declaration of `HERD_RESERVATIONS`.
