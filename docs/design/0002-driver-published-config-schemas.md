# 0002 - Driver-published configuration schemas

Status: Shipped
Issue: #23 "Driver-published configuration schemas"
Context verified against the live HERD-public tree on 2026-06-09.

Decided mechanism (not re-litigated): a driver advertises its config schema via a
`config_schema()` classmethod on the `Driver` class returning a plain JSON Schema
dict. Inventory fetches it (proxying execution) and uses it to validate device
configs, falling back to today's hardcoded registry when the driver omits it.

## 1. Current state (verified, with citations)

### 1.1 The Driver-class contract (docs/DRIVERS.md)

The driver package is a `.zip`/`.tar.gz` whose root holds `driver.py` (required,
must define class `Driver`), optional `driver_metadata.json`, optional
`requirements.txt`, optional `lib/`/`_deps/` (`docs/DRIVERS.md:26-39`). Methods
are per-connection-type: L1 (`login/logout/connect_ports/disconnect_ports/status`),
L2 (`login/logout/create_vlan/add_to_vlan/remove_from_vlan/delete_vlan/status`),
L3 (`login/logout/configure_route/remove_route/status`), Management (`login/logout/configure/backup/status`)
(`docs/DRIVERS.md:14-19`, class sketches at `:140-219`, `:448-537`, `:601-609`).

All current methods are instance methods taking `self` plus per-call args;
`__init__(self, context: dict)` stores the injected `HERD_`-prefixed context
(`docs/DRIVERS.md:148-156`). `config_schema()` is fundamentally different: it
describes the driver class, not a device session, so it must NOT need a live
`context` (no credentials, no device). A classmethod is the right shape; it slots
into the contract as a new, optional, connection-agnostic capability method.

