# Code organization conventions

This file records the organizational rules a change to HERD must respect: where
things live, which helper is the one implementation of a rule, which lists a new
item must be added to, and which deliberate asymmetries must not be "fixed". It has
one section per area of the code. What the system does for a caller (routes, status
codes, state transitions, ledger semantics) is not repeated here; it lives in the
ten specifications under `docs/specs/`. Testing and workflow rules live in
`docs/specs/TESTING.md` and `docs/specs/DEVELOPMENT.md`.

## Contents

- [Service boundaries](#service-boundaries)
- [Shared helpers in herd_common](#shared-helpers-in-herd_common)
- [Auth and identity](#auth-and-identity)
- [Gateway and events](#gateway-and-events)
- [Reservations and the state machine](#reservations-and-the-state-machine)
- [Cabling and forks](#cabling-and-forks)
- [Execution and wiring](#execution-and-wiring)
- [AI orchestrator](#ai-orchestrator)
- [Config service](#config-service)
- [Driver packages and the sandbox](#driver-packages-and-the-sandbox)
- [Inventory and device config](#inventory-and-device-config)
- [Frontend](#frontend)
- [L3 routing intent](#l3-routing-intent)
- [Purpose classification](#purpose-classification)

## Service boundaries

Behavior is specified in `docs/specs/inventory.md`, `docs/specs/topology.md`, and `docs/specs/reservations.md`; this section covers only how the code is organized.

- Twelve FastAPI services live under `services/` plus the shared `common` package (thirteen directories). Each service owns its FastAPI app (`app/main.py`), its own Postgres schema, its own Alembic migration chain, and its own test suite.
- There are no cross-schema foreign keys or JOINs. Services reference each other only by UUID, validated over HTTP at the service boundary. A feature that spans services makes an HTTP call to the owning service; it never reaches into another service's schema.
- Physical and cloud devices never mix in one reservation or topology (`topology_type` is enforced at the DB, in `reservation_service`, and in the topology editor). The rule covers the booked PHYSICAL device set only: a dynamic-only booking is CLOUD, mixed bookings of physical devices plus DYNAMIC instances are intended, and an instance device materialized into a PHYSICAL reservation is normal.
- A schema change means adding an Alembic revision under `services/<svc>/migrations/versions/`. The Postgres schemas are created by `infra/postgres/init.sql`, which runs only on a fresh data volume, so a NEW service must add its `CREATE SCHEMA` line there (this bit the `integration` and `secrets` scaffolds).
- Prefer extending `herd_common` (imported as `from herd_common import ...`) over duplicating a helper across services.
- The router directory is `app/routers/` for most services but `app/routes/` for `cabling` and `ai-orchestrator`. Every service's entrypoint is `app/main.py`.
- "Dev mount" means `docker-compose.override.yml` bind-mounts the code for hot-reload under `make up`; "rebuild" means a change needs `docker compose up -d --build <svc>`, not a restart. An `herd_common` edit hot-reloads only in services with a dev mount; the four rebuild-only services need the rebuild for it too.

| Service | Owns | DB / `migrate-<svc>` | Routers | Dev mount | NATS role |
|---|---|---|---|---|---|
| auth | JWT/LDAP, RBAC, users, groups, LDAP group sync (ADR 0011) | yes | routers/ | yes | none |
| inventory | templates (incl. dynamic), devices, ports, drivers, device groups, hypervisor registry, config-apply jobs | yes | routers/ | yes | none |
| reservations | lifecycle, conflict detection, expiration sweep, dynamic requests | yes | routers/ | yes | outbox producer |
| cabling | connections, topology canvas, pathfinding, reservation forks | yes | routes/ | yes | none |
| acl | resource-level grants | yes | routers/ | rebuild | none |
| execution | driver sandbox, health polling, dynamic-instance ledger, wiring reconcile | yes | routers/ | yes | consumer and producer |
| ai-orchestrator | LLM topology generation, reservation assistant, recipe drafting | yes | routes/ | rebuild | none |
| user-profile | preferences | yes | routers/ | rebuild | none |
| notifications | in-app notifications | yes | routers/ | rebuild | consumer |
| config | first-startup web UI | none | none | yes | none |
| integration | versioned `/api/v1` facade, outbound webhooks | yes | routers/ | yes | consumer |
| secrets | encrypted-at-rest credential store (ADR 0003), ACL-gated, envelope encryption under an env KEK | yes | routers/ | yes | none |
| common | shared `herd_common/` package; no container, no `app/` | n/a | n/a | mounted into each mounted service | n/a |

- `config` has no database, so it has no `migrate-config` or `shell-config` target. `ai-orchestrator` does have one (its `ai_orchestrator` schema). `common` ships its code under `herd_common/`, not `app/`.
- `inventory` and `cabling` expose bulk import/export routers (`app/routers/bulk.py` and `app/routes/bulk.py`): inventory for devices and templates, cabling for topologies. Both match by name and update in place rather than duplicating.
- Three cross-service guards protect referential integrity across the no-cross-schema-FK boundary: inventory's admin device DELETE, cabling's POST /connections existence check, and secrets' `delete_secret`. Inventory's device DELETE asks reservations and cabling through ONE helper, `assert_device_deletable` in `services/inventory/app/services/device_delete_guard.py`; do not add a second copy. Secrets' `inventory_guard` module docstring records the claims-registry migration path if a second consumer of secrets ever appears.
- Cabling's POST /connections 422s when either device does not exist, because inventory's GET /device-groups/device/{id} 404s for a missing device while `200 []` means real-but-ungrouped; the guard's fail-open path is ONLY for transport and 5xx outages.
- TEARDOWN CONTRACT: a device's cables are deleted BEFORE the device, and before a port is deleted or renamed (#1023). The test-side helpers and the repo-root guard that enforce it are described in `docs/specs/TESTING.md`.
- CSV formula neutralization (#910): every CSV writer passes TEXT cells through `herd_common.csv_safety.csv_safe_cell` and every CSV importer passes them through `csv_unsafe_cell`; the frontend twin is `csvSafeCell` in `frontend/src/lib/csvSafety.ts`, applied BEFORE the RFC 4180 quoting in `ReportingPage.tsx`. Writers today: cabling topology CSV, inventory device and template CSV (a `text_columns` set threaded symmetrically through export and import), the utilization report, the Reporting page. Never quote enumerations, numbers, booleans, or the `field_data` and `sections` JSON blobs. A new CSV writer must use the helper and a new CSV importer its inverse. See `docs/BULK_IMPORT_EXPORT.md`.
- For many-item reads prefer the batch endpoints over per-id GET fan-out: inventory's `POST /devices/batch` and cabling's `POST /pathfind/batch` exist for exactly that.
- For many-item cable creates use `POST /connections/bulk` (admin-only, in `app/routes/connections.py`, NOT the `app/routes/bulk.py` topology importer). It shares `_extract_bearer_token` and the per-row validation with single-create. It differs in ONE deliberate way: the device-group boundary check fails CLOSED (an unverifiable device aborts the whole batch with 503), mirroring the fail-closed fork fetch. Do not "fix" it into symmetry with single-create.
- `docs/api/v1-openapi.json`, the published v1 facade contract that `docs/EXTERNAL_API.md` links, is a checked-in file updated BY HAND alongside facade changes; the contract snapshot lives separately at `tests/contract/snapshots/v1.json`, and an additive facade change touches both.
- The `docs/` set is larger than the files the README names; grep `docs/` rather than assuming a topic is undocumented.

## Shared helpers in herd_common

Behavior is specified in `docs/specs/operations-and-observability.md`; this section covers only how the code is organized.

`services/common/herd_common/` has no container. Where a helper below exists, use it; never write a local copy.

- `internal_auth.internal_token_matches`: the constant-time, fail-closed X-Internal-Token verifier every service uses instead of `==` (#311).
- `acl.py`: the closed-by-default ACL-service and reservations-ownership HTTP checks, shared by secrets' `delete_secret` guard, inventory's device-config and apply-job manage checks, and execution's `_user_has_acl_manage` (which delegates to `user_has_grant`). `user_has_manage_or_owns_active_reservation` is the reservation-owner free pass documented in `docs/ROLES.md`. No private ACL reader remains. A non-object 200 body is no grant.
- `device_config.py`: the per-connection-type `configure` schema registry plus the SSRF-guarded driver-published-schema sanitizer behind `validate_device_config` and `validate_device_config_with_schema`, shared by inventory's driver and device-config routers, ai-orchestrator's config validator, and execution's driver-call path.
- `advisory_lock` (#513): the dialect-gated Postgres advisory-lock idiom, `xact_lock` (transaction-scoped, blocking) and `session_try_lock`/`session_unlock` (session-scoped try-lock), each a no-op on non-Postgres so SQLite needs no branch. Never hand-roll `pg_advisory_lock` SQL in any service. `xact_lock` users: reservations' `_acquire_device_locks`, execution's VLAN allocation, secrets' DEK rotation.
- `rehearsal.rehearsal_session` (#1064): the ONE rolled-back import dry-run session, used by inventory's importers and cabling's topology importer. Never reintroduce a dry run that skips the service calls.
- `public_address.is_public_address`: the ONE public-address predicate, used by ai-orchestrator's `docs_web.py` and integration's webhook destination check.
- `config_redaction.redact_config`: the masking of stored driver arguments.
- `database.make_database`: a service's `app/database.py` is ONE line, `engine, AsyncSessionLocal, Base, get_db = make_database(settings.database_url)`. `engine` must stay a real module attribute (auth's `_SyncSlot` reads it at call time). A new service does not copy `database.py`.
- `cors.add_cors_middleware(app, settings.cors_origins)` everywhere except `config` (`allow_origins=["*"]` on purpose). A new service does not copy a CORS block.
- `auth_settings`: `secret_key: AuthSecretKey` and `algorithm: JwtAlgorithm = "HS256"`; a new service copies those two lines, never `str`.
- `auth.caller_id(payload)` parses the JWT `sub`.
- `pagination.paginate(db, stmt, skip=, limit=)` is the count-then-page helper for the six list endpoints; ORDER BY stays with the caller. Notifications keeps `offset` and `le=200` by decision, mapping `skip=offset`.
- `internal_client.call_service(base_url, method, path, *, timeout, auth, json_body=, params=)` with `InternalTokenAuth` or `ForwardedAuth` returns the raw response so each caller keeps its own error mapping. `InternalTokenAuth.on_behalf_of` forwards a user bearer beside the internal token.
- `ttl_cache.TTLCache` and `SingletonTTLCache` keep the check, lock, re-check, fetch, store sequence. A fetch that could not get an answer returns its fallback wrapped in `Uncached(value)`; only the call site decides what is a failure (#1075).
- `csv_safety` (see Service boundaries), `webhooks` (`sign_body`, `WEBHOOK_SIGNATURE_HEADER`; the notifications webhook channel has no local signing copy), `l3_route_identity.route_identity_key`, and `l3_validation` (see L3 routing intent).
- `version`: `service_version("herd-<svc>")` for `FastAPI(version=...)` and `add_version_route(app, service=, distribution=)` for the unauthenticated `GET /version`, registered beside `/health` (including on a poller-only execution replica). Never hardcode a version in an `app/main.py`. Exceptions: `integration` keeps `version="1.0.0"` (the published facade contract) and registers the route with `include_in_schema=False`; the config service uses its own stdlib copy (`services/config/app/version.py`).
- `jetstream` (see Gateway and events), `jwt_codec` and `auth_settings` (see Auth and identity), `bounded_tar.BoundedTarStream` (see Driver packages and the sandbox), `logging.JSONFormatter` (see Driver packages and the sandbox, log extras).
- `HerdBaseSettings` (`herd_common.base_settings`) owns the source-order classmethod and shared model_config; a new service's `app/config.py` subclasses it and declares only its own fields. All settings resolve through `herd_common.config_loader.herd_settings_sources`.
- A new setting needs the Settings field, the compose line (or an entry in the wiring guard's `_EXEMPTIONS` with a reason), and a `docs/ENV_VARS.md` row; a new AI setting also needs `.env.example`. Compose passes a variable to a service ONLY through an explicit `${VAR}` or `${VAR:-default}` line in its `environment:` block (no service uses `env_file`), with the default equal to the Settings default. A field wired under another variable name needs a `_RENAMED_WIRING` entry. `tests/unit/test_compose_settings_wiring.py` refuses anything else.
- A service-local helper list, for orientation: execution `services/execution/app/services/_uuid_utils.py` (`as_uuid`), inventory `services/inventory/app/services/manage_guard.py`, cabling `_to_delta` in `routes/forks.py`, cabling `schemas/_types.py` (`UUIDStr`, `OptionalUUIDStr`, `UUIDStrList`; do NOT add `WithJsonSchema` there, it would change the published OpenAPI).
- The poll floor: inventory's `MIN_POLL_INTERVAL_SECONDS` must equal execution's DEFAULT `HEALTH_POLL_SCHEDULER_TICK_SECONDS`; there is no separate floor setting.

## Auth and identity

Behavior is specified in `docs/specs/identity-and-access.md`; this section covers only how the code is organized.

- Two auth modes: a forwarded user JWT (each downstream service re-validates RBAC locally; there is no central gate) and the internal token `X-Internal-Token` for service-to-service side-effect writes with no acting user.
- Roles are user, admin, superadmin. Inside the auth service every authorization decision uses the EFFECTIVE role through `effective_role` and `get_effective_role` in `services/auth/app/dependencies/auth.py`; `require_role` and the `POST /tokens` rank checks depend on it. The other ten services trust the claim alone via `herd_common.auth`.
- Auth's HTTP tests that need the real claim path use `make_client()` with no user (see `docs/specs/TESTING.md`).
- Token settings are validated at load and every access-token decode is one helper. Every service that checks tokens declares `secret_key: AuthSecretKey` (no default anywhere, ai-orchestrator included) and `algorithm: JwtAlgorithm = "HS256"` from `herd_common.auth_settings`. The algorithm is HS256, HS384, or HS512 only.
- EVERY access-token decode calls `herd_common.jwt_codec.decode_access_token(token, key, algorithm)`, the ONE PyJWT decode (IAM-JWT-4, IAM-JWT-5, IAM-JWT-6, OPS-CONFIG-23). `leeway` is 10 s and `enforce_minimum_key_length` stays OFF (owner decisions). The helper lets every `jwt.PyJWTError` propagate.
- The three decode sites (`make_auth_dependencies`, auth's `verify_access_token`, notifications' `get_bearer_credentials`) catch `jwt.PyJWTError`, NEVER only `InvalidTokenError`: `InvalidKeyError` is a `PyJWTError` but not an `InvalidTokenError`, and narrowing the catch turns a bad key into a 500. A key error is logged ONCE at ERROR through `herd_common.auth.log_unusable_key`.
- The round trip that validates a key ignores `InsecureKeyLengthWarning` inside its own two calls ONLY. Never suppress that warning anywhere else and never add a `filterwarnings` ignore for it. A secret under 32 UTF-8 bytes loads and logs one warning (byte length only).
- `HerdBaseSettings` sets `hide_input_in_errors`, so a Settings error never prints the value.
- The config service keeps a stdlib copy of the blank, placeholder, and algorithm rules in `services/config/app/auth_rules.py` (same two message strings, pinned equal to the shared module by `services/config/tests/test_auth_rules.py`); the key-shape check and the round trip are not copied, because the service ships PyJWT but not `herd_common`.
- Repo-root guard: `tests/unit/test_jwt_settings_wiring.py` refuses a PyJWT decode anywhere but `DECODE_HELPER` or a `DIRECT_DECODE_ALLOWED` entry (one today: the config service's `_verify_token`), a decode without a one-element `algorithms=` list or with signature verification off, and any `services/*/app/config.py` whose `secret_key` or `algorithm` lacks the shared type or carries a default.
- `grep -rn --include=*.py jose services tests` must print nothing; no Python file, test, or comment names the old library.
- Every service conftest signs with ONE 64-character hex test key (see `docs/specs/TESTING.md`).
- `pyjwt` is a runtime dependency of common, auth, and notifications only; ai-orchestrator, cabling, integration, and secrets carry it in their `dev` extras. `services/config/app` must stay `herd_common`-free.
- Device-scoped non-admin reads in inventory go through ONE helper, `check_device_read_visibility` in `services/inventory/app/services/device_visibility.py` (admins unfiltered; a hidden device answers with the route's OWN unknown-id 404 detail, passed as `not_found_detail`; unavailable visibility is 503). A new device-scoped read must use it. Three older routes (`GET /devices/{id}`, `GET /ports/{id}`, `GET /device-groups/device/{id}`) carry inline equivalents.
- Inventory's ONE non-admin visibility path is `_resolve_visible_device_ids` (in `device_visibility.py`); `GET /device-groups/visible-devices` uses it too. Execution has its own one resolver, `resolve_caller_visibility` in `services/execution/app/services/device_visibility.py`, which its non-admin device-scoped reads must use. Cabling's is `resolve_caller_visibility` in `services/cabling/app/services/visible_devices.py`; no inline copy remains.
- LDAP group sync lives in auth: the directory client in `ldap_service.py` (`fetch_group`, `resolve_member`, `resolve_members`, `user_present_by_email`; a client function answers `None` or `skip_reason` only for what the directory said and raises `LdapUnavailableError` when it could not be asked: error is never absence), the reconciler in `ldap_sync_service.run_sync`, the loop in `services/auth/app/tasks/ldap_sync_loop.py`, and the stale-run reaper `reap_stale_running_runs`. Rules for a change there: sync functions refuse anonymous binds via `require_service_account`; do not reintroduce a presence flag on the mapping store; do not move the reap outside `_SyncSlot` and do not add a heartbeat column (both considered and rejected); every sweep flip is a guarded compare-and-swap recorded only after commit. Sync-now is serialized by an asyncio lock plus a Postgres session-scoped advisory lock owned by `ldap_sync_service`. `errorDetail` lives in a single copy at `frontend/src/lib/errors.ts`.
- The directory mapping 503 detail is the fixed `MAPPING_DIRECTORY_UNAVAILABLE_DETAIL`; directory text goes to the log message, class in `extra`.
- Auth startup must survive a schema one migration behind (#1261): every startup existence check selects ids only (`superadmin_exists`, `email_registered`, `group_name_exists`). A NEW startup read in any service must select only the columns it needs (IAM-BOOT-6).
- Auth's four paginated responses subclass `app/schemas/pagination.py`'s `Paginated[T]` (auth-local by decision); never use the bare generic as a `response_model`, since subclassing keeps the OpenAPI component names.
- User profile (#1252): `PATCH /users/{user_id}/profile` is ONE self-or-admin route, deliberately NOT `require_role`; the frontend pieces are `ProfileForm.tsx`, `EditProfileDialog.tsx`, `displayName`, and `profileSaveErrorText` in `lib/errors.ts`.
- Dynamic template visibility (#1053): inventory's `resolve_visible_dynamic_hypervisor_ids` and `dynamic_template_visible` are the gate; the hypervisor `device_group_id` is the only grant path.

## Gateway and events

Behavior is specified in `docs/specs/integration.md` and `docs/specs/provisioning-and-wiring.md`; this section covers only how the code is organized.

- Traefik v3 terminates TLS and routes `/api/<service>/...` path prefixes. Its static configuration is the `traefik` command list in `docker-compose.yml`; only the dynamic config and the TLS certs live under `infra/traefik/`. Never add a traefik `command` to an override (it REPLACES the base list, constraint included).
- Declare JetStream streams ONLY through `herd_common.jetstream`: `ensure_stream` for the owning producer and `ensure_stream_exists` for consumers. A bare `js.add_stream` against an existing stream with a different config raises; no `.add_stream(` call remains in any service's `app/`. Each stream is created by its producing service's startup.
- Create durables through `herd_common.jetstream.ensure_consumer` before `pull_subscribe`. No ConsumerConfig carries `backoff`; every transient NAK passes `delay=` through `herd_common.jetstream.nak_delay` from the `NATS_NAK_BACKOFF_SECONDS` schedule.
- Events reach NATS through a transactional outbox (`herd_common/outbox.py`): the producer writes the event row in the same DB transaction as the state change and a background relay publishes it with a `Nats-Msg-Id` header plus a payload `event_id`. Never publish a state-change event directly to NATS from request code; write an outbox row.
- Consumers (execution, notifications, integration) are durable pull consumers with service-scoped DLQ subjects; the 4-token DLQ subjects sit deliberately outside the 3-token consumer filters. Consumers fetch ONE message per pull; do not reintroduce a batch without a pull API that returns partial batches promptly.
- NATS consumers start via `herd_common.consumer_schema_gate.start_consumer_when_schema_ready`.
- Heartbeat: no in-flight message may reach `ack_wait` while its handler runs. The ONE implementation is `herd_common.jetstream.keep_messages_alive`; `process_batch_with_heartbeat` wraps a fetched batch, and execution, integration, and notifications all run every batch through their module-level `process_batch`. Never wrap `await heartbeat` in `try/except CancelledError`. `ack_wait` is `NATS_ACK_WAIT_SECONDS` (default 30, minimum 2) and the heartbeat interval is `heartbeat_interval(NATS_ACK_WAIT_SECONDS)`, derived ONLY in `herd_common.jetstream`. Guard: `tests/unit/test_consumer_heartbeat_wiring.py` refuses a module that calls `pull_subscribe(` without the helper, keeps its own heartbeat task, hardcodes `ack_wait`, or derives its own fraction.
- Connect: every service connects through `herd_common.jetstream.connect_nats` (a bounded first connect that raises `NoServersError`, then the reconnect cap is switched to -1). Never write a bare `nats.connect(..., max_reconnect_attempts=-1)`.
- Decode: every consumer decodes with `herd_common.jetstream.decode_event_object`; non-object JSON is poison (DLQ plus ack).
- Execution's `process_reservation_message` runs ONE corroboration gate, `_verify_reservation_event` with the `_EVENT_CORROBORATION_RULES` table, before any handler.
- Execution's consumer runs driver calls off the event loop via `asyncio.to_thread`.
- Integration: a `/api/v1` facade `reservation_id` is a `uuid.UUID` path param. The webhook destination rule lives in `services/integration/app/services/destination.py` (resolver seam: the module attribute `resolver`). The notifications webhook channel signs through `herd_common.webhooks`. A NEW default event key in notification preferences needs no data migration (`NotificationPreferences.with_defaults`).
- Secrets key rotation: the key table is the source of truth; rotation holds `xact_lock` on `herd-secrets-dek-rotation`.
- Bell UI: the notification bell renders each notification as an `<li>` holding two SIBLING buttons; never nest a button in a button. `useNotifications(params, enabled)` takes `isAuthenticated`.

## Reservations and the state machine

Behavior is specified in `docs/specs/reservations.md` and `docs/specs/dynamic-resources.md`; this section covers only how the code is organized.

- Every reservation status transition is a compare-and-swap through ONE helper, `_claim_status_transition(db, id, expected, new_status)` (`_claim_provision_transition` is its `{PENDING_PROVISION}` wrapper). Never write `reservation.status = ...` as a bare ORM set on a live row. The one existing exception is the sweep's PENDING to PENDING_PROVISION claim, safe only through the row lock (RES-STATUS-3), which lives in `_due_pending_stmt` and `_claim_due_pending_rows`.
- Every release of an exclusive device goes through the ONE holder-aware filter `release_devices_not_held_by_others`. Best-effort releases use `_release_exclusive_devices_best_effort`.
- Every terminal-status write stamps `purpose_classify_requested_at` in the same transaction through `stamp_purpose_classify_requested`. A new terminal writer must call it (the AST test in `test_purpose_classify_marker.py` counts terminal CASes against stamp calls).
- Create, the PATCH extension, and PATCH-add share ONE refusal, `_assert_no_window_conflicts`. The maximum duration lives once in `check_max_duration`. Do not point the calendar back at the lenient `_fetch_visible_device_ids`; the calendar uses `_fetch_visible_device_ids_strict`.
- Reservation list filters go through ONE function, `_apply_list_filters` with `ReservationListFilters`, ANDed AFTER the visibility clause in both `list_user_reservations` and `list_all_reservations`; a new filter goes through it so it can only narrow what the caller may see.
- Both provision backstops measure `PROVISION_TIMEOUT_SECONDS` from `coalesce(provision_started_at, updated_at)`; do not measure a new backstop from `updated_at`.
- Specifications live in `docs/specs/` (template and conventions in its README; rule ids `<AREA>-<TOPIC>-<N>`, each with "Enforced in" and "Pinned by"). `tests/unit/test_spec_references.py` fails on a dangling path, symbol, or test name, so a rename must update the specification in the same PR. A route is specified ONCE in the document of the service that implements it and other documents cite that rule id. The word "withdrawn" in a rule's text exempts it from the reference checks; never use it casually. Every open-defect bullet needs a filed issue number before merge.
- Dynamic-instance ledger (execution): `set_instance_ref` and `mark_active` in `dynamic_instance_service.py` are SQL compare-and-swap updates guarded on `status IN LIVE_STATUSES`; `mark_destroyed` takes the `instance_ref` and `device_id` teardown read (required keywords). Never retire a row from a stale snapshot. `destroy_instance` is idempotent by driver contract (`docs/DRIVERS.md`), and the Hypervisor contract includes keyed destroy with `instance_ref=None`; there is no capability flag.
- Recipe success predicates live ONCE in `services/execution/app/services/recipe_result.py` (`recipe_reported_success`, `created_instance_ref`, `recipe_session_succeeded`); the package validator judges steps with those same functions. Do NOT switch a recipe `login` to the strict rule; validator and consumer must not disagree.
- `get_by_request_id` takes the event's `reservation_id` and the create path scopes its reads and insert to it.
- The dynamic-instance device grant for non-admins is `GET /internal/held-devices` on reservations, asked only when an instance device exists outside group visibility.

## Cabling and forks

Behavior is specified in `docs/specs/topology.md`; this section covers only how the code is organized.

- Cabling owns the fork surface (`routes/forks.py`) and the tables `reservation_fork`, `fork_connections`, `fork_versions`; reservations forwards the user-facing fork surface.
- Every fork WRITER (restore, canvas PUT, save, prune) loads the row with `_load_fork(..., for_update=True)` and holds the lock through commit. Lock any new fork writer too: the version-allocation retry loop reapplies WITHOUT reacquiring the lock, which is safe only because every version-appending writer holds it. A restore appends NO `fork_versions` row (it would falsely trip the wiring-heal reconciler).
- Hop pairing groups hops by `edge_key` and chain-walks each group; positional pairing is UNSOUND and was retired with the legacy device-set path, so do not reintroduce it. Recorded hops apply verbatim, never re-pathfound.
- `fork_connections.edge_key` is deliberately OUTSIDE the connection identity: it drives hop-pairing, not what a connection IS.
- Device node allowlist: `strip_device_nodes` in `services/cabling/app/services/canvas_nodes.py` is the ONE function that reduces `data.device` to `DEVICE_NODE_ALLOWED_KEYS` (never `field_data`), applied at EVERY canvas write boundary and EVERY canvas read. A new key needed by a render or validation site goes into the allowlist AND `persistableDevice` in `frontend/src/lib/canvasNodes.ts`. Migration 0013 uses a FROZEN copy of the allowlist and is never edited; migrations never import app code (a new data migration changes stored rows).
- Node kind is decided ONLY by the two shared predicates in `services/cabling/app/services/canvas_nodes.py`, `is_element_node` and `is_device_node`.
- `classify_element_edge` in `canvas_nodes.py` (re-exported by `fork_save_service.py`) is the ONE classifier for both `validate_canvas_edges` and `resolve_canvas_wiring`. An element edge never becomes a hop, so execution needs no element rule.
- Port constraints are read through the ONE helper the save uses, `edge_port_constraints`; the validator's pre-check must never be looser than the judge (the save). Topology CSV export reads `source_port_name` and `target_port_name`, never a React Flow handle.
- Do not carry `data.layer` into `WireSpec.layer`: the per-line layer is a canvas annotation only; provisioning derives L2 and L3 from the resolved hops.
- Reservations forwards the caller's bearer beside the internal token on the fork GET ONLY (`InternalTokenAuth.on_behalf_of`). A caller that forwards no bearer (execution's `_fetch_fork_intended_wires`, ai-orchestrator) gets the unredacted payload; never forward a bearer from the wiring path.
- Topology text ordering goes through `_topology_order_by` (`lower(coalesce(col, ''))`, `COLLATE "C"` on Postgres only, id tiebreak); a new text sort field must go through it. `delete_topology` calls `assert_topology_deletable` (`services/cabling/app/services/reservation_guard.py`), which fails CLOSED while `find_blocking_reservations` (the edit lock) stays fail-OPEN; do not merge the two. The DELETE route is the only path that deletes a topology row; a new one must call the helper.
- Visibility: `POST /pathfind` and `/pathfind/batch`, the connections list, `GET /connections/{id}`, and the fork GET all use `resolve_caller_visibility`; visibility is resolved BEFORE the row is read. `POST /topologies/import` resolves it once per request before name resolution, and `redact_invisible_device_nodes` runs on the rewritten canvas before validation.
- Cabling's topology importer runs every row through `_import_topology_rows` on `rehearsal_session` for a dry run and on the request session for a commit.
- Template reads and writes drop a stray `data.device` on an element node; template `name` and `description` bounds and taken-name paths go through `_duplicate_name`.
- Terminology drift in the fork editor: docs and the chip say Save/unsaved while the
  fork-mode button reads "Commit to reservation".

## Execution and wiring

Behavior is specified in `docs/specs/provisioning-and-wiring.md`; this section covers only how the code is organized.

- Provisioning at all three layers is connection-driven (ADR 0006 and ADR 0009). The ledger functions (`record_route_active`, `record_route_failed`, `release_route_membership`, and the L2 membership service's coupled release path) are the only L3/L2 assignment API.
- Driver-call success is keyed on the DRIVER RESULT payload through the shared `driver_result_failed` helper in `execution_service.py`; every L1, L2, and L3 driver call site gates through it. `_recipe_reported_success` is deliberately stricter; do not swap the two.
- `_fetch_fork_intended_wires` fails CLOSED. The switch device and template reads (`_fetch_device`, `_fetch_template`) judge inventory's answer through ONE function, `_inventory_record_or_none`. The siblings `_fetch_latest_config`, `_fetch_hypervisor`, `_fetch_secret_value` deliberately KEEP the non-200-means-absent idiom; do not "fix" them into symmetry (WIRE-ORDER-11, WIRE-ORDER-14; #1216 tracks the siblings). `fetch_device` and `fetch_template` for runs go through ONE helper, `_fetch_inventory_internal`.
- VLAN allocation holds ONE `xact_lock` (`herd-execution-vlan-allocation`) around read, choose, insert, commit. `vlan_service.fetch_fabric_id` fails closed. The release supersession guard is judged through `vlan_service.find_superseding_allocation` (one memoized `FabricResolver` per release pass), never by stored fabric ids.
- Driver load classification: the ONE classifier is `driver_loader.is_permanent_load_failure` with row text `driver_loader.driver_load_failure_text`; `nats_consumer._wiring_load_failure` maps it. Do not add another load-failure predicate.
- Retry channels: failure writes pass `row_id` and `expected_status="FAILED"` into `record_l1_failed`, `record_l2_failed`, `record_route_failed`. Row claiming is a CAS on `claimed_until`, taken immediately before a row's own driver call inside the shared applies (`_apply_one_port_action`, `_apply_one_vlan_action`, the L3 per-switch item), never at selection; the budget is `wiring_claim.claim_budget()` (no knob).
- The retry-only `due_failed_*` selects take the `unclaimed` predicate plus FOR UPDATE SKIP LOCKED (`of=<Model>.__table__`, never `of=` the mapped class) and the tick's two skip rules via `wiring_due.tick_would_drive(Model)`. A new non-retryable prefix or skip rule must keep select and drive rule equal. The reason test is `substr(last_error, 1, len(prefix)) != prefix` built from `NON_RETRYABLE_REASON_PREFIXES`, never LIKE; the frozen test is a correlated EXISTS, never a join. The three `failed_*_for_reservation` readers take NEITHER predicate.
- The three `_reattempt_*` functions catch `TransientUpstreamError` per reservation only when the tick passes `upstream_skips`; the manual channel passes None.
- `record_*_failed` never downgrades an ACTIVE row. The retry outcome vocabulary is seven-valued and pinned in `frontend/src/types/reservation.types.ts`, `api/reservations.ts`, `docs/USER_GUIDE.md`, and the manual's live-editing page.
- Freeze: terminal teardown freezes wiring FIRST; all three ledgers re-check frozen at record time. Stale-intent widening is asymmetric: RELEASES diff against (ACTIVE union FAILED intended-ACTIVE) rows, BUILDS diff stays ACTIVE-only.
- Route-set helpers: `get_effective_pinned_routes`, `release_unapplied_route_pin`, `record_route_reconciled`/`record_route_reconcile_failed`; `_gate_l3_drive_routes`; `_apply_l3_adjacency` is the single L3 apply; `remove_route` for a route that is not installed MUST succeed (driver requirement, `docs/DRIVERS.md`; `create_vlan` idempotency likewise).
- Postgres-only suites (`services/*/tests/*_live_pg.py`) must be added to the `_gate-pg-live-tests` recipe; see `docs/specs/TESTING.md`.
- Run reads: `GET /runs` and run-commands use `resolve_caller_visibility`; the run row stores `method_kwargs` masked through `herd_common.config_redaction`; the sandbox child's kwargs travel in a second 0600 temp file, not on argv. `_assert_execute_reservation` and `_assert_action_permitted` are the execute-scope gates. Everything after the run row is written runs under `_fail_run_unexpected`.
- `herd_common.l3_route_identity` holds the route identity; `_route_run_identity` packs the VRF into port_b.

## AI orchestrator

Behavior is specified in `docs/specs/ai-features.md`; this section covers only how the code is organized.

- Provider code lives behind the `LLMProvider` Protocol in `services/ai-orchestrator/app/services/llm_provider.py`. `AIClient` builds neutral types and never imports an SDK directly; add backends under `app/services/providers/` rather than branching on provider in client code. The Anthropic provider builds its own `DefaultAsyncHttpxClient` (`_build_anthropic_http_client`) and aliases `import httpx2 as httpx` LOCALLY; never call `httpx2.alias_httpx()` process-wide. The OpenAI provider stays on httpx.
- `ai_is_configured()` gates the feature; `/api/ai/status` has a seven-key payload and a new field means updating `tests/integration/test_ai_provider_wiring.py`, which pins that exact key set.
- Write tools (`propose_config_change`, `schedule_config_apply`) and docs tools are gated at the DISPATCH boundary (write-tools discipline), not merely hidden from the advertised list. The assistant's device scope is enforced in `ToolDispatcher.dispatch` via `_enforce_device_scope` and the `DEVICE_ID_ARGUMENTS` table; a new device-taking tool must be added to it.
- Cabling-aware resolution lives in `app/services/resolver.py` (pure search) and `app/services/cabling_client.py`; `_resolve_devices` in `generator.py` calls it INSIDE the repair loop. The resolver must judge wireability by the SAME rule as the validator it pre-empts, never a stricter one. Port choice stays with the fork-save resolver (ADR 0006, #531). The repair cap is the setting `ai_generate_max_repairs`; a test pinning repair behavior patches the setting (the old hardcoded `MAX_REPAIR_ATTEMPTS` constant in `generator.py` is gone).
- Commit-time fail fast: `committer.py`'s `_validate_topology_wireable` calls cabling's `POST /topologies/{id}/validate`. `tests/ai_eval/scoring.py`'s `build_canvas_data` mirrors the committer's `_build_canvas_data`; a change to the committer's canvas builder must be mirrored there. The committer still writes `topologyType: "PHYSICAL"` on every node.
- Gate order: `_inventory_provider` returns a LOADER the generate route awaits only after the provider gate, the quota gate, and the upload checks; keep new upstream reads behind the gates.
- Streaming: no async generator may `yield` while a timeout scope is open. The streaming route keeps ONE absolute deadline and bounds only the wait for the next event (`async with asyncio.timeout_at(deadline): ev = await anext(agen, _STREAM_END)`), yields after that scope closes, and closes the inner generator in a `finally`. Guard: `tests/unit/test_no_yield_inside_cancel_scope.py` fails on a `yield` inside `asyncio.timeout`, `timeout_at`, or `TaskGroup` under `services/*/app`.
- No-text turns follow the two-case rule at both loops' exit points in `ai_client.py`. The decision uses a per-call local list, never the dispatcher's `call_log`.
- Errors: upstream reads fail closed with typed errors that carry operation, class, and status, never upstream text (`InventoryUnavailableError`, `ContextUpstreamUnavailableError`). Usage is booked on failure through `usage_repo.record_failed_usage`. Model output never enters a log: the OpenAI provider logs only the SHAPE of a malformed tool-call argument, and schema-violation branches use `e.errors(include_input=False, include_url=False)`.
- Generation refuses a resolved set mixing `PHYSICAL` and `CLOUD` (`_check_uniform_topology_type`); frontend narrowers are `topologyMixedTypesDetail` and `formatMixedTypesDetail`, `aiCommitTopologyUnwireableDetail`, and `topologyUnconnectableDetail` in `frontend/src/lib/errors.ts`.
- Recipe authoring: the drafting loop validates through execution's internal `POST /internal/validate-package`; nothing in the service can upload a driver; the DriversPage "Draft with AI" panel submits through inventory's existing admin upload endpoint. It reads `INTERNAL_API_TOKEN`.
- Docs lookup: sources are registered in `app/services/docs_sources.py` and the web fetch guard is `app/services/docs_web.py`. The built-in `docs/manual/` corpus is COPIED into the image at `/app/docs/manual` (build context is the repo root). `normalize_url` resolves dot segments and the normalized URL is the one requested; `_build_index` skips any entry `resolve_in_root` would refuse. Ranking and HTML-to-text are stdlib.
- `app/services/transcript_retention` uses `call_service` and fails closed by KEEPING the conversation.
- Tgz upload extraction goes through `herd_common.bounded_tar.BoundedTarStream` (see Driver packages and the sandbox); never call `extract_files` on the event loop (`_read_uploads` awaits `asyncio.to_thread`); member names enter text only through `_safe_member_name`; `_extract_tgz` catches `RecursionError` BY NAME beside `TarError`; that catch must never be widened to `Exception` (`test_tgz_walk_lets_an_error_it_does_not_name_propagate` fails if a `KeyError` or `MemoryError` becomes a 400), and it must never be routed through `_GZIP_FAILURES` (that relabels it `ReadError`).

## Config service

Behavior is specified in `docs/specs/operations-and-observability.md`; this section covers only how the code is organized.

- The `config` service is stateless (no DB) and STANDALONE: its `pyproject.toml` has no `herd-common` dependency, its Dockerfile never copies the package in, and its dev mount covers only its own `app/`. ANY `herd_common` import under `services/config/app` boots on the host and crashes the container with `ModuleNotFoundError`. A static AST check in its tests fails on such an import.
- Its stdlib copies of shared rules: `services/config/app/auth_rules.py`, `services/config/app/version.py`, and the class-name-only error rule for `POST /apply`. `services/config/app/login_limits.py` is its own standard-library-only limiter, kept local because the config service stays `herd_common`-free.
- `herd_common.config_loader.herd_settings_sources` resolves settings everywhere; a `config.json` saved through the config UI outranks container env vars, and a file from first-boot auto-bootstrap carries a `config.bootstrapped` marker and ranks BELOW env. `docs/ENV_VARS.md` has the ladder.
- `PUT /settings` refuses keys outside `CONFIG_SCHEMA` (`SCHEMA_KEYS`); `INTERNAL_API_TOKEN` is a schema key. CORS keeps `allow_origins=["*"]` with `allow_credentials=False`. The compose-settings wiring guard does not cover the config service (no `app/config.py`), so its knobs are checked by hand. The config service never calls `setup_logging`.
- Frontend: the config login narrows the 429 through `loginRetryAfterText` and `loginRetryAfterSeconds` in `lib/errors.ts`; the login form is disabled while status reports `configured: false` (`isUnconfigured` in `LoginPage.tsx`).

## Driver packages and the sandbox

Behavior is specified in `docs/specs/device-configuration.md`; this section covers only how the code is organized.

- Drivers are admin-uploaded `.zip` or `.tar.gz` packages containing a `Driver` class. The checked-in `drivers/frr_mgmt/` is the concrete reference for the package layout: a `driver.py` with a `Driver` class plus a `driver_metadata.json`. See `docs/DRIVERS.md`.
- A driver may publish its config schema through an optional `config_schema()` classmethod; extraction failures are swallowed and stored as NULL, and a broken schema must never block a driver load.
- Driver package table:

| Package | Connection type | Purpose |
|---|---|---|
| `drivers/mock_l1/` | Layer 1 Switch | hardware-free test driver, acknowledges port connect/disconnect ops deterministically |
| `drivers/mock_l2/` | Layer 2 Switch | hardware-free test driver, acknowledges VLAN create/add/remove/delete ops deterministically |
| `drivers/mock_l3/` | Layer 3 Switch | hardware-free test driver, acknowledges route ops deterministically, declares `supports_vrf` |
| `drivers/mock_hypervisor/` | Hypervisor | hardware-free test driver for dynamic-resource provisioning (`create_instance`/`destroy_instance`) |
| `drivers/frr_mgmt/` | Management | configures an FRR router over SSH via vtysh (netmiko), honors `context["dry_run"]`, the live-config demo reference |
| `drivers/frr_l3/` | Layer 3 Switch | real FRR route driver over SSH via vtysh (netmiko), NOS lab reference driver, declares `supports_vrf` |
| `drivers/srl_l2/` | Layer 2 Switch | real Nokia SR Linux VLAN driver (mac-vrf network-instances) over SSH (netmiko), NOS lab |

- The mock drivers expose `HERD_mock_fail_actions`, `HERD_mock_raise_actions`, `HERD_mock_sleep_ms` field_data knobs for failure injection. The sandbox is POSIX `setrlimit` caps (`DRIVER_RLIMIT_*`), NOT OS-level isolation; driver packages are trusted code.
- `execute_driver_method` is the ONE child spawn (the `__config_schema__` sentinel and the package validator's dry run both go through it); it starts `python -I -B _runner.py ...` with no `PYTHONPATH` or `PYTHONDONTWRITEBYTECODE`. `_runner.py` applies the rlimits FIRST, THEN `_put_package_on_path` inserts the package directory and `_deps` right after `_RUNNER_DIR`. The module-level `sys.path.insert(0, _RUNNER_DIR)` is load-bearing under `-I`, and the runner dir must stay ahead of the package. Do not move the rlimit call after the package is on the path. A Python subprocess a driver starts gets no `PYTHONPATH` and must be passed the package and `_deps` paths explicitly. The optional `requirements.txt` install runs as `python -I -B -m pip install`.
- Package checks NEVER import `driver.py`: `driver_structure.driver_structure_errors` in `services/execution/app/services/driver_structure.py` is the ONE structural check, used by the load path's `validate_driver` and the package validator alike; it parses via `parse_python_file`. `PARSE_FAILURES` is written out, never `Exception`. `load_driver` removes the attempt's extraction directory when a package check raises. The validator parses each `.py` once and hands driver.py's result to the shared check. Guard: `tests/unit/test_checked_in_drivers_structure.py` runs the check over every package under `drivers/`.
- Extraction is bounded before anything is written: `MAX_ARCHIVE_ENTRIES` and `MAX_EXTRACTED_BYTES` in `driver_loader.py`. A tar is read through `herd_common.bounded_tar.BoundedTarStream` (the ONE implementation, shared by execution's loader, inventory's upload, and ai-orchestrator's extractor), which refuses any single read over `MAX_TAR_READ_BYTES`. A caller that reads a member itself passes its read size through: bound the member first or read in chunks of at most that. Do not swap it for `tarfile.open(mode="r:gz")`.
- The tgz extractor opens the archive with `tarfile.open(..., tarinfo=_BoundedTarInfo)`, which overrides the private `_proc_member`, `_apply_pax_info`, `_proc_gnusparse_00`, `_01`, `_10`; a header guard that counts a declared size must count what the parser READS. Its walk loops on `tar.next()` and clears `tar.members` after each step. Fixed ceilings (`MAX_TGZ_ENTRIES`, `MAX_TGZ_DECOMPRESSED_BYTES`, `MAX_TGZ_MEMBER_HEADER_BYTES`, `MAX_TGZ_GLOBAL_HEADER_BYTES`) are constants with no setting; `_CeilingStream` enforces the decompressed-stream ceiling; do not replace it with a sum of `member.size`.
- Inventory's `_parse_driver_metadata` bounds `driver_metadata.json` at `MAX_DRIVER_METADATA_BYTES`; at load `read_driver_metadata` catches `(OSError, ValueError, RecursionError)` and logs the class only.
- Packages are cached per driver (`DriverCache`), extracted to `<cache>/<driver id>-<random hex>`; `_record_cache_row` writes with `INSERT ... ON CONFLICT (driver_id) DO NOTHING`.
- Driver exception text never reaches a row or API response: `_runner.py` prints a structured line to stderr and `driver_sandbox.py` parses ONLY THE LAST non-empty stderr line (`_parse_driver_exception`); the stored error is `driver raised <ClassName>` and the driver-load failure row is `driver load failed: <ClassName>`. Deliberate exceptions are the package validator's report (`package_validator._validation_error_text`, and `driver.py failed to parse: <message>`).
- Log extras: `herd_common.logging.JSONFormatter` emits every `extra=` key, redacts keys matching `_REDACT_KEY_PATTERN`, and renames envelope-colliding keys to `extra_<name>`. Key-name redaction cannot see inside a string VALUE, so never pass a raw config dict or model-output text through `extra=` (execution's start log passes `method_kwarg_keys` and `context_keys`). The raw text of every sanitized failure is logged in the log MESSAGE, not only in `extra`.
- Template password defaults are masked in ONE place, `services/inventory/app/services/field_redaction.py` (`redact_field_data`, `redact_template_sections`); `_template_to_response` takes a REQUIRED `redact` keyword, so a new caller must decide.

## Inventory and device config

Behavior is specified in `docs/specs/inventory.md` and `docs/specs/device-configuration.md`; this section covers only how the code is organized.

- Device-scoped non-admin reads use `check_device_read_visibility` (see Auth and identity). Config-version and apply-job write routes deliberately 404-before-403 (`docs/ROLES.md`).
- `manage_guard.py` holds inventory's manage helpers: `_assert_driver_can_configure` is the ONE gate called by both apply entry points (`schedule_apply_job`, `apply_config_version`) and re-checked by `apply_scheduler.fire_job`; `_user_has_explicit_manage` backs the immediate apply. Capability comes from the connection type's contract: `herd_common.device_config.CONFIGURE_CONNECTION_TYPES` (`connection_type_supports_configure`) with a parity test pinning it to `REQUIRED_METHODS` in execution's `driver_loader.py`. To apply config to a box that also needs a layer driver, pair them as `frr_l3` and `frr_mgmt` do.
- `driver_for_device` in `services/inventory/app/services/published_schema.py` is the one driver lookup; do not add another copy.
- BOTH apply paths judge execution's answer through ONE helper, `services/inventory/app/services/apply_outcome.py` (`judge_success_answer`, `move_current_config_pointer`, `unreachable_error`, `refusal_error`).
- Migrations 0023 and 0024 add the unique `(device_id, version_number)` index and `claimed_at` on apply jobs. The immediate apply requires an explicit `manage` grant; schedule keeps the manage-or-owner widening. The reservation scope check for scheduling is one 422 per caller kind (`RESERVATION_MISMATCH_ERROR`, `RESERVATION_MISMATCH_ADMIN_ERROR`).
- An unhandled 500 from a bad input is a defect, never a contract. `BulkPortCreate` checks the LONGEST generated name against the 255-character column. The device importer's update branch builds its kwargs CONDITIONALLY, as the template importer does, so an omitted column keeps the stored value. `update_device` reports ONLY a unique violation as a name clash. A schema error's row reason lists field and message and never echoes the row.
- Import dry run is a rehearsal: `import_devices` and `import_templates` run through the SAME service functions as a commit on `herd_common.rehearsal.rehearsal_session`.
- Port guard: `delete_port`, and `update_port` when the name changes, call `port_cabling_guard.assert_port_uncabled`; frontend wording goes through `deletePortErrorText` in `lib/errors.ts`. Template export and import carry `hypervisor_name` listed in `TEMPLATE_CSV_TEXT_COLUMNS`.
- `_fetch_user_group_names` pages auth's `GET /groups` at 500; `_assert_templates_allow_connection_type` guards driver connection-type change.
- The device-config UI: every catch in `DeviceConfigSection.tsx` and `ApplyJobsPanel.tsx` goes through `lib/errors.ts` (`configApplyErrorText`, `configRestoreErrorText`, built on `driverCannotConfigureDetail`, `restoreBlockedDetail`, `formatRestoreBlocked`; `errorDetail` everywhere else), so an object detail never reaches a toast. The load class `DeviceConfigUser` is in `tests/load/locustfile.py`.
- The device exporter leaves out instance devices (`request_id IS NULL`, INV-BULK-21).
- A new `docs/specs/` rule for any route is added in the document of the service that implements it.

## Frontend

Behavior is specified in `docs/specs/topology.md`, `docs/specs/reservations.md`, and `docs/specs/ai-features.md`; this section covers only how the code is organized.

### Layout and shared libraries

- React 19 + Vite + TypeScript under `frontend/`. State is Zustand (`src/stores/`: auth, topology, config, prefs) plus TanStack Query for server state. There is one API client module per domain in `src/api/`; `src/api/client.ts` is the axios instance with the JWT header and silent-refresh interceptor. Topology editing uses a React Flow canvas.
- `frontend/src/test/vitest-jest-dom.d.ts` is a TEMPORARY type shim re-augmenting vitest 5's `Assertion` with jest-dom's matchers; delete it once jest-dom ships a vitest 5 compatible release.
- The admin-role predicate lives ONCE as `isAdminRole(role)` in `src/lib/roles.ts`; do not reintroduce the inline `role === "admin" || ...` form. Single-role checks stay inline.
- All structured error narrowers sit on `structuredDetail` in `lib/errors.ts`; `errorDetail` lives in a single copy there. The manual URL is `MANUAL_URL` in `src/lib/links.ts`.
- Version display: `vite.config.ts` `define` injects `__APP_VERSION__`, `__APP_BUILD__`, `__APP_BUILD_DATE__`, and `src/lib/appVersion.ts` is the only module allowed to touch them. The service list in `api/about.ts` is mirrored BY HAND in `tests/e2e/test_about_page_playwright.py` (note ai-orchestrator at `/api/ai` and integration at `/api/v1`).
- `class-variance-authority` is no longer a dependency; do not add it back for a one-off. `components/devices/DeviceDetailModal.tsx` was removed; the page-based device view is the only detail surface. `AIAssistantTabLegacy` is the DEFAULT render path despite its name.

### Routes and pages

- Admin route gating lives in ONE place: `AdminGuard` in `components/guards.tsx`, wrapping a pathless route group in `routes.tsx` holding the fifteen `/admin/*` routes plus `/reporting`. Adding an admin route means adding it in FOUR places: the group, the Administration dropdown in `AppLayout.tsx`, the literal list in `src/test/routes.test.tsx`, and `REMAINING_GUARDED_PATHS` in `tests/e2e/test_admin_guard_redirect_playwright.py`. Pages under `pages/admin/` and `ReportingPage.tsx` carry NO per-page role check. The route table is the exported `appRouteElements` JSX constant in `src/routes.tsx` (an interim bridge shape, not a pattern to copy).
- Page scroll contract (#1260): `AppLayout` never scrolls; a page scrolls only when its own top-level element is `h-full overflow-y-auto` (both tokens). A new page under the layout needs the wrapper; a new full-screen page needs an entry in the allowlists of `frontend/src/test/pages/pageScrollContainer.test.tsx` (`OUTSIDE_LAYOUT_PAGES`, `NO_PAGE_SCROLL_BY_DESIGN`).
- Dialogs: Tailwind 4's preflight zeroes the user-agent `margin: auto`, so the three native dialogs (`Modal`, `ConfirmDialog`, `BulkImportExport`) carry `m-auto`. A NEW native dialog needs the class AND its file added to the known set in `src/test/components/dialogCentring.test.tsx`. All Modals share `id="modal-title"`.
- Toasts: `<Toaster position="bottom-center" />` in `App.tsx`. Page-end controls on `/config` and `/templates/new` sit above a `pb-56` clearance from `TOAST_CLEARANCE_CLASS` in `src/lib/toastClearance.ts`.
- List filter panel: list pages put search and filters in `components/ui/ListFilterPanel.tsx` (`ListFilterLayout`, `ListFilterPanel`, `FilterSelect`). It owns layout and labels ONLY (no state, query parameters, or persistence); the page decides `showClear`. Width, stacking breakpoint, and stickiness are single constants at the top of the file.
- Persisted filters are read and written ONLY through their helpers: `parseSavedInventoryFilter` and `serializeInventoryFilter` in `src/lib/inventoryFilters.ts`, `src/lib/topologyFilters.ts`, and `src/lib/reservationFilters.ts`; a new filter goes through that pair. A saved search applies through `debouncedUserSearch ?? storedSearch`. A filter change made before the preferences GET returns is held in `preferencesStore.ts` and merged when the load settles. `Pagination` takes optional `pageSizeOptions` and `onPageSizeChange`; `InventoryPage` reads `getPageSize("inventory", 50)` and writes `setPageSize` (`getSavedFilter` was removed).
- Gates live ONCE: `canCancel`, `canRelease`, `canCancelAs`, `canReleaseAs`, `canClassifyPurpose` in `src/lib/reservationStatus.ts` (must match the backend rule exactly, never stricter or looser; do not gate any surface on the status-only functions alone); `canDeleteTopologyAs` in `src/lib/topologyBulk.ts`. Bulk actions fan out over per-id calls with `Promise.allSettled`; the pure pieces are in `src/lib/reservationBulk.ts`.
- `InventoryPage` prunes `expandedIds` to ids still listed while `selected` is cleared. Topology wording goes through `topologyDeleteErrorText`; device page Save sends only changed fields via `deviceEditPayload` in `lib/deviceEdit.ts`.
- `TemplateEditorPage.tsx` disables Save while `identityValid` is false.

### Topology canvas

- Drawing a line between two device nodes opens `components/topology-editor/WiringDialog.tsx`; the old single-pair modal survives as `QuickConnectPopover.tsx`. Confirming emits one enriched edge per line through the shared `addEnrichedEdges` append path (`addEnrichedEdge` is a one-item wrapper; neither dedupes like `addEdge`). Many same-pair edges render as ONE `BundledEdge`, RENDER-ONLY via `groupEdgesForRender`: the store always holds N distinct edges.
- `components/admin/connections/MultiConnectDialog.tsx` is the default create surface on `pages/admin/ConnectionsPage.tsx` and reuses the shared primitives under `components/topology-editor/wiring/` (`PortColumn`, `filterPorts`, `usePortAvailability`, `portAvailability`). The dialog SHELL is deliberately DUPLICATED with `WiringDialog.tsx` (issue #539: do not refactor until a third consumer or a bug that must be fixed twice forces it). Partial-success handling lives in `bulkStaging.ts` `applyBulkResult`.
- Node classification: `isDeviceNode` requires `data.device` to be an OBJECT; predicates `isDeviceNode`, `isDynamicPlaceholder`, `isNetworkElement` live in `frontend/src/lib/canvasNodes.ts`, and every `DeviceNodeData` read must be guarded by the POSITIVE `isDeviceNode`. Node classification keys on the presence of `data.device`, not `node.type` (CSV-imported nodes carry no type). `DeviceNode` renders a device-less node as "Unknown device".
- Dynamic placeholders are EPHEMERAL and never enter `canvas_data`; network elements PERSIST into `canvas_data`, so do not copy the placeholder strip in `persistableCanvas`. Element definitions are in `frontend/src/lib/networkElements.ts`; attachments are created by `ElementAttachDialog.tsx`.
- The live editor is `pages/TopologyEditorPage.tsx`; the node components under `components/topology-editor/nodes/` and `edges/` are the ones in use.
- Fork editor: `ForkHistoryPanel.tsx`, `useForkVersionPreview.ts`, `lib/forkDiff.ts`; the draft overlay of `invalid_edges` goes through `lib/edgeProblems.ts` onto React Flow's RENDER COPIES only and is stripped on the persist path.
- `ReservationDetailModal` remembers classified ids itself because its `reservation` prop is a snapshot. Purpose-classify wording lives in `src/lib/purposeClassify.ts`, hand-mirrored in `tests/e2e/test_purpose_classify_playwright.py`.

## L3 routing intent

Behavior is specified in `docs/specs/topology.md` and `docs/specs/provisioning-and-wiring.md`; this section covers only how the code is organized.

- Cabling parses `data.l3.routes` at the boundary in `services/cabling/app/services/l3_intent.py`; fork save resolves it into `fork_l3_routes` (migration 0011, `ForkL3Route`). The save gate `gate_l3_intent` runs OUTSIDE the fork row lock and the port advisory locks, and the locked re-load after that HTTP window MUST use `_load_fork(..., refresh=True)` (`populate_existing`; a plain second SELECT returns the stale identity-mapped row).
- Validation lives in `services/cabling/app/services/topology_validation.py` (`validate_canvas_edges`, `validate_canvas_l3`, `run_full_topology_validation`). The per-route validator lives ONCE in `herd_common/l3_validation.py` (cabling and execution both import it). `wired_ports` is a REQUIRED keyword through cabling and execution; the shared validator's `None` default means skip and must not be relied on by a new caller. `touched_devices_from_specs` derives from `wired_ports_from_specs`.
- Frontend: `DeviceNodeData.l3.routes` is the only persisted shape and every read goes through `l3RoutesOf(data)` in `lib/canvasNodes.ts` (one frozen `EMPTY_L3_ROUTES`); `setNodeL3Routes` removes `data.l3` on an empty list; `isBlockingRouteProblem` in `lib/l3.ts` gates every count and badge; `routeProblems` clears through `loadCanvasAndClearRouteProblems` at every canvas-replacing site. The client-side destination canonicalizer was REMOVED by decision.
- Execution: `_fetch_fork_intended_wires` returns `ForkIntent` (a list subclass carrying `l3_routes`). Drivers declare `supports_vrf` in `driver_metadata.json`; execution never passes the `virtual_router` keyword otherwise. `frr_l3` renders VRF routes. `infra/nos-test/frr/start.sh` creates the lab VRF fixture; tests never delete it.
- `POST /internal/devices/batch` exists on inventory for these reads.

## Purpose classification

Behavior is specified in `docs/specs/ai-features.md` and `docs/specs/reservations.md`; this section covers only how the code is organized.

- `purpose_category` is validated against `PURPOSE_CATEGORIES`, a plain string list, not a Postgres enum.
- The single-row classifier lives at `purpose_service.classify_purpose_one` (moved out of `tasks/expiration.py` so the router does not import from tasks); the sweep keeps ADR 0013's oldest-first order.
- Terminal writers stamp through `stamp_purpose_classify_requested` (see Reservations and the state machine).
- The frontend gate is `canClassifyPurpose` beside `canCancel` and `canRelease` in `src/lib/reservationStatus.ts`; the admin-only Classify now button lives in `ReservationDetailModal.tsx`, not on the Purpose Review page (`/admin/purpose-review`).
- Contract: `docs/AI_PURPOSE_CLASSIFICATION.md` has the endpoint contract; ADR 0013 is `docs/design/0013-lab-purpose-classification.md`.
