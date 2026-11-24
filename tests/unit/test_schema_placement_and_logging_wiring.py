"""Each database service's model tables sit in the schema it is deployed with, and
each service but config installs the JSON log handler at `LOG_LEVEL` when its
`app.main` is imported (issue #1527).

Background. Every service's unit suite pins `DB_SCHEMA=''` (SQLite has no schemas),
where `settings.db_schema or None` and `settings.db_schema and None` both give no
schema, so a model that drops its schema passed every suite while, under the
deployed `DB_SCHEMA`, its table would be declared bare and land wherever Postgres's
database-wide `search_path` resolves the name. Each suite also imports `app.main`
once, so none can see a `setup_logging` call that is missing.

What is checked. The services are the directories under `services/` with an
`app/main.py` (today twelve); the database services are those with an
`app/database.py` (today eleven, all but config). Each service's `app.main` is
imported in a subprocess of its own (every service's package is named `app`; the
reason is spelled out in `test_compose_settings_wiring._load_settings_fields`), with
`LOG_LEVEL=WARNING` (a value no default has), no config file, a `DATABASE_URL`
nothing connects to (the lifespan is not run), and `DB_SCHEMA` exactly as the base
`docker-compose.yml` passes it to the service of the same name (all eleven pass a
literal today), or unset where it passes none. After `app.main` and every module
under `app.models` (every database service has that package today) are imported:

- the schema the process ends up with (`settings.db_schema`) is not empty and is one
  of the schemas `infra/postgres/init.sql` creates
  (`docs/specs/CONVENTIONS.md`: each service owns its own Postgres schema);
- every table on `app.database.Base.metadata` has that schema, every foreign key of
  those tables targets a table in that schema (no cross-schema foreign keys), and
  every named native enum type of their columns has that schema;
- for every service but config, the root logger is at `LOG_LEVEL` and has exactly
  one handler, whose formatter is `herd_common.logging.JSONFormatter`, and
  `uvicorn.access`, `uvicorn.error`, and `sqlalchemy.engine` are set to WARNING
  (OPS-LOG-8); for config, the root logger has no handler (OPS-LOG-9).

The placement check is the pure function `placement_problems`, tested below on
fixture reports in both directions.

Limits. The first and third were confirmed with temporary probes when this guard was
written: a bare table on `Base` in a module nothing imports, a bare table on a second
`MetaData` under `app.models`, and a sequence in `public` on `Base.metadata` each left
the guard passing, while the same bare table on `Base` under `app.models` failed it.
The others follow from what the guard reads.

- Only `app.database.Base.metadata` after importing `app.main` and the modules
  under `app.models` is read. A table declared on another `MetaData` (a second
  declarative base), or in a module neither of those imports, is not seen.
- Placement is read from SQLAlchemy's metadata, never from Postgres: what
  `create_all` and the Alembic migrations create is not checked here (migrations
  against models is issue #1348), and raw SQL text that names a table
  (`text(...)`, `op.execute`) is not seen.
- Of the schema-bound objects only tables, foreign key targets, and named native
  enum types are checked; a sequence, or a type created only in a migration, is not.
- `DB_SCHEMA` is read from the base `docker-compose.yml` only, the one file that sets
  it today; `docker-compose.override.yml` and `.env` are not read. A value that is not
  a plain literal fails with a message saying to extend the guard.
- The logging state is the one right after the import, with neither the lifespan run
  nor uvicorn's own logging setup applied (for reservations,
  `services/reservations/tests/test_service_wiring_as_deployed.py` applies uvicorn's
  setup first and checks the lines written). This proves the handler's formatter
  class and the levels, not the content of a line, whose rules (OPS-LOG-1 to
  OPS-LOG-7) are pinned elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "services"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
INIT_SQL = REPO_ROOT / "infra" / "postgres" / "init.sql"

PROBE_LOG_LEVEL = "WARNING"
HELD_AT_WARNING = ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine")
JSON_FORMATTER = "herd_common.logging.JSONFormatter"

_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://dummy:dummy@dummy-host:5432/dummy",
    "SECRET_KEY": "dummy-test-secret-key-not-real-0123456789abcdef",
    "HERD_CONFIG_FILE": "/nonexistent/herd-config.json",
    "LOG_LEVEL": PROBE_LOG_LEVEL,
}

_INTROSPECT = """
import importlib
import importlib.util
import json
import logging
import pkgutil

