# Operations and observability specification

| | |
|---|---|
| Area prefix | `OPS` (used in rule identifiers, for example `OPS-OUTBOX-1`) |
| Verified at | commit `cd3eaeed` (`v0.6.0-215-gcd3eaeed`), 2026-10-08 |
| Owning services | common (`services/common/herd_common/`: logging, settings sources, schema lifecycle, outbox, JetStream helpers, version), config (`services/config/`), secrets (`services/secrets/`), user-profile (`services/user-profile/`), execution (the device health scheduler and its read routes), reservations (the utilization report routes); the About, Config, and Reporting pages in `frontend/` |
| Other services involved | every backend service (each serves `/health` and `/version`, logs through the shared formatter, and resolves settings through the shared sources), inventory (the poll registry route, the secret reference lookup), auth (user groups for the report), cabling (transit devices for the report), acl (secret grants), notifications and integration (consumers of health events), NATS JetStream, Postgres, Docker (the config service's restart) |
| Design records | [ADR 0003](../design/0003-encrypted-credential-store.md), [ADR 0013](../design/0013-lab-purpose-classification.md) |
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
| Config operator (config-page password, not a HERD account) | Read the merged settings (secret values masked), change the config password, save settings and restart this compose project's services once the password is rotated (OPS-CONFIG-9) | Save or restart before rotating a generated password; restart services of another compose project (OPS-CONFIG-13) |
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
| PUT | `/settings` (config) | config session, password rotated | 200 | OPS-CONFIG-9, OPS-CONFIG-11, OPS-CONFIG-12 |
| POST | `/apply` (config) | config session, password rotated | 200 | OPS-CONFIG-9, OPS-CONFIG-13 to OPS-CONFIG-16 |
| POST | `/secrets` (secrets) | admin | 201 | OPS-SECRET-1, OPS-SECRET-2 |
| GET | `/secrets` (secrets) | any signed-in user | 200 | OPS-SECRET-5 |
| GET | `/secrets/{secret_id}` (secrets) | admin, or a `view` or `manage` grant | 200 | OPS-SECRET-6 |
| GET | `/secrets/{secret_id}/value` (secrets) | admin, or a `manage` grant | 200 | OPS-SECRET-7, OPS-SECRET-8 |
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
| GET | `/internal/secrets/{secret_id}/value` (secrets) | `X-Internal-Token` | execution (hypervisor credentials, `dynamic-resources.md`), inventory (hypervisor registration, `inventory.md`) | `{id, name, data}` | OPS-SECRET-9, OPS-SECRET-10 |
| GET | `/internal/secrets/by-name/{name}/value` (secrets) | `X-Internal-Token` | none in the services today | `{id, name, data}` | OPS-SECRET-9, OPS-SECRET-10 |
| GET | `/preferences/internal?user_id` (user-profile) | `X-Internal-Token` | notifications (channel and event opt-outs) | the user's preferences, created empty if missing | OPS-PREF-7 |
| POST | `/device-check` (execution) | `X-Internal-Token` | none in the services today | `{run_id, device_id, status, output, error}` | OPS-HEALTH-6 to OPS-HEALTH-10 |

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
- **OPS-BUILD-3.** Every service Dockerfile declares the two args after its last
  dependency install and exports them as environment variables, so a new build string
  rebuilds only that layer. \
  Enforced in: `services/auth/Dockerfile` (`HERD_BUILD`) \
  Pinned by: `tests/unit/test_build_args_wiring.py` (`test_every_service_dockerfile_declares_the_build_args_and_env`)
- **OPS-BUILD-4.** The frontend Dockerfile declares its two args before `npm run build`,
  and Vite injects the package version, the build, and the build date as compile-time
  constants (`dev` and empty when unset). \
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
  Pinned by: `frontend/src/test/lib/appVersion.test.ts` (`APP_VERSION is read from package.json`, `APP_BUILD falls back to 'dev' when VITE_HERD_BUILD is unset (default vitest env)`, `APP_BUILD_DATE is null when VITE_HERD_BUILD_DATE is unset (default vitest env)`)
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
  Pinned by: `frontend/src/test/api/about.test.ts` (`throws InvalidVersionResponseError for an HTML string body (proxy error page served with 200)`, `throws InvalidVersionResponseError for a body missing version`); `frontend/src/test/pages/AboutPage.test.tsx` (`renders 'invalid response' with dashed cells and no 'differs' for a malformed 200 body (issue #874)`, `still renders 'unreachable' (not 'invalid response') for an ordinary transport/HTTP failure`)
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
  Pinned by: `services/common/tests/test_logging.py` (`test_json_formatter_basic_output`, `test_json_formatter_includes_exception`)
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
  Pinned by: `services/common/tests/test_logging.py` (`test_request_logging_middleware_emits_access_record`); `services/secrets/tests/test_api.py` (`test_plaintext_never_hits_the_logs`)
- **OPS-LOG-8.** `setup_logging` installs the one JSON handler on the root logger at
  `LOG_LEVEL` (INFO when the name is unknown) and holds `uvicorn.access`,
  `uvicorn.error`, and `sqlalchemy.engine` at WARNING. \
  Enforced in: `services/common/herd_common/logging.py` (`setup_logging`) \
  Pinned by: `services/common/tests/test_logging.py` (`test_setup_logging_configures_root_logger`)
- **OPS-LOG-9.** The config service does not use this formatter: it logs through the
  standard library with no handler of its own, so its lines are plain text and its INFO
  lines are not emitted. `FEATURES.md` and `docs/ENV_VARS.md` (`LOG_LEVEL`) say so. \
  Enforced in: `services/config/app/main.py` (`logger`) \
  Pinned by: none
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
- **OPS-SET-4.** An empty or whitespace-only string in the file means unset, and an
  unreadable or corrupt file is logged and treated as empty, never as a boot failure. \
  Enforced in: `services/common/herd_common/config_loader.py` (`HerdJsonConfigSource`, `_load_json`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_source_skips_empty_string_values`, `test_source_falls_back_when_config_json_is_corrupt`)
- **OPS-SET-5.** The file's `AUTH_*` keys map to the services' unprefixed names, and the
  file's `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` build a percent-encoded
  `DATABASE_URL` against `postgres:5432` only when all three are set. \
  Enforced in: `services/common/herd_common/config_loader.py` (`_KEY_MAP`, `_build_database_url`) \
  Pinned by: `services/common/tests/test_config_loader.py` (`test_source_maps_auth_keys`, `test_source_builds_database_url_when_postgres_fields_present`, `test_source_url_encodes_special_chars_in_password`, `test_source_skips_database_url_when_postgres_incomplete`)
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
presses Save and Restart. Login to HERD stays disabled until the file exists.

**Surfaces.** `services/config/app/` (`main.py`, `config_store.py`, `auth.py`,
`docker_ctl.py`, `config_schema.py`); `frontend/src/pages/ConfigPage.tsx` and
`frontend/src/stores/configStore.ts`. The login page's use of `GET /status` is
`identity-and-access.md` (IAM-UI-9).

**Rules.**

- **OPS-CONFIG-1.** At startup, when `config.json` does not exist and every required
  field has a non-blank environment value, the service writes the file from the
  environment values of every schema field and writes the `config.bootstrapped` marker
  beside it; otherwise it logs the missing names and writes nothing. \
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
  `password_changed`; a corrupt auth file reports `password_changed` false instead of
  failing. \
  Enforced in: `services/config/app/main.py` (`get_status`); `services/config/app/config_store.py` (`is_configured`, `is_password_changed`) \
  Pinned by: `services/config/tests/test_config.py` (`test_status_unconfigured`, `test_status_configured`, `test_status_with_corrupt_auth_file`)
- **OPS-CONFIG-5.** On first use the config password is `CONFIG_ADMIN_PASSWORD` when set,
  counted as rotated; otherwise a random password is generated, logged once at WARNING,
  and counted as not rotated. No fixed default exists. \
  Enforced in: `services/config/app/config_store.py` (`_initial_password`, `load_auth`) \
  Pinned by: `services/config/tests/test_config.py` (`test_old_default_password_rejected`, `test_random_seed_used_and_unrotated`, `test_env_password_is_used_and_marks_rotated`)
- **OPS-CONFIG-6.** `POST /login` checks the password against the stored bcrypt hash and
  answers a session token valid for 30 minutes plus `password_changed`; a wrong password
  is 401 `Invalid password`. \
  Enforced in: `services/config/app/main.py` (`login`); `services/config/app/auth.py` (`create_session_token`, `_EXPIRE_MINUTES`) \
  Pinned by: `services/config/tests/test_config.py` (`test_login_success`, `test_login_wrong_password`); `services/config/tests/test_auth.py` (`test_create_session_token_round_trips`)
- **OPS-CONFIG-7.** A present but unreadable auth file denies every login and is never
  regenerated; removing the file is the operator's recovery. \
  Enforced in: `services/config/app/config_store.py` (`load_auth`, `verify_password`, `ConfigAuthError`) \
  Pinned by: `services/config/tests/test_config.py` (`test_verify_password_fails_closed`, `test_default_password_not_regenerated_on_corrupt`, `test_login_with_corrupt_auth_file`)
- **OPS-CONFIG-8.** `POST /change-password` needs a session and a new password of 8 to 32
  characters, and marks the password rotated. \
  Enforced in: `services/config/app/main.py` (`change_password_endpoint`, `ChangePasswordRequest`) \
  Pinned by: `services/config/tests/test_config.py` (`test_change_password`, `test_change_password_too_short`, `test_change_password_too_long`, `test_change_password_unauthenticated`)
- **OPS-CONFIG-9.** `PUT /settings` and `POST /apply` answer 403 `Change the config
  password before modifying or applying configuration` until the password is rotated. \
  Enforced in: `services/config/app/main.py` (`require_password_rotated`) \
  Pinned by: `services/config/tests/test_config.py` (`test_put_settings_locked_until_rotated`, `test_apply_locked_until_rotated`, `test_write_allowed_with_operator_password`)
- **OPS-CONFIG-10.** `GET /schema` lists the editable fields to anyone; `GET /settings`
  needs a session and answers the environment values overlaid by the file's values,
  limited to the schema's keys, with every non-empty secret field shown as `********`. \
  Enforced in: `services/config/app/main.py` (`get_schema`, `get_settings`, `SCHEMA_KEYS`) \
  Pinned by: `services/config/tests/test_config.py` (`test_schema`, `test_get_settings_includes_env_values_when_no_file`, `test_get_settings_file_overrides_env`, `test_get_settings_redacts_env_secrets`, `test_settings_unauthenticated`, `test_get_settings_answers_schema_keys_only`)
- **OPS-CONFIG-11.** A secret field sent back as `********` keeps the file's value, else
  the environment's; with neither it is dropped, and the placeholder itself is never
  written. \
  Enforced in: `services/config/app/main.py` (`update_settings`) \
  Pinned by: `services/config/tests/test_config.py` (`test_save_settings_preserves_redacted_secrets`, `test_save_settings_resolves_masked_secret_from_env`, `test_save_settings_drops_masked_optional_secret_without_source`, `test_save_settings_masked_required_secret_without_source_is_422`)
- **OPS-CONFIG-12.** A save may write only keys `CONFIG_SCHEMA` declares: a body naming
  any other key is 422 `{"errors": ["Unknown settings: <KEY>, ..."]}` (the unknown keys
  sorted, checked first) and writes nothing. A save missing a required field, or carrying
  it blank, is 422 `{"errors": ["<KEY> is required", ...]}` and writes nothing. Otherwise
  the body's values are written, and a key already in the file outside the schema (placed
  there by hand) is carried over unchanged. A file that is not a JSON object reads as
  empty. \
  Enforced in: `services/config/app/main.py` (`update_settings`, `SCHEMA_KEYS`); `services/config/app/config_schema.py` (`SCHEMA_KEYS`); `services/config/app/config_store.py` (`save_config`, `load_config`) \
  Pinned by: `services/config/tests/test_config.py` (`test_save_settings_refuses_keys_outside_the_schema`, `test_save_settings_with_unknown_key_leaves_existing_file_unchanged`, `test_unknown_key_is_refused_before_the_required_check`, `test_save_settings_with_schema_keys_only_saves`, `test_save_settings_keeps_keys_already_in_the_file_outside_the_schema`, `test_load_config_non_object_returns_empty`, `test_save_settings_missing_required`, `test_save_config_blank_required`, `test_save_and_get_settings`)
- **OPS-CONFIG-13.** `POST /apply` restarts only containers of the config service's own
  compose project, read from its own container's label; when that label cannot be read
  it restarts nothing and reports an error. \
  Enforced in: `services/config/app/docker_ctl.py` (`restart_services`, `_own_compose_project`) \
  Pinned by: `services/config/tests/test_docker_ctl.py` (`test_restart_services_scopes_to_own_project`, `test_restart_services_fails_closed_when_self_lookup_fails`, `test_restart_services_fails_closed_when_own_project_label_missing`)
- **OPS-CONFIG-14.** The restart skips `config`, `traefik`, `postgres`, `nats`, and
  `frontend`, and restarts each other service with a 30 second timeout. \
  Enforced in: `services/config/app/docker_ctl.py` (`SKIP_SERVICES`, `restart_services`) \
  Pinned by: `services/config/tests/test_docker_ctl.py` (`test_restart_services_restarts_non_skipped`, `test_restart_services_never_restarts_a_skipped_service`, `test_restart_services_skips_every_known_service`, `test_skip_services_is_exactly_the_expected_names`)
- **OPS-CONFIG-15.** `POST /apply` with no `config.json` is 400 `No configuration to
  apply`. \
  Enforced in: `services/config/app/main.py` (`apply_config`) \
  Pinned by: `services/config/tests/test_config.py` (`test_apply_not_configured`)
- **OPS-CONFIG-16.** `POST /apply` answers 200 `{restarted, errors}`; each per-service
  failure, a missing Docker SDK, an unreachable Docker daemon, and a failed listing are
  entries in `errors`. An entry names what failed and the exception class only
  (`Failed to restart <service>: <ClassName>`, `Cannot connect to Docker: <ClassName>`,
  `Cannot list containers: <ClassName>`); the exception text goes to the log message. \
  Enforced in: `services/config/app/docker_ctl.py` (`restart_services`) \
  Pinned by: `services/config/tests/test_docker_ctl.py` (`test_restart_services_collects_errors`, `test_restart_services_returns_error_when_docker_sdk_missing`, `test_restart_services_returns_error_when_from_env_fails`, `test_restart_services_returns_error_when_list_fails`)
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
  services and each error under "Restart Result". \
  Enforced in: `frontend/src/pages/ConfigPage.tsx` (`handleSaveAndRestart`) \
  Pinned by: `tests/e2e/test_config_playwright.py` (`test_config_save_and_restart_gated`)
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
  volume recreated or the schema created by hand. \
  Enforced in: `infra/postgres/init.sql` (`CREATE SCHEMA`) \
  Pinned by: none

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
  engine's own driver arguments. \
  Enforced in: `services/common/herd_common/outbox.py` (`_listen_for_wakeups`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_listen_for_wakeups_retries_then_wakes_on_connect_and_notify`, `test_listen_for_wakeups_termination_closes_conn_then_backs_off_before_reconnect`, `test_listen_for_wakeups_registration_failure_closes_conn_and_leaks_none`, `test_listen_for_wakeups_dsn_from_dialect_connect_args`)
- **OPS-OUTBOX-12.** A consumer's deduplication key is the payload `event_id`, else the
  JetStream `<stream>:<sequence>`, else none. \
  Enforced in: `services/common/herd_common/outbox.py` (`event_dedupe_key`) \
  Pinned by: `services/common/tests/test_outbox.py` (`test_dedupe_key_prefers_payload_event_id`, `test_dedupe_key_falls_back_to_stream_sequence`, `test_dedupe_key_none_when_no_id_and_no_metadata`, `test_dedupe_key_stable_across_resequence`)
- **OPS-OUTBOX-13.** The relay task is cancelled at shutdown and a crash of it is logged.
  Against a running stack, an event staged by reservations reaches its consumers. \
  Enforced in: `services/execution/app/main.py` (`start_outbox_relay`, `stop_outbox_relay`) \
  Pinned by: `services/execution/tests/test_main.py` (`test_start_outbox_relay_starts_task_and_stop_cancels`, `test_stop_outbox_relay_noop_when_never_started`); `services/common/tests/test_outbox.py` (`test_relay_cancelled_error_during_sleep_propagates`); `tests/integration/test_outbox_durability.py` (`test_outbox_delivers_reservation_event_end_to_end`)
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
  `ensure_stream` once its consumer has connected; a failed declaration is logged and the
  service keeps running. \
  Enforced in: `services/execution/app/main.py` (`_ensure_health_stream`, `_start_consumer_and_streams`) \
  Pinned by: `services/execution/tests/test_main.py` (`test_lifespan_starts_consumer_immediately_when_schema_ready`)
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
  Pinned by: `services/common/tests/test_jetstream.py` (`test_ensure_consumer_sets_name_durable_name_filter_subject_and_calls_add_consumer`, `test_ensure_consumer_does_not_overwrite_an_already_set_filter_subject`, `test_ensure_consumer_tolerates_a_config_double_missing_filter_subject_attrs`)
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
  from settings and its heartbeat from `heartbeat_interval`. \
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
  first, never-polled last. \
  Enforced in: `services/execution/app/routers/health.py` (`list_device_health`) \
  Pinned by: `services/execution/tests/test_health_endpoints.py` (`test_list_health_admin_only`, `test_list_health_pagination_and_filter`)
- **OPS-HEALTH-4.** An unknown `last_status` filter answers 422 `Invalid last_status:
  <value>` only when it matches no row. \
  Enforced in: `services/execution/app/routers/health.py` (`list_device_health`) \
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
  `data` object of string values; it answers 201 with metadata only. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`create_secret`); `services/secrets/app/schemas/secret.py` (`SecretCreate`, `SecretResponse`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_admin_create_returns_metadata_only`, `test_empty_data_is_422`, `test_non_admin_cannot_create`)
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
  active key, and without it the ciphertext is untouched. The name cannot change. \
  Enforced in: `services/secrets/app/routers/secrets.py` (`update_secret`); `services/secrets/app/schemas/secret.py` (`SecretUpdate`) \
  Pinned by: `services/secrets/tests/test_api.py` (`test_update_data_reencrypts`); `services/secrets/tests/test_routers_direct.py` (`test_update_secret_direct_reencrypts_and_updates_metadata`, `test_update_secret_direct_no_data_leaves_ciphertext_untouched`, `test_update_secret_direct_not_found`)
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
  (default 366; 0 disables) is 422 naming the limit. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`); `services/reservations/app/routers/reservations.py` (`get_utilization_report`, `get_utilization_report_csv`) \
  Pinned by: `services/reservations/tests/test_rbac_denial.py` (`test_non_admin_denied`); `services/reservations/tests/test_reporting_service.py` (`test_build_report_rejects_inverted_window`, `test_build_report_span_over_max_raises`, `test_build_report_span_at_max_succeeds`, `test_build_report_span_guard_disabled_when_zero`); `services/reservations/tests/test_coverage_gaps.py` (`test_csv_report_inverted_window_returns_422`); `tests/integration/test_reporting.py` (`test_utilization_report_non_admin_forbidden`)
