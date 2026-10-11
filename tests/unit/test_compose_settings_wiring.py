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
        it through: the value under the container-side key interpolates the
        field's OWN operator-facing variable, `${FIELD_NAME...}`, or, for
        the five fields _RENAMED_WIRING lists, the differently named
        variable(s) recorded there (anywhere on the right-hand side; the
        container-side key is always the field name upper-cased,
        HerdBaseSettings/pydantic-settings default env-var naming;
        test_no_settings_field_declares_an_alias below verifies no field
        overrides that with an alias), or
    (b) it is listed in _EXEMPTIONS below with a one-line reason.

A value that interpolates some OTHER variable is not wired (issue #1158):
`AI_GENERATE_MAX_REPAIRS: ${AI_RESOLVER_MAX_SEARCH_STEPS:-5000}` passes a
value through, but not the one an operator sets under the documented name.
Neither is an escaped `$${VAR}` (compose hands the container the literal
text) or a `${VAR:+text}` (compose inserts the text, never the value).

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

import pytest
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


# Fields that reach the container through a differently named operator-facing
# variable, by design (docs/ENV_VARS.md: the AUTH_SECRET_KEY, AUTH_ALGORITHM and
# AUTH_*_EXPIRE_* rows, and "Database URLs (auto-computed)"). Every other field
# must be wired through its own name upper-cased. A field listed here must
# interpolate EVERY variable named for it; test_no_stale_renamed_wiring fails on
# an entry no service uses any more.
_RENAMED_WIRING: dict[str, frozenset[str]] = {
    "database_url": frozenset({"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}),
    "secret_key": frozenset({"AUTH_SECRET_KEY"}),
    "algorithm": frozenset({"AUTH_ALGORITHM"}),
    "access_token_expire_minutes": frozenset({"AUTH_ACCESS_TOKEN_EXPIRE_MINUTES"}),
    "refresh_token_expire_days": frozenset({"AUTH_REFRESH_TOKEN_EXPIRE_DAYS"}),
}

# `${NAME`, then what follows the name; or a bare `$NAME`.
_BRACED_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+])?")
_BARE_INTERPOLATION = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def interpolated_variables(value: object) -> set[str]:
    """The host variables whose VALUE compose substitutes into a compose value.

    Counts `${VAR}`, `${VAR:-default}`, `${VAR-default}`, `${VAR:?error}`,
    `${VAR?error}`, and a bare `$VAR`, anywhere in the string, including inside
    another substitution's default. Does not count `$${VAR}` (compose's escape:
    the container receives the literal text) or `${VAR:+text}` and `${VAR+text}`
    (compose inserts `text` when VAR is set, never VAR's value). A value that is
    not a string interpolates nothing."""
    if not isinstance(value, str):
        return set()
    text = value.replace("$$", "\0")
    names = {
        match.group(1)
        for match in _BRACED_INTERPOLATION.finditer(text)
        if not (match.group(2) or "").endswith("+")
    }
    return names | set(_BARE_INTERPOLATION.findall(text))


def operator_variables(field_name: str) -> frozenset[str]:
    """The .env variable(s) an operator sets to reach this Settings field."""
    return _RENAMED_WIRING.get(field_name, frozenset({field_name.upper()}))


def _is_wired(environment: dict, field_name: str) -> bool:
    """True when the compose key for this field (the field name upper-cased)
    is present and its value interpolates the field's operator-facing
    variable(s): its own name, or every variable _RENAMED_WIRING records for
    it (SECRET_KEY's value is `${AUTH_SECRET_KEY}`; DATABASE_URL's interpolates
    `${POSTGRES_USER}`, `${POSTGRES_PASSWORD}`, and `${POSTGRES_DB}`).

    WHICH variable matters (issue #1158): a value that interpolates some other
    variable, a pure literal (e.g. "http://inventory:8000" or "auth"), an
    escaped `$${VAR}`, or a `${VAR:+text}` cannot carry what the operator set
    under the documented name into the container.
    """
    value = environment.get(field_name.upper())
    return isinstance(value, str) and operator_variables(field_name) <= interpolated_variables(
        value
    )


def _wired_default(environment: dict, field_name: str) -> str | None:
    """The literal default from a ${VAR:-default} pattern, where VAR is the
    field's one operator-facing variable (its own name, or the single renamed
    variable _RENAMED_WIRING records), or None if the field isn't wired with a
    fallback (wired with no fallback, e.g. `${VAR}`, or through several
    variables, as DATABASE_URL is).

    Scans by brace-depth rather than a single non-greedy regex
    (`[^}]*` stopping at the first `}`), because at least one default
    contains a literal, balanced `{...}` of its own: auth's
    LDAP_USER_FILTER default is `(sAMAccountName={username})`, so
    `${LDAP_USER_FILTER:-(sAMAccountName={username})}` has two closing
    braces before the one that actually ends the substitution.
    """
    value = environment.get(field_name.upper())
    variables = operator_variables(field_name)
    if not isinstance(value, str) or len(variables) != 1:
        return None
    prefix = f"${{{next(iter(variables))}:-"
    start = value.replace("$$", "\0\0").find(prefix)
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
        "cabling_service_url": _SERVICE_URL_REASON,
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
                f"{service}.{field_name}: docker-compose.yml's environment block "
                f"has no {field_name.upper()} value that interpolates "
                f"{', '.join(sorted(operator_variables(field_name)))}, and the field "
                "is not in _EXEMPTIONS, so setting it in .env has no effect in "
                "the container"
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


def test_no_stale_renamed_wiring():
    """A _RENAMED_WIRING entry is an allowance to be wired through another
    variable name; one that no service's Settings model uses any more must be
    deleted rather than left to excuse a future field of the same name."""
    in_use = {field for service in SERVICE_NAMES for field in _load_settings_fields(service)}
    stale = sorted(set(_RENAMED_WIRING) - in_use)
    assert not stale, f"renamed-wiring entries no Settings model declares: {stale}"


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
    """Where docker-compose.yml carries a ${VAR:-default} fallback for the
    field's one operator-facing variable (its own name, or the renamed one,
    as ALGORITHM carries ${AUTH_ALGORITHM:-HS256}; not a no-fallback
    passthrough like INTERNAL_API_TOKEN's ${INTERNAL_API_TOKEN}), that default
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


def keys_not_passed_through(keys: set[str], environment: dict) -> list[str]:
    """The keys an operator can set in .env that no value of this environment
    block interpolates. A container-side key of the same name with a literal
    value does not pass the .env value through."""
    reaching = set().union(*(interpolated_variables(value) for value in environment.values()))
    return sorted(keys - reaching)


def test_every_active_ai_env_example_key_is_wired_to_ai_orchestrator():
    active_keys = _active_env_example_keys("AI_")
    missing = keys_not_passed_through(active_keys, _service_environment("ai-orchestrator"))
    assert not missing, (
        "these AI_* keys are active in .env.example but no value in the "
        "ai-orchestrator environment block in docker-compose.yml interpolates "
        "them, so setting them in .env has no effect in the container: " + ", ".join(missing)
    )


# --- the wiring predicate itself (issue #1158): every way a compose line can
# look wired without carrying the operator's value into the container.

_FIELD = "ai_generate_max_repairs"
_KEY = "AI_GENERATE_MAX_REPAIRS"


def _environment(yaml_lines: str) -> dict:
    return yaml.safe_load("environment:\n" + yaml_lines)["environment"]


@pytest.mark.parametrize(
    "value",
    [
        "${AI_GENERATE_MAX_REPAIRS:-2}",
        "${AI_GENERATE_MAX_REPAIRS}",
        "${AI_GENERATE_MAX_REPAIRS-2}",
        "${AI_GENERATE_MAX_REPAIRS:?set it}",
        "$AI_GENERATE_MAX_REPAIRS",
        "prefix-${AI_GENERATE_MAX_REPAIRS:-2}-suffix",
        "${OTHER:-${AI_GENERATE_MAX_REPAIRS}}",
        "$${literal}-${AI_GENERATE_MAX_REPAIRS}",
    ],
)
def test_wiring_predicate_accepts_the_fields_own_variable(value: str):
    assert _is_wired({_KEY: value}, _FIELD)


def test_wiring_predicate_accepts_the_recorded_renames():
    environment = _environment(
        "  SECRET_KEY: ${AUTH_SECRET_KEY}\n"
        "  ALGORITHM: ${AUTH_ALGORITHM:-HS256}\n"
        "  DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}"
        "@postgres:5432/${POSTGRES_DB}\n"
    )
    assert _is_wired(environment, "secret_key")
    assert _is_wired(environment, "algorithm")
    assert _is_wired(environment, "database_url")
    assert _wired_default(environment, "algorithm") == "HS256"
    assert _wired_default(environment, "secret_key") is None


NOT_WIRED = {
    "another variable": "  AI_GENERATE_MAX_REPAIRS: ${AI_RESOLVER_MAX_SEARCH_STEPS:-5000}\n",
    "a longer variable name ending in the field's": (
        "  AI_GENERATE_MAX_REPAIRS: ${OLD_AI_GENERATE_MAX_REPAIRS:-2}\n"
    ),
    "a longer variable name starting with the field's": (
        "  AI_GENERATE_MAX_REPAIRS: ${AI_GENERATE_MAX_REPAIRS_V2:-2}\n"
    ),
    "an escaped dollar": "  AI_GENERATE_MAX_REPAIRS: $${AI_GENERATE_MAX_REPAIRS:-2}\n",
    "the alternate-value form": "  AI_GENERATE_MAX_REPAIRS: ${AI_GENERATE_MAX_REPAIRS:+2}\n",
    "a literal": "  AI_GENERATE_MAX_REPAIRS: 2\n",
    "a literal string": '  AI_GENERATE_MAX_REPAIRS: "2"\n',
    "the variable only in a YAML comment": (
        "  AI_GENERATE_MAX_REPAIRS: 2  # ${AI_GENERATE_MAX_REPAIRS:-2}\n"
    ),
    "the variable under another key": ("  AI_GENERATE_MAX_REPAIR: ${AI_GENERATE_MAX_REPAIRS:-2}\n"),
    "the name without a dollar": "  AI_GENERATE_MAX_REPAIRS: AI_GENERATE_MAX_REPAIRS\n",
    "an empty value": "  AI_GENERATE_MAX_REPAIRS:\n",
}


@pytest.mark.parametrize("spelling", sorted(NOT_WIRED))
def test_wiring_predicate_refuses_a_line_that_only_looks_wired(spelling: str):
    assert not _is_wired(_environment(NOT_WIRED[spelling]), _FIELD), spelling


def test_wiring_predicate_refuses_a_renamed_field_missing_one_of_its_variables():
    environment = _environment(
        "  SECRET_KEY: ${SOME_OTHER_SECRET}\n"
        "  DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER}:herd@postgres:5432/${POSTGRES_DB}\n"
    )
    assert not _is_wired(environment, "secret_key")
    assert not _is_wired(environment, "database_url")


def test_wired_default_ignores_an_escaped_substitution():
    assert _wired_default({_KEY: "$${AI_GENERATE_MAX_REPAIRS:-2}"}, _FIELD) is None
    assert _wired_default({_KEY: "${AI_GENERATE_MAX_REPAIRS:-2}"}, _FIELD) == "2"


def test_env_example_scan_needs_an_interpolation_not_a_key():
    keys = {"AI_MODEL", "AI_PROVIDER"}
    wired = _environment("  AI_MODEL: ${AI_MODEL:-m}\n  AI_PROVIDER: ${AI_PROVIDER:-p}\n")
    assert keys_not_passed_through(keys, wired) == []
    literal = _environment("  AI_MODEL: some-model\n  AI_PROVIDER: ${AI_PROVIDER:-p}\n")
    assert keys_not_passed_through(keys, literal) == ["AI_MODEL"]
    crossed = _environment("  AI_MODEL: ${AI_PROVIDER:-p}\n  AI_PROVIDER: ${AI_PROVIDER:-p}\n")
    assert keys_not_passed_through(keys, crossed) == ["AI_MODEL"]