import sqlalchemy as sa

import app.main  # noqa: F401


def qualified(obj):
    return f"{type(obj).__module__}.{type(obj).__qualname__}"


root = logging.getLogger()
report = {
    "root_level": root.level,
    "handlers": [
        [qualified(h), qualified(h.formatter) if h.formatter else None] for h in root.handlers
    ],
    "held": {name: logging.getLogger(name).level for name in %(held)r},
    "database": None,
}
database = None
if importlib.util.find_spec("app.database") is not None:
    database = importlib.import_module("app.database")
if database is not None:
    if importlib.util.find_spec("app.models") is not None:
        models = importlib.import_module("app.models")
        for module in pkgutil.walk_packages(getattr(models, "__path__", []), "app.models."):
            importlib.import_module(module.name)
    from app.config import settings

    tables = database.Base.metadata.tables
    report["database"] = {
        "db_schema": settings.db_schema,
        "tables": [[key, table.schema] for key, table in sorted(tables.items())],
        "foreign_keys": sorted(
            [key, fk.target_fullname] for key, table in tables.items() for fk in table.foreign_keys
        ),
        "enums": sorted(
            [key, column.name, column.type.name, column.type.schema]
            for key, table in tables.items()
            for column in table.columns
            if isinstance(column.type, sa.Enum) and column.type.native_enum and column.type.name
        ),
    }