- **OPS-REPORT-2.** A reservation counts for the hours it overlaps the window, clipped to
  the window; one with no overlap is skipped. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_build_report_clamps_window`, `test_build_report_skips_zero_hours`); `services/reservations/tests/test_reporting_edges.py` (`test_build_report_clips_hours_to_window`)
- **OPS-REPORT-3.** Without `status`, the user, device, topology type, day, group, and
  purpose sections count only `COMPLETED` reservations and the fleet section counts
  `ACTIVE` and `COMPLETED`; a given `status` list applies to every section. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report`, `FLEET_DEFAULT_STATUS_FILTER`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_fleet_counts_active_but_legacy_default_does_not`, `test_explicit_filter_applies_to_fleet_too`, `test_report_route_fleet_defaults_count_active`); `services/reservations/tests/test_reporting_service.py` (`test_build_report_filters_status`)
- **OPS-REPORT-4.** The day section splits each reservation's hours at UTC midnight and
  counts a reservation once per day it touches. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`_split_hours_per_day`) \
  Pinned by: `services/reservations/tests/test_reporting_edges.py` (`test_split_hours_across_midnight`, `test_split_hours_three_days_full_middle`, `test_build_report_day_buckets_count_unique_reservations_per_day`); `services/reservations/tests/test_reporting_service.py` (`test_build_report_aggregates_by_day_across_midnight`)
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
  purpose rows. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`build_utilization_report`, `_fetch_transit_devices`) \
  Pinned by: `services/reservations/tests/test_transit_gear_rollup.py` (`test_transit_device_inherits_category_and_hours`, `test_reserved_and_transit_dedupes_to_reserved_only`, `test_duplicate_device_ids_in_fork_response_count_once`, `test_mixed_reserved_in_one_transit_in_another`, `test_reservation_absent_from_cabling_map_has_no_transit`, `test_ai_suggested_only_reservation_contributes_no_device_purpose_transit`)
- **OPS-REPORT-8.** Transit devices are read from cabling in chunks of 500 reservations;
  a transport error or non-200 from any chunk fails the report closed with 503
  `{"error": "transit_gear_unavailable"}`, and `include_transit=false` makes no cabling
  call. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`_fetch_transit_devices`, `_TRANSIT_BATCH_CHUNK_SIZE`, `TransitGearUnavailable`) \
  Pinned by: `services/reservations/tests/test_transit_gear_rollup.py` (`test_chunking_splits_into_500_and_1`, `test_transport_failure_raises_transit_gear_unavailable`, `test_non_200_raises_transit_gear_unavailable`, `test_include_transit_false_skips_the_cabling_call`)
- **OPS-REPORT-9.** The routes map that failure to 503 `{"error":
  "transit_gear_unavailable"}`. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report`, `get_utilization_report_csv`) \
  Pinned by: none