The doc already anticipates this feature. `docs/DRIVERS.md:611-628` ("AI-generated
configs are allowlisted (B8)") states the schema registry lives in
`services/common/herd_common/device_config.py` and that "a future iteration can
let each driver publish its own method schema via the execution service instead."
This design is that iteration.

### 1.2 How execution loads and invokes a driver (the SHA256 cache + subprocess)

- `load_driver(db, driver_id, driver_sha256, driver_filename, connection_type)`
  is the cache front door (`services/execution/app/services/driver_loader.py:178-244`):
  checks `get_cached_driver` by SHA256 (`:40-63`), on miss downloads from
  inventory's `/drivers/{id}/internal-download` (`:66-76`), extracts (`:79-94`),
  validates required methods (`:97-131`), captures `driver_metadata.json`
  (`:134-154`), writes a `DriverCache` row keyed by `driver_id` with `sha256`,
  `local_path`, `metadata_json` (`:223-238`). Returns the extracted directory.
- The sandbox invocation is `execute_driver_method(driver_path, action, context, ...)`
  (`services/execution/app/services/driver_sandbox.py:93-104`). It writes context
  to a temp JSON file, spawns `python _runner.py <driver_path> <action>
  <context_file> [kwargs_json]` (`:176-184`), applies POSIX rlimits via
  `preexec_fn` (`:51-81`, `:235`), and returns
  `{success, output, error, duration_ms, transcript}` (`:245-251`). The child's
  stdout is parsed as JSON into `output` (`:241-244`).
- The runner (`services/execution/app/services/_runner.py:30-64`) loads
  `driver.py`, does `driver = module.Driver(context)` (`:54`), then
  `getattr(driver, action)` and calls it (`:57-61`), printing `json.dumps(result)`
  to stdout (`:64`).

Can the subprocess return a JSON Schema dict for a classmethod? Yes, with one
runner change. The schema is a plain dict, so it serializes over stdout exactly
like any method's return (`_runner.py:64`, parsed at `driver_sandbox.py:241-244`).
The catch: `config_schema()` is a classmethod and must not require a constructed
`Driver(context)`. The current runner always instantiates first (`_runner.py:54`).
A driver whose `__init__` needs real credentials (e.g. the Calient example reads
`context["HERD_ip_address"]`, `docs/DRIVERS.md:378-383`) would crash if we
instantiated it just to read a schema. So the extraction path must call the
classmethod on the class object without instantiating (see 2.2).

### 1.3 How inventory validates config TODAY (the hardcoded registry)

The registry is `CONFIG_SCHEMAS` in
`services/common/herd_common/device_config.py:21-104`: hardcoded JSON Schema dicts
keyed by connection_type for `Management`, `Layer 2 Switch`, `Layer 3 Switch`.
`validate_device_config(connection_type, config, *, role)` (`:115-153`)
short-circuits on empty config (`:130-131`), 422s on missing/unknown
connection_type (`:135-146`), else runs
`jsonschema.validate(instance=config, schema=schema)` (`:148-153`) and raises
`ConfigValidationError` on failure.

The inventory write path is the config-version create endpoint:
`create_config_version` derives `connection_type = _connection_type_for(device)`
(`services/inventory/app/routers/device_configs.py:203`, helper at `:64-72` reads
`device.template.driver.connection_type`), then
`validate_device_config(connection_type, body.config, role=device.name)` and maps
`ConfigValidationError` to 422 (`:205-208`). `restore_config_version` (`:230+`)
and the AI commit path (`services/ai-orchestrator/app/services/committer.py:229`)
call the same validator. The AI orchestrator re-exports the symbols
(`services/ai-orchestrator/app/services/config_validator.py:7-18`) and exposes
them to the LLM via the `get_device_config_schema` tool
(`services/ai-orchestrator/app/services/tools.py:444-503`), which reads
`CONFIG_SCHEMAS.get(connection_type)` directly (`:498`).

What changes: `validate_device_config` (or a thin wrapper in inventory) must
prefer a driver-published schema when one exists, and fall back to
`CONFIG_SCHEMAS[connection_type]` otherwise. The fallback is load-bearing for
backward compatibility: every existing driver omits `config_schema()`, so the
registry must remain the default.

### 1.4 Existing execution to inventory HTTP contract for driver introspection

Today the direction is inventory to execution for config apply: `apply_scheduler`
POSTs `/execute/internal` (`services/inventory/app/services/apply_scheduler.py:95`),
`device_configs.py:304` POSTs `/execute`. The reverse (execution to inventory)
exists only for the package download: `download_driver_package` GETs inventory
`/drivers/{id}/internal-download` with `X-Internal-Token` (`driver_loader.py:66-76`;
inventory endpoint at `services/inventory/app/routers/drivers.py:152-153`). There
is NO endpoint today for inventory to ask execution to introspect a driver. That
is the new contract this feature adds (2.3).

### 1.5 Dependency check

`jsonschema` is already a workspace dependency:
`services/common/pyproject.toml:9` (`jsonschema>=4.21.0`),
`services/ai-orchestrator/pyproject.toml:16`, resolved to 4.26.0 in
`uv.lock:1558-1569`. No new dependency is needed for validation. Execution does
not currently depend on `jsonschema` and does not need to for extraction; if we
want to lint the extracted schema with `Draft202012Validator.check_schema` inside
execution, that is a one-line dep add (see Risks).

## 2. Design

### 2.1 Driver-class contract addition

New optional classmethod on the `Driver` class:

    class Driver:
        @classmethod
        def config_schema(cls) -> dict:
            """Return a JSON Schema (draft 2020-12) describing accepted
            `configure` inputs. Pure: must not open connections, read
            credentials, or touch context. HERD calls this on the class object
            without instantiating Driver, so it cannot rely on __init__ having
            run. Return a plain dict; HERD validates device configs against it
            before any configure action is applied."""
            return {
                "type": "object",
                "properties": {
                    "vlan": {"type": "integer", "minimum": 1, "maximum": 4094},
                    "hostname": {"type": "string", "maxLength": 128},
                },
                "additionalProperties": False,
            }

Backward-compat / fallback (explicit): the method is OPTIONAL. Resolution order
when validating a device config for connection_type `C`:

1. If the device's driver defines `config_schema()` and it returns a valid JSON
   Schema, validate against THAT.
2. Else fall back to `CONFIG_SCHEMAS[C]` from `herd_common/device_config.py`
   (today's behavior, unchanged).
3. Else (no published schema and no registry entry for `C`) keep today's 422
   from `validate_device_config` (`device_config.py:141-146`).

This guarantees every existing driver behaves identically to today. A driver that
ships a broken `config_schema()` (raises, returns non-dict, or returns an invalid
schema) must also fall back to the registry rather than break validation, with a
logged warning. We do NOT fail the config write because schema extraction failed;
we degrade to the registry.

`docs/DRIVERS.md` gets a new "Driver-published config schema" section documenting
the classmethod, the purity contract, draft version, and the fallback.
`REQUIRED_METHODS` in `driver_loader.py:24-37` is NOT extended (the method is
optional, so upload validation must not require it).

### 2.2 Execution-side extraction (reuse the cache + sandbox)

How to run it in the runner. Add a sentinel action `__config_schema__` handled
specially in `_runner.py`: load the module, read
`getattr(module.Driver, "config_schema", None)`, and if callable, invoke it on
the CLASS without instantiating `Driver(context)` (avoids the credential-dependent
`__init__` problem from 1.2). Emit `{"has_schema": bool, "schema": dict | None}`
to stdout. The context file is passed an empty `{}`. Concretely, in
`_runner.py:54-61`:

    if action == "__config_schema__":
        fn = getattr(module.Driver, "config_schema", None)
        result = {"has_schema": False, "schema": None}
        if callable(fn):
            schema = fn()  # classmethod: no instance, no context
            result = {"has_schema": True, "schema": schema}
        print(json.dumps(result, default=str))
        return
    # ... existing instantiate + call path

Whether to sandbox it. Yes, reuse `execute_driver_method`. The classmethod is
still untrusted driver code (it could loop or allocate), so it must run under the
same rlimit subprocess and a short timeout. Add a thin wrapper that calls
`execute_driver_method(driver_path, action="__config_schema__", context={},
timeout=settings.status_check_timeout_seconds)` (reuse the short status timeout,
`driver_sandbox.py:138-139`). No new sandbox mechanism is needed.

Cache reuse. The schema is a property of `(driver_id, sha256)`, identical to
`driver_metadata.json`. Reuse `load_driver` to resolve `driver_path` (cache hit
on SHA256, `driver_loader.py:191-194`), then extract once. Persist the result on
the existing `DriverCache` row as a new nullable `config_schema_json` text column
(mirrors `metadata_json`, `driver_loader.py:223-238`), populated during
`load_driver` (extract right after `read_driver_metadata`, `:218-221`) and read
back via a `get_driver_config_schema(db, driver_id)` helper paralleling
`get_driver_metadata` (`:157-175`). No extra subprocess on the hot path; the
schema is captured once per SHA256 at first load and invalidated automatically
when the SHA256 changes (`:50-57`, `docs/DRIVERS.md:728`).

New execution endpoint (the contract inventory calls):
`GET /drivers/{driver_id}/config-schema`, internal-token auth (mirror
`_require_internal_token`, `executions.py:42-46`). It needs `sha256`, `filename`,
`connection_type` to call `load_driver`; inventory owns those
(`driver_package.py:31-35`), so inventory passes them as query params (matches how
`run_driver_action` already receives them, `execution_service.py:287-290`).
Response: `{"driver_id", "sha256", "has_schema": bool, "schema": dict | None,
"source": "driver" | "none"}`.

### 2.3 Inventory-side: fetch endpoint + validation wiring

Fetch endpoint (proxy): `GET /api/inventory/drivers/{driver_id}/config-schema` in
`services/inventory/app/routers/drivers.py`. Loads the `DriverPackage` row, calls
execution's `/drivers/{id}/config-schema?sha256=...&filename=...&connection_type=...`
with `X-Internal-Token`, returns the schema (or the registry fallback when
`has_schema` is false). Admin-readable for UI; the validation path calls the same
internal helper, not the HTTP route.

Validation wiring with fallback. Add
`validate_device_config_with_schema(connection_type, config, *, schema=None,
role=None)` to `herd_common/device_config.py` that validates against `schema` when
provided, else delegates to today's `validate_device_config`. Inventory's
`create_config_version` (`device_configs.py:205-208`) and `restore_config_version`
resolve the published schema first (via a `_published_schema_for(device)` helper
that proxies execution, with the fallback baked in), then pass it. This keeps
`herd_common` pure (no HTTP) and puts the proxy/fallback orchestration in
inventory where the device and driver rows live.

Caching in inventory. Do NOT add a second persistent cache. Execution already
caches by SHA256 (`DriverCache`), so inventory's proxy is cheap and always
consistent. Add only a short-TTL in-process memo keyed by `(driver_id, sha256)`
to coalesce bursts, invalidated implicitly because the key includes the SHA256.
This avoids a stale-schema correctness bug: if inventory cached by `driver_id`
alone, replacing the driver file would leave a stale schema validating new
configs.

Failure policy. If execution is unreachable or errors when resolving the
published schema, inventory falls back to the registry (`CONFIG_SCHEMAS`) and logs
a warning rather than 503-ing a config write. The registry is a valid,
conservative safety net; a transient execution outage should not block config
authoring. Deliberate fail-open to EXISTING behavior (confirm in Risks; if the
maintainer prefers fail-closed, flip to 503).

## 3. Test plan (HERD standard QA levels)

Unit (in-memory SQLite / no stack), execution service, `services/execution/tests/`:

- `test_config_schema_extraction.py`: (a) valid `config_schema()` returns
  `{has_schema: True, schema}`; (b) no method returns `{has_schema: False}`;
  (c) non-dict / raises returns no usable schema (fail-open); (d) classmethod
  invoked WITHOUT instantiation: fixture whose `__init__` raises (requires
  `context["HERD_ip_address"]`) still extracts; (e) timeout/rlimit kill surfaces
  as no-schema, not a crash. Fixture drivers under
  `services/execution/tests/fixtures/drivers/` (`mock_schema_mgmt/`,
  `mock_no_schema/`, `mock_credential_init/`), matching `mock_ios`
  (`docs/DRIVERS.md:99-106`).
- Extend `test_driver_loader.py`: `load_driver` persists `config_schema_json`;
  SHA256 change re-extracts; `get_driver_config_schema` returns the cached dict
  and the default-shape when null (parallels `get_driver_metadata` tests).
- New `test_config_schema_endpoint.py`: `GET /drivers/{id}/config-schema`
  requires the internal token (403 without), returns `has_schema/schema/source`.

Unit, common + inventory:

- `validate_device_config_with_schema` validates against a passed schema; with
  `schema=None` it is byte-for-byte the old `validate_device_config` (golden
  behavior, including the exact 422 wording at `device_config.py:136-146,151-152`).
- `services/inventory/tests/test_device_configs.py`: the no-`config_schema`
  fallback (mock the proxy to report `has_schema: False`, assert registry used);
  a published-schema accept; a published-schema reject to 422 with role-prefixed
  detail; execution-unreachable to fall back to registry (fail-open).

Contract / integration (needs the stack), `tests/integration/`:

- `test_driver_config_schema_flow.py`: self-seed (session fixtures) an admin, a
  Management driver package that ships `config_schema()`, a template, a device.
  Then: (1) `GET /api/inventory/drivers/{id}/config-schema` returns the published
  schema; (2) a config the published schema accepts succeeds; (3) a config the
  published schema REJECTS but the registry would ACCEPT returns 422 (proves the
  published schema overrides the registry); (4) a driver WITHOUT `config_schema()`
  validates against the registry. Grep existing integration tests for the old
  `create_config_version` 422 wording before changing any status/shape.

E2E (Selenium, `make test-e2e`): admin uploads a driver that publishes a schema,
creates a template+device, opens the config editor, sees validation reflect the
driver-published fields (and a rejected field surfaces the 422 detail). Asserts
on rendered text/casing per the established Selenium patterns.

## 4. Phased, independently-mergeable delivery

1. Slice 1 (contract + extraction, no validation change). Runner
   `__config_schema__` action, sandbox wrapper, fixture drivers,
   `test_config_schema_extraction.py`. Pure addition; nothing calls it yet.
2. Slice 2 (cache + execution endpoint). `config_schema_json` column on
   `DriverCache` (new execution Alembic revision), populate in `load_driver`,
   `get_driver_config_schema` helper, `GET /drivers/{id}/config-schema` internal
   endpoint, its unit test. Still no validation change.
3. Slice 3 (inventory proxy + UI-readable route).
   `validate_device_config_with_schema` in `herd_common`, inventory's
   `_published_schema_for`/proxy helper,
   `GET /api/inventory/drivers/{id}/config-schema`. Validator helper unit-tested;
   route admin-gated.
4. Slice 4 (wire validation with fallback). Switch
   `create_config_version`/`restore_config_version` to published-schema-first with
   registry fallback and fail-open. Inventory unit tests for
   accept/reject/fallback/unreachable. First behavioral slice; ships with the
   integration test.
5. Slice 5 (docs + e2e). `docs/DRIVERS.md` section, update the B8 note
   (`:611-628`), integration + Selenium tests, a sample driver shipping
   `config_schema()`.
6. Slice 6 (optional follow-up). Adopt the published schema in the AI
   orchestrator's `get_device_config_schema` tool (`tools.py:444-503`).
   Separable; not required for #23's core.

Slices 1-3 are non-behavioral and safe to merge ahead of 4.

## 5. Open risks / decisions to confirm

- Fail-open vs fail-closed on execution unreachable (2.3): proposal is fail-open
  to the registry. If config writes should hard-fail (503) when the published
  schema cannot be resolved, it is a one-line policy flip.
- `jsonschema` dependency on execution (1.5): to lint the extracted schema with
  `Draft202012Validator.check_schema` inside execution, add `jsonschema` to
  `services/execution/pyproject.toml`. Alternative: lint in inventory only
  (already has `jsonschema`), falling back to the registry on a malformed
  published schema. Leaning toward inventory-only to avoid the dep add.
- JSON Schema draft / `$ref` policy: pin to draft 2020-12 and disallow remote
  `$ref`. A driver could point a `$ref` at an internal URL and the validator
  would try to fetch it (SSRF-shaped). Strip/forbid `$id`/`$ref` with non-local
  URIs, or use a no-network resolver. Confirm the constraint.
- Schema authority for L1/L2 (no `configure`): published schemas only make sense
  for connection types that accept a `configure` action (Management today; L3
  future). For L1/L2 a published `config_schema()` is meaningless; ignore it for
  those connection types or document it as Management/L3 only.
- Where the override applies: this design wires the published schema into the
  inventory config-version write path (`device_configs.py:205`) and, in slice 6,
  the AI tool. The AI COMMIT validation in `committer.py:229` should also honor it
  eventually; confirm whether slice 4 must cover the commit path or whether that
  can trail.
