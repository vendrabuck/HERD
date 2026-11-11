"""Compose capacity pins: bounded container logs and a database pool budget (issue #1352).

Two settings that `make prod` used to leave at defaults:

- Container logs. Without a `logging:` block every container used the Docker
  daemon's default json-file driver with no size limit, so a long-running install
  grew its logs until the disk (shared with Postgres) filled. Every service in every
  compose file now carries `logging: *default-logging`, the `x-logging` anchor at the
  top of its file: json-file, `max-size: "10m"`, `max-file: "5"`.
- Database connections. Each of the eleven database services builds its engine
  through `herd_common.database.make_database`, which opens at most `pool_size +
  max_overflow` connections per process; SQLAlchemy's defaults (5 + 10) gave
  11 x 15 = 165 against Postgres' default `max_connections` of 100. The pool is now
  set explicitly from `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` (defaults 5 and 10,
  SQLAlchemy's own) and Postgres runs with `-c max_connections=200`, so the defaults
  need 11 x 15 = 165.

What is checked, without a stack or Docker (YAML and Python ASTs only):

- every service of every compose file `tests/unit/test_image_pins.py` discovers (the
  tracked files through `git ls-files`, a filesystem walk only as a fallback) has a
  `logging` mapping equal to the one above, after YAML anchors resolve; a service in
  `docker-compose.override.yml` that `docker-compose.yml` also defines is covered by
  the base file's block, since compose merges the two;
- in `docker-compose.yml`, every service whose environment sets `DATABASE_URL` sets
  `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, and `DB_POOL_PRE_PING` as `${VAR:-default}`, and
  the sum over those services of the two size defaults is at most the
  `max_connections` the postgres service's `command` passes; and, computed from
  those compose defaults (never from literals here), docs/ENV_VARS.md states
  `<services> x (<size> + <overflow>) = <total>` and `<max_connections> - <total> =
  <headroom>`, and docs/OPERATIONS.md states the first, so a changed default fails
  until the docs follow;
- every `services/*/app/database.py` calls `make_database` with
  `pool_size=settings.db_pool_size`, `max_overflow=settings.db_max_overflow`, and
  `pool_pre_ping=settings.db_pool_pre_ping`.

Limits:

- not seen: a value an operator sets in `.env` (the budget is computed from the
  compose defaults only), extra execution replicas (`EXECUTION_POLLER_ONLY`, each one
  another full pool), connections from psql, `make migrate`, or test harnesses, a
  logging block set through `docker compose -f` files outside the repository, and a
  daemon-level `log-opts`;
- not judged: whether 10 MB times 5 suits a given host's disk, or whether 200 suits its
  memory.
"""

from __future__ import annotations

import ast
import re

import yaml

from tests.unit.test_image_pins import REPO_ROOT, compose_files

EXPECTED_LOGGING = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}
BASE = REPO_ROOT / "docker-compose.yml"
OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
POOL_VARIABLES = ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_PRE_PING")
_DEFAULTED = re.compile(r"\$\{(?P<var>[A-Z_]+):-(?P<default>[^}$]*)\}")


def logging_problems(services: dict, covered: set[str] = frozenset()) -> list[str]:
    problems = []
    for name, spec in services.items():
        if name in covered:
            continue
        logging = (spec or {}).get("logging")
        if logging != EXPECTED_LOGGING:
            problems.append(
                f"service {name}: logging is {logging!r}, expected {EXPECTED_LOGGING!r}"
            )
    return problems


def _max_connections(command) -> int | None:
    words = command.split() if isinstance(command, str) else list(command or [])
    for i, word in enumerate(words):
        text = str(word)
        if text == "-c" and i + 1 < len(words):
            text = str(words[i + 1])
        m = re.fullmatch(r"max_connections=(\d+)", text)
        if m:
            return int(m.group(1))
    return None


def pool_budget(services: dict) -> tuple[list[str], int, int | None, list[str]]:
    """(database services, summed size defaults, max_connections, problems)."""
    problems = []
    names = []
    total = 0
    for name, spec in services.items():
        environment = (spec or {}).get("environment") or {}
        if not isinstance(environment, dict) or "DATABASE_URL" not in environment:
            continue
        names.append(name)
        for variable in POOL_VARIABLES:
            m = _DEFAULTED.fullmatch(str(environment.get(variable, "")))
            if not m or m.group("var") != variable:
                problems.append(f"service {name}: {variable} must be ${{{variable}:-<default>}}")
                continue
            if variable != "DB_POOL_PRE_PING":
                total += int(m.group("default"))
    limit = _max_connections((services.get("postgres") or {}).get("command"))
    if limit is None:
        problems.append("the postgres service's command passes no max_connections")
    elif total > limit:
        problems.append(
            f"{len(names)} database services can open {total} connections with the "
            f"compose defaults, over max_connections={limit}"
        )
    return names, total, limit, problems