- **OPS-REPORT-10.** The fleet section lists every device inventory returns (paged by 500,
  with the caller's token, stopping at 100,000), each with its hours over the whole
  window as `utilization_pct`, unclamped; devices with no hours are the idle count, and a
  deleted device stays only in the device section. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_fleet_devices`, `_build_fleet_section`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_fleet_section_math_and_idle_devices`, `test_fleet_pct_is_not_clamped_at_100`, `test_device_missing_from_inventory_stays_in_legacy_section_only`, `test_empty_inventory_yields_empty_fleet_section`, `test_fetch_fleet_devices_paginates_past_500`); `tests/integration/test_reporting.py` (`test_fleet_section_counts_active_and_lists_idle_devices`)
- **OPS-REPORT-11.** When inventory cannot answer, the JSON report answers 200 with
  `fleet` null. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_fleet_devices`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_fetch_fleet_devices_none_on_non_200`, `test_fetch_fleet_devices_none_on_unreachable`, `test_report_route_fleet_null_when_inventory_unreachable`)
- **OPS-REPORT-12.** `execution_run_count` is the number of driver runs started in the
  window, read from execution with the caller's token, and null when execution cannot
  answer. \
  Enforced in: `services/reservations/app/services/reporting_service.py` (`fetch_execution_run_count`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_fetch_execution_run_count_returns_total`, `test_fetch_execution_run_count_swallows_http_errors`, `test_fetch_execution_run_count_returns_none_on_non_200`)
- **OPS-REPORT-13.** The CSV route serves one `section` (`user`, `device`, `fleet`,
  `purpose`, `user_purpose`, `device_purpose`, `purpose_suggested`; anything else is
  422) as `text/csv` with a dated attachment name; hours have four decimals. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report_csv`); `services/reservations/app/services/reporting_service.py` (`report_to_csv`) \
  Pinned by: `services/reservations/tests/test_reporting_service.py` (`test_report_to_csv_user_section`, `test_report_to_csv_device_section_carries_transit_columns`, `test_report_to_csv_purpose_suggested_section`, `test_report_to_csv_rejects_unknown_section`); `tests/integration/test_reporting.py` (`test_utilization_report_csv_download`, `test_utilization_report_csv_rejects_unknown_section`)