print(json.dumps(report))
""" % {"held": HELD_AT_WARNING}


def service_names() -> list[str]:
    return sorted(p.parent.parent.name for p in SERVICES_DIR.glob("*/app/main.py"))


def database_service_names() -> list[str]:
    return sorted(p.parent.parent.name for p in SERVICES_DIR.glob("*/app/database.py"))


SERVICES = service_names()
DATABASE_SERVICES = database_service_names()


def init_sql_schemas() -> list[str]:
    return re.findall(r"^CREATE SCHEMA IF NOT EXISTS (\w+);$", INIT_SQL.read_text(), re.M)


@lru_cache(maxsize=None)
def compose_environment(service: str) -> dict:
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    assert service in services, (
        f"{service}: no service of that name in docker-compose.yml; this guard reads the "
        "service's DB_SCHEMA from the compose service named like its directory"
    )
    environment = services[service].get("environment") or {}
    assert isinstance(environment, dict), (
        f"{service}: the compose environment is not a mapping; teach compose_environment "
        "the list form before relying on this guard"
    )
    return environment


def deployed_db_schema_env(service: str) -> str | None:
    """DB_SCHEMA as base compose passes it, or None when compose passes none."""
    value = compose_environment(service).get("DB_SCHEMA")
    if value is None:
        return None
    assert isinstance(value, str) and re.fullmatch(r"\w+", value), (
        f"{service}: DB_SCHEMA in docker-compose.yml is {value!r}, not a plain literal; "
        "this guard imports the models with the deployed value and cannot resolve a "
        "substitution, so extend deployed_db_schema_env before changing it"
    )
    return value


@lru_cache(maxsize=None)
def imported(service: str) -> dict:
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env.update(_ENV)
    env.pop("DB_SCHEMA", None)
    if service in DATABASE_SERVICES:
        schema = deployed_db_schema_env(service)
        if schema is not None:
            env["DB_SCHEMA"] = schema
    result = subprocess.run(
        [sys.executable, "-c", _INTROSPECT],
        cwd=SERVICES_DIR / service,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"{service}: {result.stderr[-3000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def placement_problems(report: dict) -> list[str]:
    """Every way the database part of an import report breaks placement."""
    schema = report["db_schema"]
    problems = []
    if not schema:
        problems.append("the deployed DB_SCHEMA is empty")
    for key, table_schema in report["tables"]:
        if table_schema != schema or not key.startswith(f"{schema}."):
            problems.append(f"table {key} is in schema {table_schema!r}, not {schema!r}")
    for key, target in report["foreign_keys"]:
        parts = target.split(".")
        if len(parts) != 3 or parts[0] != schema:
            problems.append(f"{key} has a foreign key to {target}, outside schema {schema!r}")
    for key, column, type_name, type_schema in report["enums"]:
        if type_schema != schema:
            problems.append(
                f"{key}.{column} uses enum type {type_name} in schema {type_schema!r}, "
                f"not {schema!r}"
            )
    return problems


def test_every_service_is_found():
    assert set(SERVICES) >= {
        "acl",
        "ai-orchestrator",
        "auth",
        "cabling",
        "config",
        "execution",
        "integration",
        "inventory",
        "notifications",
        "reservations",
        "secrets",
        "user-profile",
    }
    assert set(DATABASE_SERVICES) == set(SERVICES) - {"config"}


@pytest.mark.parametrize("service", DATABASE_SERVICES)
def test_the_deployed_schema_is_one_init_sql_creates(service):
    schema = imported(service)["database"]["db_schema"]
    assert schema in init_sql_schemas(), (
        f"{service}: DB_SCHEMA {schema!r} is not created by infra/postgres/init.sql; a new "
        "service adds its CREATE SCHEMA line there (docs/specs/CONVENTIONS.md)"
    )


@pytest.mark.parametrize("service", DATABASE_SERVICES)
def test_every_model_table_is_in_the_deployed_schema(service):
    report = imported(service)["database"]
    assert report["tables"], f"{service}: no table was found on app.database.Base.metadata"
    assert placement_problems(report) == []


@pytest.mark.parametrize("service", [s for s in SERVICES if s != "config"])
def test_the_root_logger_is_the_json_handler_at_log_level(service):
    report = imported(service)
    assert [formatter for _, formatter in report["handlers"]] == [JSON_FORMATTER], (
        f"{service}: app.main must call herd_common.logging.setup_logging at import "
        f"(OPS-LOG-8); root handlers: {report['handlers']}"
    )
    assert report["root_level"] == logging.getLevelName(PROBE_LOG_LEVEL)
    assert report["held"] == dict.fromkeys(HELD_AT_WARNING, logging.WARNING)


def test_config_installs_no_log_handler():
    """OPS-LOG-9: the config service logs through the standard library with no
    handler of its own."""
    assert imported("config")["handlers"] == []


# Fixture reports for placement_problems: the honest shape, then each broken one.
_HONEST = {
    "db_schema": "svc",
    "tables": [["svc.a", "svc"], ["svc.b", "svc"]],
    "foreign_keys": [["svc.b", "svc.a.id"]],
    "enums": [["svc.a", "status", "status", "svc"]],
}


def _with(**changes) -> dict:
    return {**_HONEST, **changes}


def test_an_honest_report_has_no_problem():
    assert placement_problems(_HONEST) == []


@pytest.mark.parametrize(
    ("report", "problem"),
    [
        (
            {"db_schema": "", "tables": [], "foreign_keys": [], "enums": []},
            "the deployed DB_SCHEMA is empty",
        ),
        (
            _with(tables=[["svc.a", "svc"], ["b", None]]),
            "table b is in schema None, not 'svc'",
        ),
        (
            _with(tables=[["svc.a", "svc"], ["other.b", "other"]]),
            "table other.b is in schema 'other', not 'svc'",
        ),
        (
            _with(foreign_keys=[["svc.b", "a.id"]]),
            "svc.b has a foreign key to a.id, outside schema 'svc'",
        ),
        (
            _with(foreign_keys=[["svc.b", "other.a.id"]]),
            "svc.b has a foreign key to other.a.id, outside schema 'svc'",
        ),
        (
            _with(enums=[["svc.a", "status", "status", None]]),
            "svc.a.status uses enum type status in schema None, not 'svc'",
        ),
    ],
)
def test_each_broken_placement_is_named(report, problem):
    assert placement_problems(report) == [problem]
