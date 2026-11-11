"""The Postgres init scripts name no role and no database (issue #1325).

`infra/postgres/init.sql` used to GRANT every schema `TO herd` and run `ALTER
DATABASE herd SET search_path ...`. The official postgres image runs init files
through psql with `ON_ERROR_STOP=1` as `POSTGRES_USER` connected to `POSTGRES_DB`,
so with any other user or database name the first statement naming `herd` failed,
the container exited with the schemas created and the rest of the file unapplied,
and the restart then skipped initialization because the data directory existed. The
GRANTs were redundant (the connecting role owns the schemas it creates), and the
database is now named through `current_database()`.

What is read: every file docker-compose.yml mounts under
`/docker-entrypoint-initdb.d/` (the init files), with `--` comments and the contents
of single-quoted strings blanked out, except that a string right after `format(`, or
right after a `||`, is kept, since that is SQL the script executes. In what remains,
the guard fails on a statement that names a role or a database: `GRANT ... TO`,
`REVOKE ... FROM`, `OWNER TO`, `AUTHORIZATION`, `REASSIGN OWNED BY`, `DROP OWNED BY`,
`FOR ROLE` or `FOR USER`, `CREATE`, `ALTER`, or `DROP` of a `ROLE`, `USER`, or
`DATABASE` and `COMMENT ON DATABASE`, each followed by anything but the `%I`
placeholder, `SET ROLE`, `SET SESSION AUTHORIZATION`, and psql's `\\c` or
`\\connect`; and on the word `herd`, the default user and database name, anywhere.
Separately, with comments removed but strings intact, every `format(...)` call is
split into its top-level arguments, and an argument after the template that holds a
quoted string fails: a literal name passed as a `format()` argument (only computed
values such as `current_database()` pass).

Limits: read as text, never run against Postgres. Not seen: a name built at run time
(`EXECUTE` of a variable; a name in a string that neither follows `format(` or `||`
nor is an argument of `format()`, such as `quote_ident('lab')`),
a block comment (`/* */`), and an init file mounted by a compose file other than
docker-compose.yml. Dollar-quoted text (a `DO $$ ... $$` body) is read as SQL, not as
a string.
Whether the file succeeds at all is proven only by a fresh-volume boot (CI's
integration job and the gates start Postgres on a new volume).
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.unit.test_image_pins import REPO_ROOT

INIT_TARGET = "/docker-entrypoint-initdb.d/"

_FORBIDDEN = [
    (re.compile(r"\bGRANT\b[^;]*\bTO\b", re.I | re.S), "GRANT ... TO names a role"),
    (re.compile(r"\bREVOKE\b[^;]*\bFROM\b", re.I | re.S), "REVOKE ... FROM names a role"),
    (re.compile(r"\bOWNER\s+TO\b", re.I), "OWNER TO names a role"),
    (re.compile(r"\bAUTHORIZATION\b", re.I), "AUTHORIZATION names a role"),
    (
        re.compile(r"\b(CREATE|ALTER|DROP)\s+(ROLE|USER|DATABASE)\s+(?!%I\b)", re.I),
        "names a role or database",
    ),
    (re.compile(r"\bSET\s+ROLE\b", re.I), "SET ROLE names a role"),
    (
        re.compile(r"\bCOMMENT\s+ON\s+DATABASE\s+(?!%I\b)", re.I),
        "COMMENT ON DATABASE names a database",
    ),
    (re.compile(r"\b(REASSIGN|DROP)\s+OWNED\s+BY\b", re.I), "OWNED BY names a role"),
    (re.compile(r"\bFOR\s+(ROLE|USER)\b", re.I), "FOR ROLE names a role"),
    (re.compile(r"^\s*\\c(onnect)?\b", re.I | re.M), "psql \\connect names a database"),
    (re.compile(r"\bherd\b", re.I), "names herd, the default user and database"),
]


def init_files() -> list:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    files = []
    for spec in compose["services"].values():
        for volume in spec.get("volumes") or []:
            source, _, rest = str(volume).partition(":")
            target = rest.split(":")[0]
            if target.startswith(INIT_TARGET):
                files.append((REPO_ROOT / source).resolve())
    return files


def _executed_text(sql: str) -> str:
    """The SQL with comments and plain string contents blanked; format() strings kept."""
    out = []
    i = 0
    while i < len(sql):
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        if sql[i] == "'":
            end = i + 1
            while end < len(sql):
                if sql[end] == "'" and sql.startswith("''", end):
                    end += 2
                    continue
                if sql[end] == "'":
                    break
                end += 1
            literal = sql[i + 1 : end]
            inside_format = re.search(r"format\s*\(\s*$", "".join(out)[-200:], re.I) or re.search(
                r"\|\|\s*$", "".join(out)[-20:]
            )
            out.append(" " + literal + " " if inside_format else " '' ")
            i = end + 1
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def _without_comments(sql: str) -> str:
    """The SQL with `--` comments removed and every string left intact."""
    out = []
    i = 0
    in_string = False
    while i < len(sql):
        c = sql[i]
        if in_string:
            out.append(c)
            if c == "'":
                if sql.startswith("''", i):
                    out.append("'")
                    i += 2
                    continue
                in_string = False
        elif c == "'":
            in_string = True
            out.append(c)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def format_arguments(sql: str) -> list[list[str]]:
    """The top-level arguments of every `format(...)` call, comments removed."""
    text = _without_comments(sql)
    calls = []
    for match in re.finditer(r"\bformat\s*\(", text, re.I):
        i, depth, in_string = match.end(), 1, False
        args, current = [], []
        while i < len(text) and depth:
            c = text[i]
            if in_string:
                if c == "'" and text.startswith("''", i):
                    current.append("''")
                    i += 2
                    continue
                in_string = c != "'"
            elif c == "'":
                in_string = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 1:
                args.append("".join(current))
                current = []
                i += 1
                continue
            if depth:
                current.append(c)
            i += 1
        args.append("".join(current))
        calls.append([a.strip() for a in args])
    return calls


def init_problems(sql: str) -> list[str]:
    text = _executed_text(sql)
    problems = [
        f"a literal passed to format() as an argument names a role or database: {arg!r}"
        for args in format_arguments(sql)
        for arg in args[1:]
        if "'" in arg
    ]
    for statement in text.split(";"):
        for pattern, why in _FORBIDDEN:
            m = pattern.search(statement)
            if m:
                problems.append(f"{why}: {' '.join(statement.split())[:120]!r}")
    return problems


def test_compose_mounts_the_init_script():
    assert [p.relative_to(REPO_ROOT).as_posix() for p in init_files()] == [
        "infra/postgres/init.sql"
    ]


def test_init_files_name_no_role_or_database():
    problems = [
        f"{path.relative_to(REPO_ROOT)}: {problem}"
        for path in init_files()
        for problem in init_problems(path.read_text())
    ]
    assert not problems, "\n".join(problems)


def test_init_sql_still_creates_every_service_schema_and_sets_the_search_path():
    sql = (REPO_ROOT / "infra" / "postgres" / "init.sql").read_text()
    schemas = re.findall(r"^CREATE SCHEMA IF NOT EXISTS (\w+);$", sql, re.M)
    expected = [
        "auth",
        "inventory",
        "reservations",
        "acl",
        "user_profile",
        "cabling",
        "execution",
        "notifications",
        "ai_orchestrator",
        "integration",
        "secrets",
    ]
    assert schemas == expected
    # The format() template, its string pieces joined as the || operators join them,
    # must give the same search path the file set before #1325.
    start = sql.index("format(")
    call = sql[start : sql.index("current_database()", start)]
    template = "".join(re.findall(r"'((?:[^']|'')*)'", call))
    assert template == "ALTER DATABASE %I SET search_path TO public, " + ", ".join(expected)


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT ALL PRIVILEGES ON SCHEMA auth TO herd;",
        "GRANT USAGE ON SCHEMA auth TO labadmin;",
        "ALTER DATABASE herd SET search_path TO public;",
        "ALTER DATABASE lab SET search_path TO public;",
        "CREATE SCHEMA x AUTHORIZATION labadmin;",
        "ALTER SCHEMA x OWNER TO labadmin;",
        "CREATE ROLE reader;",
        "\\connect lab",
        "DO $$ BEGIN EXECUTE format('ALTER DATABASE herd SET x TO y'); END $$;",
        "DO $$ BEGIN EXECUTE format('GRANT ALL ON SCHEMA a ' || 'TO someone'); END $$;",
        "COMMENT ON DATABASE lab IS 'x';",
        "REASSIGN OWNED BY labadmin TO postgres;",
        "DROP OWNED BY labadmin;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE labadmin IN SCHEMA a REVOKE ALL ON TABLES FROM PUBLIC;",
        "DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET search_path TO public', 'lab'); END $$;",
        "DO $$ BEGIN EXECUTE format('COMMENT ON DATABASE %I IS %L', 'lab', 'x'); END $$;",
    ],
)
def test_detector_refuses_a_literal_role_or_database(sql):
    assert init_problems(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE SCHEMA IF NOT EXISTS newsvc;",
        "-- Grant the herd user access (a comment)\nCREATE SCHEMA IF NOT EXISTS a;",
        "DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET search_path TO public, a', "
        "current_database()); END $$;",
        "COMMENT ON SCHEMA a IS 'owned by the herd user';",
        "DO $$ BEGIN EXECUTE format('COMMENT ON DATABASE %I IS ''x''', "
        "current_database()); END $$;",
        "SELECT format('%s', upper(current_user));",
    ],
)
def test_detector_passes_name_free_sql(sql):
    assert init_problems(sql) == []