- **OPS-REPORT-14.** The `fleet` CSV is 503 `Inventory service is unreachable` when
  inventory cannot answer; the other sections make no inventory call. \
  Enforced in: `services/reservations/app/routers/reservations.py` (`get_utilization_report_csv`) \
  Pinned by: `services/reservations/tests/test_fleet_report.py` (`test_csv_route_fleet_503_when_inventory_unreachable`, `test_csv_route_other_sections_skip_inventory_fetch`, `test_csv_route_fleet_section`)
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

### 8.15 In-app help link

**What it does.** The `?` icon in the header opens the published user manual in a new
tab.

**Surfaces.** `frontend/src/lib/links.ts`, `frontend/src/components/layout/AppLayout.tsx`.

**Rules.**

- **OPS-HELP-1.** The header links to `MANUAL_URL`, the published manual address, defined
  once. \
  Enforced in: `frontend/src/lib/links.ts` (`MANUAL_URL`); `frontend/src/components/layout/AppLayout.tsx` (`MANUAL_URL`) \
  Pinned by: `frontend/src/test/components/AppLayout.test.tsx` (`renders a Settings link and a help link to the published manual`)

**Out of scope.** The manual's content (`docs/manual/`).

### 8.16 Compose and image guards

**What it does.** Static tests keep the deployment files honest: every setting can be set
from `.env`, nothing but the gateway listens beyond the host, and the images run the
locked dependency set.