def test_every_compose_service_rotates_its_logs():
    base_services = set(yaml.safe_load(BASE.read_text())["services"])
    problems = []
    for path in compose_files():
        services = yaml.safe_load(path.read_text()).get("services") or {}
        covered = base_services if path == OVERRIDE else set()
        problems += [
            f"{path.relative_to(REPO_ROOT)}: {p}" for p in logging_problems(services, covered)
        ]
    assert not problems, "\n".join(problems)


def test_database_pools_fit_within_postgres_max_connections():
    services = yaml.safe_load(BASE.read_text())["services"]
    names, total, limit, problems = pool_budget(services)
    assert not problems, "\n".join(problems)
    assert len(names) == 11, f"expected the eleven database services, found {sorted(names)}"


def test_docs_state_the_budget_the_compose_defaults_give():
    services = yaml.safe_load(BASE.read_text())["services"]
    names, total, limit, _ = pool_budget(services)
    pairs = {
        tuple(
            int(_DEFAULTED.fullmatch(str(services[n]["environment"][v])).group("default"))
            for v in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW")
        )
        for n in names
    }
    assert len(pairs) == 1, f"the database services use different pool defaults: {pairs}"
    ((size, overflow),) = pairs
    product = f"{len(names)} x ({size} + {overflow}) = {total}"
    headroom = f"{limit} - {total} = {limit - total}"
    env_vars = (REPO_ROOT / "docs" / "ENV_VARS.md").read_text()
    operations = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
    assert product in env_vars and headroom in env_vars, (
        f"docs/ENV_VARS.md must state {product!r} and {headroom!r}"
    )
    assert product in operations, f"docs/OPERATIONS.md must state {product!r}"


def test_every_service_passes_its_pool_settings_to_make_database():
    expected = {
        "pool_size": "settings.db_pool_size",
        "max_overflow": "settings.db_max_overflow",
        "pool_pre_ping": "settings.db_pool_pre_ping",
    }
    files = sorted((REPO_ROOT / "services").glob("*/app/database.py"))
    assert len(files) == 11
    problems = []
    for path in files:
        calls = [
            node
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "make_database"
        ]
        if len(calls) != 1:
            problems.append(f"{path.relative_to(REPO_ROOT)}: {len(calls)} make_database calls")
            continue
        passed = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
        if passed != expected:
            problems.append(
                f"{path.relative_to(REPO_ROOT)}: keywords {passed}, expected {expected}"
            )
    assert not problems, "\n".join(problems)


def test_logging_check_reports_a_missing_or_different_block():
    services = {
        "ok": {"logging": EXPECTED_LOGGING},
        "none": {"image": "x:1.2.3"},
        "bigger": {
            "logging": {"driver": "json-file", "options": {"max-size": "1g", "max-file": "5"}}
        },
        "inherited": {"environment": {"A": "1"}},
    }
    assert logging_problems(services, {"inherited"}) == [
        f"service none: logging is None, expected {EXPECTED_LOGGING!r}",
        "service bigger: logging is "
        f"{services['bigger']['logging']!r}, expected {EXPECTED_LOGGING!r}",
    ]


def test_pool_budget_check_sums_defaults_and_compares_with_max_connections():
    def service(size, overflow):
        return {
            "environment": {
                "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/d",
                "DB_POOL_SIZE": f"${{DB_POOL_SIZE:-{size}}}",
                "DB_MAX_OVERFLOW": f"${{DB_MAX_OVERFLOW:-{overflow}}}",
                "DB_POOL_PRE_PING": "${DB_POOL_PRE_PING:-true}",
            }
        }

    services = {
        "a": service(5, 5),
        "b": service(5, 10),
        "nodb": {"environment": {"X": "1"}},
        "postgres": {"command": ["postgres", "-c", "max_connections=30"]},
    }
    assert pool_budget(services) == (["a", "b"], 25, 30, [])
    services["postgres"]["command"] = "postgres -c max_connections=20"
    assert pool_budget(services)[3] == [
        "2 database services can open 25 connections with the compose defaults, "
        "over max_connections=20"
    ]
    services["postgres"]["command"] = ["postgres"]
    assert pool_budget(services)[3] == ["the postgres service's command passes no max_connections"]
    services["b"]["environment"]["DB_MAX_OVERFLOW"] = "10"
    assert (
        "service b: DB_MAX_OVERFLOW must be ${DB_MAX_OVERFLOW:-<default>}"
        in pool_budget(services)[3]
    )
