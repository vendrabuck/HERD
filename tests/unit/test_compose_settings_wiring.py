"""Static pin: every operator-settable Settings field of every HERD service is
either passed through by docker-compose.yml or carries an explicit, per-field
exemption (issue #873).

No stack, no Docker: this only introspects each service's `app.config.Settings`
model (via a subprocess, see `_load_settings_fields` below) and parses
docker-compose.yml and .env.example as text/YAML.

Background: issue #849 found that AI_GENERATE_MAX_REPAIRS and two
AI_RESOLVER_* knobs were documented and defaulted in ai-orchestrator's Settings
model but had no line in docker-compose.yml's ai-orchestrator environment
block, so setting them in .env silently did nothing. The fix (#851) added
test_compose_ai_env_wiring.py, but that test only checked keys that were
ACTIVE in .env.example AND started with "AI_", so issue #873 found ten more
ai-orchestrator settings with the identical defect (not in .env.example, or a
different prefix like ASSISTANT_/UPLOAD_) that the narrow test could not see.

This test generalizes the invariant to every service and every Settings
field, not just ai-orchestrator's AI_/ASSISTANT_/UPLOAD_ knobs:

    For every Settings field of every HERD service, either
    (a) docker-compose.yml's base environment block for that service passes
        it through via an explicit ${VAR...} reference (anywhere on the
        right-hand side; the container-side key is always the field name
        upper-cased, HerdBaseSettings/pydantic-settings default env-var
        naming; test_no_settings_field_declares_an_alias below verifies no
        field overrides that with an alias), or
    (b) it is listed in _EXEMPTIONS below with a one-line reason.

Compose passes an env var to a service only through an explicit ${VAR} line
in its `environment:` block; no service uses `env_file`. docker-compose.yml
is the base file: this test deliberately does NOT look at
docker-compose.override.yml, because a var set there with a literal dev
value (never `${VAR}`) is still not operator-settable under `make prod`,
which excludes the override file. A stale exemption (a field that no longer
exists, or that has since been wired) fails loudly so this list cannot rot
the way the narrow #851 test did.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
SERVICES_DIR = REPO_ROOT / "services"


def _discover_service_names() -> list[str]:
    """Every services/<name>/app/config.py on disk, by structure rather than
    a hardcoded name list: a new service that follows this repo's Settings
    convention (every service's Settings model subclasses
    herd_common.base_settings.HerdBaseSettings and lives at app/config.py)
    is picked up automatically. The config service is excluded because it
    has no app/config.py at all (it is stateless, see docs/ENV_VARS.md
    "Config service"), not because its name is filtered out anywhere here;
    test_service_names_matches_discovered_services below turns that fact
    into an assertion instead of a silent assumption.
    """
    return sorted(path.parent.parent.name for path in SERVICES_DIR.glob("*/app/config.py"))


# Kept as a literal, ordered list (roughly service-dependency order) for
# stable, readable failure output across the test functions below, rather
# than iterating _discover_service_names() directly everywhere.
# test_service_names_matches_discovered_services asserts this stays in sync
# with what is actually on disk.
SERVICE_NAMES = [
    "auth",
    "inventory",
    "reservations",
    "cabling",
    "acl",
    "execution",
    "ai-orchestrator",
    "user-profile",
    "notifications",
    "integration",
    "secrets",
]

# Every DB-backed service's Settings model requires database_url and
# secret_key with no default (ai-orchestrator is the one exception: both have
# defaults there, a dev sqlite URL and a dev secret). Every config.py
# instantiates `settings = Settings()` at import time, so importing it without
# these set raises a pydantic ValidationError before we ever see
# model_fields. The values are never used for anything but introspecting
# field metadata (no DB connection or auth is attempted), so any
# syntactically valid placeholder works.
_DUMMY_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://dummy:dummy@dummy-host:5432/dummy",
    "SECRET_KEY": "dummy-test-secret-key-not-real",
}

_INTROSPECT_SCRIPT = """
import json
from app.config import Settings
from pydantic_core import PydanticUndefined

data = {}
for name, field in Settings.model_fields.items():
    required = field.default is PydanticUndefined and field.default_factory is None
    if required:
        default = None
    elif field.default_factory is not None:
        default = field.default_factory()
    else:
        default = field.default
    data[name] = {
        "required": required,
        "default": default,
        "alias": field.alias,
        "validation_alias": field.validation_alias,
    }