**Surfaces.** `docker-compose.yml`, `docker-compose.override.yml`, the service
Dockerfiles, and the unit tests under `tests/unit/` named below; the CI backend job
(`.github/workflows/ci.yml`).

**Rules.**

- **OPS-GUARD-1.** Every `Settings` field of every service is passed by the base compose
  file as `${VAR:-default}` with the same default as the code, or is listed as an
  exemption with a reason; a stale exemption fails, and no field declares an alias. \
  Enforced in: `docker-compose.yml` (`environment`) \
  Pinned by: `tests/unit/test_compose_settings_wiring.py` (`test_service_names_matches_discovered_services`, `test_no_settings_field_declares_an_alias`, `test_every_settings_field_is_wired_or_exempt`, `test_no_stale_exemptions`, `test_no_stale_default_mismatch_exemptions`, `test_wired_compose_defaults_match_settings_defaults`, `test_every_active_ai_env_example_key_is_wired_to_ai_orchestrator`)
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

**Out of scope.** The build-identifier wiring guard (OPS-BUILD-2, OPS-BUILD-3); the NATS
CLI documentation guard (OPS-NATS-14).

## 9. Errors

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 401 | `Invalid password` | config login with a wrong password, or with an unreadable auth file | OPS-CONFIG-6, OPS-CONFIG-7 |
| 429 | `Too many failed login attempts; try again later`, with `Retry-After` | config login while its source, or every source, is waiting after failed attempts | OPS-CONFIG-22 |
| 401 | `Session expired`, `Invalid session token` | a config route with an expired or bad session token | OPS-CONFIG-17 |
| 403 | `Change the config password before modifying or applying configuration` | config save or apply before the password is rotated | OPS-CONFIG-9 |
| 422 | validation list | a new config password outside 8 to 32 characters | OPS-CONFIG-8 |
| 422 | `{"errors": ["<KEY> is required", ...]}` | a config save missing a required field, including a masked secret with no source | OPS-CONFIG-11, OPS-CONFIG-12 |
| 422 | `{"errors": ["Unknown settings: <KEY>, ..."]}` | a config save naming a key outside `CONFIG_SCHEMA` | OPS-CONFIG-12 |
| 400 | `No configuration to apply` | config apply with no `config.json` | OPS-CONFIG-15 |
| 200 | `{"restarted": [...], "errors": [...]}` | config apply; failures are entries in `errors` | OPS-CONFIG-13, OPS-CONFIG-16 |
| 403 | `Admin or superadmin role required` | a non-admin lists health snapshots, reads a report, or writes a secret | OPS-HEALTH-3, OPS-REPORT-1, OPS-SECRET-1 |
| 422 | `Invalid last_status: <value>` | an unknown health filter that matches nothing | OPS-HEALTH-4 |
| 500 | `Internal API token not configured` | a device check while execution has no internal token | OPS-HEALTH-6 |
| 403 | `Invalid internal token` | a device check without the right token | OPS-HEALTH-6 |
| 404 | `Device <id> not found`, `Template <id> not found` | a device check on a device or template inventory does not know | OPS-HEALTH-7 |
| 503 | `Could not verify device visibility; nothing was returned. Retry the request.` | a non-admin health read whose visibility lookup cannot be answered | OPS-HEALTH-1 |
| 503 | `Failed to fetch device: <reason>`, `Failed to fetch template: <reason>` (HERD-authored reason: an upstream status, `unreachable (<ClassName>)`, a malformed body, or a class name) | a device check when inventory cannot answer, or answers a body that is not a JSON object | OPS-HEALTH-7 |
| 409 | `{"error": "device_has_no_driver", "message"}` | a device check on a device with no driver | OPS-HEALTH-7 |
| 422 | validation list | a secret body outside the schema, an empty `data` | OPS-SECRET-1 |
| 409 | `A secret with this name already exists` | a taken secret name | OPS-SECRET-2 |
| 404 | `Secret not found` | an unknown secret id or name, or a non-admin with no grant | OPS-SECRET-6, OPS-SECRET-8, OPS-SECRET-9, OPS-SECRET-12 |
| 403 | `manage permission required` | reveal by a holder of `view` only | OPS-SECRET-8 |
| 403 | `Invalid internal token` | a secrets internal route without the right token | OPS-SECRET-10 |
| 409 | `{"error": "secret_in_use", "hypervisor_ids", "hypervisor_names"}` | deleting a secret a hypervisor references | OPS-SECRET-13 |
| 503 | `inventory service unreachable while checking secret references`, `inventory service returned an error while checking secret references` | the delete guard cannot ask inventory, or cannot read its 200 | OPS-SECRET-14 |
| 409 | `Another key rotation committed first; nothing was changed. Retry.` | two rotations at once that the lock did not serialize | OPS-SECRET-18 |
| 503 | `Secret key material is unavailable to this service` | a key version that is missing or does not unwrap under this process's key | OPS-SECRET-17 |
| 401 | `Invalid subject in token` | a preferences route with a missing or non-UUID subject | OPS-PREF-1 |
| 422 | validation list, or `merged <reason>` | a preferences body or merged result over a cap | OPS-PREF-4, OPS-PREF-5 |
| 401 | `Invalid internal token` | the internal preferences read without the right token | OPS-PREF-7 |
| 422 | `window_end must be after window_start`, `Utilization window cannot exceed <N> days (...)`, a validation list | a bad report window or an unknown CSV section | OPS-REPORT-1, OPS-REPORT-13 |
| 503 | `{"error": "transit_gear_unavailable"}` | cabling cannot list transit devices | OPS-REPORT-8, OPS-REPORT-9 |
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
| Out (reservations, report) | cabling | `POST /internal/forks/devices/batch` (internal token, 10 s, chunks of 500) | transit devices | Fail closed: 503 (OPS-REPORT-8) |
| Out (reservations, report) | inventory | `GET /devices` with the caller's token (10 s, paged by 500) | fleet section | Fail open: `fleet` null, or 503 for the fleet CSV (OPS-REPORT-11, OPS-REPORT-14) |
| Out (reservations, report) | auth | `POST /groups/users/groups` with the caller's token (5 s) | group section | Fail open: every user `Ungrouped` (OPS-REPORT-5) |
| Out (reservations, report) | execution | `GET /runs` with the caller's token (5 s) | run count | Fail open: null (OPS-REPORT-12) |
| Out (config) | Docker | the socket's container list and restart | Save and Restart | Reported in `errors`; nothing restarted when its own project is unknown (OPS-CONFIG-13, OPS-CONFIG-16) |
| In | every service | reads `config.json` at import | settings | A missing or corrupt file falls back to the environment (OPS-SET-3, OPS-SET-4) |

## 11. Configuration

[ENV_VARS.md](../ENV_VARS.md) has the full list and the precedence ladder.

| Setting | Default | Effect |
|---|---|---|
| `LOG_LEVEL` (every service but config) | `INFO` | Root log level (OPS-LOG-8) |
| `HERD_BUILD`, `HERD_BUILD_DATE` | `dev`, empty | Build identity reported by `/version` (OPS-VER-3) |
| `HERD_CONFIG_FILE` | `/etc/herd/config.json` | Where services read the config file (OPS-SET-7) |
| `HERD_CONFIG_DATA_DIR` (config) | `/data/herd-config` | Where the config service keeps `config.json`, the marker, and the auth file |
| `CONFIG_ADMIN_PASSWORD` (config) | empty | The config-page password; empty means a generated one (OPS-CONFIG-5) |
| `CONFIG_SESSION_SECRET` (config) | empty | Session signing key; empty means a random per-process key (OPS-CONFIG-17) |
| `CONFIG_LOGIN_MAX_ATTEMPTS` (config) | `20` | Failed config logins across all sources, within the lockout window, that make every source wait (OPS-CONFIG-22) |
| `CONFIG_LOGIN_LOCKOUT_SECONDS` (config) | `300` | The cross-source window and wait (OPS-CONFIG-22) |
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
periods; the 30-minute config session; the per-source config login schedule (two free
failures, then 1 s doubling to 60 s, a source forgotten after 15 quiet minutes, at most
10,000 sources kept); the 500-reservation transit chunk and the
100,000-device fleet stop; the preference caps of 200 keys, 64 KB, and page sizes of 1
to 500.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/common/tests/` (`test_logging.py`, `test_config_loader.py`, `test_base_settings.py`, `test_schema_init.py`, `test_consumer_schema_gate.py`, `test_outbox.py`, `test_jetstream.py`, `test_version.py`); `services/execution/tests/test_health_scheduler*.py`; `services/config/tests/`; `services/secrets/tests/test_crypto.py`, `test_keyring.py`; `services/reservations/tests/test_reporting_*.py`, `test_fleet_report.py`, `test_transit_gear_rollup.py`; the `tests/unit/` guards (`test_compose_settings_wiring.py`, `test_compose_ports.py`, `test_build_args_wiring.py`, `test_consumer_heartbeat_wiring.py`, `test_docs_nats_cli_form.py`, `test_check_image_matches_lock.py`); the frontend tests named in sections 8.3, 8.6, 8.11, and 8.14 | SQLite in memory; the scheduler's concurrency is driven within one process |
| Functional (through the service API) | each service's `test_version.py`; `services/execution/tests/test_health_endpoints.py`; the device check tests in `services/execution/tests/test_api_endpoints.py`, `test_router_endpoints.py`, and `test_router_direct.py`; `services/config/tests/test_config.py`; `services/secrets/tests/test_api.py`; `services/user-profile/tests/test_preferences*.py`; the report route tests in `test_fleet_report.py` | httpx against the app |
| Integration (running stack) | `tests/integration/test_device_check_internal_fetch.py`, `test_outbox_durability.py`, `test_nats_consumer_configs_live.py`, `test_health_alerting_flow.py`, `test_secrets_flow.py`, `test_reporting.py`, `test_dlq_and_idempotency.py`; the live-Postgres suite `services/common/tests/test_outbox_wake_live_pg.py` | No running-stack test covers the config service, a scheduler poll end to end (the alerting test publishes its own events), or a JetStream store surviving a recreate |
| Stress and load | `tests/load/locustfile.py` ([LOAD_TESTING.md](../LOAD_TESTING.md)) | The load test drives reservations, inventory, ACL, export, validation, notifications, and bulk cabling; none of its users polls health, reads reports, or touches secrets or the config service. Scheduler fleet scale is covered only by the unit bounds in section 8.10 |
| Browser end-to-end | `tests/e2e/test_about_page_playwright.py`, `tests/e2e/test_config_playwright.py` (Save and Restart runs only with `HERD_E2E_RESTART=1`), `tests/e2e/test_config.py`, `tests/e2e/test_reporting_page.py` | No browser test covers the health badge or the help link |

Not run for this document: nothing was checked against a running stack. The
integration, live-Postgres, load, and browser suites were read, not run; `tests/unit/`
was run. The startup behavior with an unreachable broker (OPS-NATS-1) was reproduced on
the host with the installed NATS client against a closed local port.

## 13. Known limits and gaps

### Open defects

None at present.

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
- The config session ends on every config-service restart unless
  `CONFIG_SESSION_SECRET` is set (OPS-CONFIG-17). Recorded in issue #246 and the
  docstring of `_load_session_secret`.

### Rules with no test

- OPS-BUILD-1: the Makefile's build identifier and date.
- OPS-LOG-9: the config service's plain-text logging.
- OPS-SET-9: settings read once at import.
- OPS-SCHEMA-9: schemas created only on an empty volume.
- OPS-OUTBOX-14: the relay does not republish an acknowledged event.
- OPS-NATS-13: JetStream store durability under `make prod`.
- OPS-HEALTH-10: the device check writes no health status row and stages no event.
- OPS-NATS-15: reservations' declaration of `HERD_RESERVATIONS`.
- OPS-REPORT-9: the routes' mapping of a transit failure to 503.