print(json.dumps(data, default=str))
"""


@lru_cache(maxsize=None)
def _load_settings_fields(service: str) -> dict:
    """Return {field_name: {"required", "default", "alias",
    "validation_alias"}} for a service's Settings model.

    Import strategy: every service's config.py lives at `app/config.py` under
    the SAME top-level package name `app` (services/auth/app,
    services/inventory/app, ...). Importing two services' `app.config` in one
    process collides on `sys.modules["app"]`: the second import silently
    reuses the first service's already-cached `app` package rather than
    loading the second service's code, so a single in-process loop over all
    11 services would only ever see the first one's fields. A subprocess per
    service sidesteps this cleanly (fresh sys.modules, fresh cwd) and was
    chosen over two other approaches:

    - importlib with a synthetic unique module name per service would still
      leave `app.config`'s own imports (herd_common, pydantic_settings, ...)
      resolving against whatever `app` package happens to be
      sys.modules-cached at that moment, since those imports are absolute
      (`from herd_common...`) and not relative to the synthetic name; it does
      not actually solve the collision, only relocate it.
    - static AST parsing of config.py avoids the process-isolation problem
      entirely, but several fields default to a computed expression rather
      than a literal (`5 * 1024 * 1024`, `7 * 24 * 3600`,
      `256 * 1024 * 1024`, ...), which `ast.literal_eval` cannot evaluate
      (it accepts literals and a narrow set of container/unary-operator
      forms, not general BinOp arithmetic) without a hand-rolled safe
      evaluator that is itself one more thing to keep in sync with
      config.py's grammar.

    Uses `sys.executable` directly (the interpreter already running this
    test, i.e. the workspace venv that `uv sync --all-extras` populated with
    every service's editable install), not `uv run`: shelling out to `uv`
    from inside a test can trigger a sync or install and needs `uv` on
    PATH, neither of which this introspection needs once the workspace venv
    already has every service installed. `-c` gives the child cwd as
    sys.path[0] (an empty string, resolved against the process's actual
    working directory), so `cwd=services/<svc>` is enough for `app.config`
    to resolve to that service's own code.
    """
    service_dir = SERVICES_DIR / service
    env = dict(os.environ)
    env.update(_DUMMY_ENV)
    result = subprocess.run(
        [sys.executable, "-c", _INTROSPECT_SCRIPT],
        cwd=service_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"failed to introspect {service}'s Settings model via "
        f"`{sys.executable} -c ...` in {service_dir}:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout.strip())


@lru_cache(maxsize=None)
def _compose_data() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def _service_environment(service: str) -> dict:
    data = _compose_data()
    block = data["services"][service]
    environment = block.get("environment") or {}
    assert isinstance(environment, dict), (
        f"{service}'s environment block in docker-compose.yml is not the dict "
        "(KEY: value) form this test expects; if it changed to the list "
        "(KEY=value) form, update this test"
    )
    return environment


def _is_wired(environment: dict, field_name: str) -> bool:
    """True when the compose key for this field (the field name
    upper-cased) is present and its value contains at least one explicit
    ${VAR...} interpolation.

    Deliberately loose about WHICH var is referenced, not just the field's
    own name: several fields are wired through a differently-named
    operator-facing var by design (SECRET_KEY's value is `${AUTH_SECRET_KEY}`,
    not `${SECRET_KEY}`; DATABASE_URL's value interpolates
    `${POSTGRES_USER}`/`${POSTGRES_PASSWORD}`/`${POSTGRES_DB}`, not
    `${DATABASE_URL}`, see docs/ENV_VARS.md "Database URLs (auto-computed)"
    and the AUTH_SECRET_KEY row in the Required table). What matters for the
    invariant is only whether SOME .env value can reach this field at
    container-creation time; a pure literal (e.g. "http://inventory:8000" or
    "auth") can never do that regardless of which var it might have been
    named after.
    """
    value = environment.get(field_name.upper())
    return isinstance(value, str) and "${" in value


def _wired_default(environment: dict, field_name: str) -> str | None:
    """The literal default from a ${FIELD_NAME:-default} pattern, or None if
    the field isn't wired through its OWN name with a fallback (wired
    through a renamed var, or wired with no fallback, e.g. `${VAR}`).

    Scans by brace-depth rather than a single non-greedy regex
    (`[^}]*` stopping at the first `}`), because at least one default
    contains a literal, balanced `{...}` of its own: auth's
    LDAP_USER_FILTER default is `(sAMAccountName={username})`, so
    `${LDAP_USER_FILTER:-(sAMAccountName={username})}` has two closing
    braces before the one that actually ends the substitution.
    """
    value = environment.get(field_name.upper())
    if not isinstance(value, str):
        return None
    prefix = f"${{{field_name.upper()}:-"
    start = value.find(prefix)
    if start == -1:
        return None
    i = start + len(prefix)
    depth = 1  # the ${ that opened this substitution is already open
    result_start = i
    while i < len(value) and depth > 0:
        if value[i] == "{":
            depth += 1
        elif value[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return None
    return value[result_start:i]


def _defaults_match(compose_default: str, settings_default: object) -> bool:
    if isinstance(settings_default, bool):
        return compose_default.strip().lower() == str(settings_default).lower()
    if isinstance(settings_default, int):
        try:
            return int(compose_default) == settings_default
        except ValueError:
            return False
    if isinstance(settings_default, float):
        try:
            return float(compose_default) == settings_default
        except ValueError:
            return False
    if isinstance(settings_default, list):
        # e.g. reservations' PURPOSE_CATEGORIES: a comma-separated string in
        # compose expands to the field's list default.
        parts = [item.strip() for item in compose_default.split(",") if item.strip()]
        return parts == settings_default
    return compose_default == settings_default


# Per-service, per-field exemptions. Each entry needs a one-line reason; the
# stale-exemption tests below fail if a listed field no longer exists on the
# Settings model, or if it has since become properly wired (in which case the
# exemption should simply be deleted).
#
# Two shapes recur across almost every service and are captured once instead
# of restated per field:
#
# - `db_schema`: every service pins DB_SCHEMA to a literal in
#   docker-compose.yml (e.g. `DB_SCHEMA: auth`). docs/ENV_VARS.md ("Database
#   URLs (auto-computed)") documents this as deliberate: changing a service's
#   schema means editing the matching CREATE SCHEMA in
#   infra/postgres/init.sql, which only runs on a fresh Postgres volume, so
#   it is explicitly "not a knob to turn on a live stack".
# - `*_service_url`: every inter-service URL field's in-network default
#   (e.g. `http://inventory:8000`) is correct for the standard compose
#   topology. These are either hardcoded compose literals or absent
#   entirely; docs/ENV_VARS.md's "Service URLs" section documents them as
#   overridable, but that override path is the config UI's config.json layer
#   (which outranks the environment regardless of compose wiring), not a
#   compose-level ${VAR} gap of the #849/#873 shape.
_DB_SCHEMA_REASON = (
    "DB_SCHEMA is pinned to a literal per service in docker-compose.yml by "
    "design (docs/ENV_VARS.md 'Database URLs (auto-computed)'): changing it "
    "means editing infra/postgres/init.sql's CREATE SCHEMA, which only runs "
    "on a fresh Postgres volume, so it is explicitly not a live-stack knob."
)
_SERVICE_URL_REASON = (
    "in-network default is correct for the standard compose topology; a "
    "*_SERVICE_URL knob is not meant to be operator-tuned via .env under "
    "compose (an off-topology deployment goes through the config UI's "
    "config.json layer instead, which outranks the environment regardless "
    "of compose wiring)."
)

_EXEMPTIONS: dict[str, dict[str, str]] = {
    "auth": {
        "db_schema": _DB_SCHEMA_REASON,
    },
    "inventory": {
        "db_schema": _DB_SCHEMA_REASON,
        "auth_service_url": _SERVICE_URL_REASON,
        "execution_service_url": _SERVICE_URL_REASON,
        "reservations_service_url": _SERVICE_URL_REASON,
        "acl_service_url": _SERVICE_URL_REASON,
        "secrets_service_url": _SERVICE_URL_REASON,
    },
    "reservations": {
        "db_schema": _DB_SCHEMA_REASON,
        "inventory_service_url": _SERVICE_URL_REASON,
        "auth_service_url": _SERVICE_URL_REASON,
        "ai_orchestrator_service_url": _SERVICE_URL_REASON,
        "cabling_service_url": _SERVICE_URL_REASON,
        "execution_service_url": _SERVICE_URL_REASON,
    },
    "cabling": {
        "db_schema": _DB_SCHEMA_REASON,
        "reservations_service_url": _SERVICE_URL_REASON,
        "inventory_service_url": _SERVICE_URL_REASON,
    },
    "acl": {
        "db_schema": _DB_SCHEMA_REASON,
        "auth_service_url": _SERVICE_URL_REASON,
    },
    "execution": {
        "db_schema": _DB_SCHEMA_REASON,
        "inventory_service_url": _SERVICE_URL_REASON,
        "cabling_service_url": _SERVICE_URL_REASON,
        "acl_service_url": _SERVICE_URL_REASON,
        "reservations_service_url": _SERVICE_URL_REASON,
        "secrets_service_url": _SERVICE_URL_REASON,
    },
    "ai-orchestrator": {
        "db_schema": _DB_SCHEMA_REASON,
        "inventory_service_url": _SERVICE_URL_REASON,
        "cabling_service_url": _SERVICE_URL_REASON,
        "reservations_service_url": _SERVICE_URL_REASON,
        "execution_service_url": _SERVICE_URL_REASON,
    },
    "user-profile": {
        "db_schema": _DB_SCHEMA_REASON,
    },
    "notifications": {
        "db_schema": _DB_SCHEMA_REASON,
        "user_profile_service_url": _SERVICE_URL_REASON,
        "auth_service_url": _SERVICE_URL_REASON,
        "reservations_service_url": _SERVICE_URL_REASON,
    },
    "integration": {
        "db_schema": _DB_SCHEMA_REASON,
        "reservations_service_url": _SERVICE_URL_REASON,
    },
    "secrets": {
        "db_schema": _DB_SCHEMA_REASON,
        "acl_service_url": _SERVICE_URL_REASON,
        "inventory_service_url": _SERVICE_URL_REASON,
    },
}

# Fields that are correctly wired (docker-compose.yml passes them through by
# their own name), but whose compose ${VAR:-default} is DELIBERATELY not the
# Settings field's own default. cors_origins is the one case today:
# docs/ENV_VARS.md's "Web / CORS / TLS" section documents it explicitly:
# every service's Settings model defaults cors_origins to "" (so the
# in-process unit tests, which never see docker-compose.yml, get no CORS
# middleware origins), while docker-compose.yml supplies
# ${CORS_ORIGINS:-https://localhost} for every service so a real stack has a
# usable out-of-the-box origin. This is not the #849/#873 bug (a value that
# never reaches the container); it is a deliberately different fallback for
# two different run contexts.
_CORS_ORIGINS_MISMATCH_REASON = (
    "docs/ENV_VARS.md 'Web / CORS / TLS': the Settings default is \"\" (so "
    "in-process unit tests get no CORS origins) while docker-compose.yml "
    "deliberately supplies https://localhost as the container fallback; a "
    "documented, intentional divergence, not a wiring gap."
)
_DEFAULT_MISMATCH_EXEMPTIONS: dict[str, dict[str, str]] = {
    service: {"cors_origins": _CORS_ORIGINS_MISMATCH_REASON} for service in SERVICE_NAMES
}


def test_service_names_matches_discovered_services():
    """SERVICE_NAMES is a literal list (for stable, readable failure output
    in the other tests here), not the source of truth; this test is that
    source of truth's check. A new services/<name>/app/config.py (or a
    removed one) must fail here until SERVICE_NAMES is updated to match,
    rather than silently going unchecked or crashing on a missing
    _EXEMPTIONS/_DEFAULT_MISMATCH_EXEMPTIONS entry elsewhere."""
    discovered = _discover_service_names()
    assert discovered, f"no services/*/app/config.py found under {SERVICES_DIR}"
    assert sorted(SERVICE_NAMES) == discovered, (
        "SERVICE_NAMES has drifted from the services/*/app/config.py files on "
        f"disk: discovered={discovered!r} SERVICE_NAMES={sorted(SERVICE_NAMES)!r}"
    )
    assert "config" not in discovered, (
        "the config service now has an app/config.py (a Settings model), but "
        "this test suite still assumes it is stateless and has none; add it "
        "to SERVICE_NAMES and give it an _EXEMPTIONS entry if it needs one"
    )


def test_no_settings_field_declares_an_alias():
    """_is_wired (and every other check in this file) assumes the
    container-side env var name is always the field name upper-cased, the
    pydantic-settings default derivation with no alias in play. If a
    service ever adds `Field(alias=...)` or a `validation_alias`, that
    assumption silently breaks the wiring check for that one field without
    anything here noticing; fail loudly instead so the derivation gets
    updated deliberately."""
    failures = []
    for service in SERVICE_NAMES:
        fields = _load_settings_fields(service)
        for field_name, meta in fields.items():
            if meta.get("alias") or meta.get("validation_alias"):
                failures.append(
                    f"{service}.{field_name}: alias={meta.get('alias')!r} "
                    f"validation_alias={meta.get('validation_alias')!r}; this "
                    "test's env-var-name derivation (field name upper-cased) "
                    "needs updating to honor it"
                )
    assert not failures, "fields declaring an alias:\n" + "\n".join(sorted(failures))


def test_every_settings_field_is_wired_or_exempt():
    failures = []
    for service in SERVICE_NAMES:
        fields = _load_settings_fields(service)
        environment = _service_environment(service)
        exemptions = _EXEMPTIONS.get(service, {})
        for field_name in fields:
            if field_name in exemptions:
                continue
            if _is_wired(environment, field_name):
                continue
            failures.append(
                f"{service}.{field_name} (env var {field_name.upper()}): missing "
                "from docker-compose.yml's environment block and not in "
                "_EXEMPTIONS, so setting it in .env has no effect in the "
                "container"
            )
    assert not failures, "unwired, unexempted Settings fields:\n" + "\n".join(sorted(failures))


def test_no_stale_exemptions():
    """An exemption naming a field that no longer exists, or that has since
    been wired through docker-compose.yml, must be deleted rather than left
    to rot (the #851 test's narrow scope is exactly how issue #873's ten
    fields went unnoticed)."""
    failures = []
    for service, exemptions in _EXEMPTIONS.items():
        fields = _load_settings_fields(service)
        environment = _service_environment(service)
        for field_name in exemptions:
            if field_name not in fields:
                failures.append(
                    f"{service}.{field_name}: exempted but no longer a Settings "
                    "field; remove the stale exemption"
                )
            elif _is_wired(environment, field_name):
                failures.append(
                    f"{service}.{field_name}: exempted but IS wired in "
                    "docker-compose.yml; remove the stale exemption"
                )
    assert not failures, "stale exemptions:\n" + "\n".join(sorted(failures))


def test_no_stale_default_mismatch_exemptions():
    """Same staleness discipline for _DEFAULT_MISMATCH_EXEMPTIONS: a field
    that no longer exists, or whose compose default now agrees with the
    Settings default, should not still carry an exemption."""
    failures = []
    for service, exemptions in _DEFAULT_MISMATCH_EXEMPTIONS.items():
        fields = _load_settings_fields(service)
        environment = _service_environment(service)
        for field_name in exemptions:
            if field_name not in fields:
                failures.append(
                    f"{service}.{field_name}: default-mismatch exemption but no "
                    "longer a Settings field; remove the stale exemption"
                )
                continue
            compose_default = _wired_default(environment, field_name)
            if compose_default is not None and _defaults_match(
                compose_default, fields[field_name]["default"]
            ):
                failures.append(
                    f"{service}.{field_name}: default-mismatch exemption but "
                    "compose and Settings defaults now agree; remove the stale "
                    "exemption"
                )
    assert not failures, "stale default-mismatch exemptions:\n" + "\n".join(sorted(failures))


def test_wired_compose_defaults_match_settings_defaults():
    """Where docker-compose.yml carries a ${FIELD_NAME:-default} fallback
    (the field is wired through its OWN name with a literal default, not a
    renamed var like SECRET_KEY<-AUTH_SECRET_KEY and not a no-fallback
    passthrough like INTERNAL_API_TOKEN<-${INTERNAL_API_TOKEN}), that default
    must equal the Settings field's own default, or a fresh clone (no .env
    value set) silently diverges from what the Settings model documents."""
    failures = []
    for service in SERVICE_NAMES:
        fields = _load_settings_fields(service)
        environment = _service_environment(service)
        exemptions = _DEFAULT_MISMATCH_EXEMPTIONS.get(service, {})
        for field_name, meta in fields.items():
            if meta["required"] or field_name in exemptions:
                continue
            compose_default = _wired_default(environment, field_name)
            if compose_default is None:
                continue
            if not _defaults_match(compose_default, meta["default"]):
                failures.append(
                    f"{service}.{field_name}: compose default={compose_default!r} "
                    f"settings default={meta['default']!r}"
                )
    assert not failures, "default mismatches:\n" + "\n".join(sorted(failures))


# --- issue #849's original check, preserved: every active AI_* key in
# .env.example must reach the ai-orchestrator container. This is a narrower,
# independent guard (keyed off .env.example rather than the Settings model)
# and stays useful even after the generalized checks above, since a key can
# be added to .env.example for a field that already exists and is wired
# elsewhere without ever being exercised by the model-driven tests (e.g. a
# documentation-only alias).

_ACTIVE_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def _active_env_example_keys(prefix: str) -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE_PATH.read_text().splitlines():
        match = _ACTIVE_ENV_KEY_RE.match(line)
        if match and match.group(1).startswith(prefix):
            keys.add(match.group(1))
    return keys


def test_every_active_ai_env_example_key_is_wired_to_ai_orchestrator():
    active_keys = _active_env_example_keys("AI_")
    wired_keys = set(_service_environment("ai-orchestrator"))

    missing = sorted(active_keys - wired_keys)
    assert not missing, (
        "these AI_* keys are active in .env.example but missing from the "
        "ai-orchestrator environment block in docker-compose.yml, so setting "
        "them in .env has no effect in the container: " + ", ".join(missing)
    )
