"""Static pin: a live-Postgres suite connects only to the database HERD_TEST_PG_DSN names.

No database, no stack: this parses Python files with `ast` and reads the Makefile,
`.env.example`, and the `pyproject.toml` files as text.

Before issue #1401 every `*_live_pg.py` suite fell back to the dev stack's published
Postgres (`postgresql+asyncpg://herd:herd@127.0.0.1:${POSTGRES_PORT:-5433}/herd`)
when HERD_TEST_PG_DSN was unset, so a bare `make test` or `pytest` wrote into the
working database. The rule now lives in ONE helper,
`herd_common.pg_live_suite.pg_live_target` (its behavior is pinned by
`services/common/tests/test_pg_live_suite.py`), and this guard keeps a default from
coming back anywhere else. It fails when:

1. a `services/*/tests/**/*_live_pg.py` suite does not take its database from the
   helper in the one shape: `from herd_common.pg_live_suite import pg_live_target`
   at module level, one module-level `_PG = pg_live_target()` with no arguments,
   `PG_DSN`, when bound, bound once as a module-level `PG_DSN = _PG.dsn`,
   `pytestmark` bound once, at module level, exactly to
   `pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")`,
   and a module-level function decorated exactly `@pytest.fixture(autouse=True)`
   whose body is exactly `if _PG.failure is not None: pytest.fail(_PG.failure)`.
   `_PG`, `PG_DSN`, and `pg_live_target` may not be bound a second time anywhere in
   the file (an assignment at any depth, `global`, a loop, `with`, or `except`
   target, a parameter, an import), and no call may pass the string `"PG_DSN"` or
   `"_PG"` (`monkeypatch.setattr(module, "PG_DSN", ...)`);
2. a live suite, or the helper module itself, holds outside a docstring a string
   that is a Postgres URL (`postgres://`, `postgresql://`, `postgresql+<driver>://`,
   any case) whose host is not the placeholder `HOST`, a string holding a loopback
   address (`localhost`, `127.x.y.z`, `0.0.0.0`, `::1`), the old default port 5433
   as an int or inside a string, or a string that is the name of an environment
   variable a default could be read from (`HERD_TEST_PG_*`, `POSTGRES_*`, the libpq
   `PG*` names, `DATABASE_URL`); the helper is exempt from the last item only, since
   it is the reader. A `+` of literals and the literal parts of an f-string are
   joined before they are judged (adjacent literals are joined by the parser);
3. any Python file under `services/`, `tests/`, `scripts/`, or `infra/` (outside
   `.venv`, `node_modules`, and `__pycache__`), other than the files in
   `READER_EXEMPT` (the helper, its tests, and the gate-recipe guard, which reads a
   parsed recipe's assignments), reads or writes HERD_TEST_PG_DSN or HERD_TEST_PG_REQUIRED in the
   process environment: a `getenv`, `putenv`, or `unsetenv` call, or a `.get`,
   `.pop`, `.setdefault`, `.setenv`, or `.delenv` call, whose first argument names
   the variable; a subscript by the name (load, store, or delete); an `.update(...)`
   call that carries the name as a keyword or a dict key; and an `|=` of a dict that
   carries it as a key. The variable is named by the string literal, by a name the
   same file binds to that literal, or by `DSN_VARIABLE` / `REQUIRED_VARIABLE` from
   the helper (imported, or read as an attribute). Naming it as a dict key or a
   keyword anywhere else (a child process's `env=`) is allowed: that names a
   database explicitly for a process the file starts;
4. the Makefile names HERD_TEST_PG_DSN on a line that is not a comment outside the
   `_gate-pg-live-tests` rule and its recipe (a global or target-specific
   `export HERD_TEST_PG_DSN = ...`, or `HERD_TEST_PG_DSN=... pytest` in `make test`,
   would hand every run a default), or `.env.example` or a `pyproject.toml` (the
   repo root's and each service's) names it on a line that is not a comment.

Limits. Not seen: a DSN assembled at run time from pieces none of these rules
refuses (a non-loopback host name and a port that is not 5433, `chr()` arithmetic,
text read from a file); a default supplied from outside the scanned files (a pytest
plugin installed in the environment, `PYTEST_ADDOPTS`, `sitecustomize`, a wrapper
script, a CI workflow's `env:`, which is an explicit choice and allowed); the
environment reached by another spelling (`getattr(os, "environ")`,
`os.environ.__setitem__`, a name built by concatenation or bound in another file
than the helper); a test file not named `*_live_pg.py` that opens its own Postgres
connection; a binding inside a `match` pattern; and the helper's run-time behavior,
which its own tests pin. Read conservatively (refused though it may be sound): a
`.get`, `.pop`, or `.setdefault` with the variable's name on ANY object in a file
outside `READER_EXEMPT`; a live suite that adds another mark to `pytestmark` or
writes the fixture differently; a Postgres URL, loopback address, or 5433 in a live
suite even as a harmless example outside a docstring (examples go in the
docstring); a TOML or `.env.example` line that holds the name before a trailing
comment. In the Makefile a line is a comment only when it starts with `#` after any
leading whitespace and `@`, `-`, or `+` prefix characters.

The detector tests below feed these spellings to the same scans the real-tree tests
use, in both directions.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "services"
HELPER_PATH = "services/common/herd_common/pg_live_suite.py"
HELPER_MODULE = "herd_common.pg_live_suite"
GATE_TARGET = "_gate-pg-live-tests"

DSN_NAME = "HERD_TEST_PG_DSN"
REQUIRED_NAME = "HERD_TEST_PG_REQUIRED"
VARIABLE_NAMES = frozenset({DSN_NAME, REQUIRED_NAME})
# The helper's constants that hold the two names.
HELPER_NAME_CONSTANTS = frozenset({"DSN_VARIABLE", "REQUIRED_VARIABLE"})
# Names a live suite may not bind more than its one sanctioned binding.
PINNED_NAMES = ("_PG", "PG_DSN", "pg_live_target")

# The files allowed to read or write the two variables in the process environment.
READER_EXEMPT = {
    # The ONE reader.
    HELPER_PATH,
    # Its tests drive the default (process environment) path with monkeypatch.setenv.
    "services/common/tests/test_pg_live_suite.py",
    # Reads the variables from a dict of a parsed Makefile recipe's assignments
    # (`env.get("HERD_TEST_PG_REQUIRED")`), never from the process environment.
    "tests/unit/test_pg_live_gate_wiring.py",
}
PYTHON_ROOTS = ("services", "tests", "scripts", "infra")
SKIPPED_DIRS = frozenset({".venv", "node_modules", "__pycache__", ".git"})

_URL = re.compile(r"(?i)\bpostgres(?:ql)?(?:\+[a-z0-9_]+)?://(?P<rest>[^\s'\"]*)")
_LOOPBACK = re.compile(r"(?i)(?<![\w.:])(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|::1)(?![\w.])")
_OLD_PORT = re.compile(r"(?<!\d)5433(?!\d)")
_ENV_NAME = re.compile(r"(?:HERD_TEST_PG_[A-Z0-9_]*|POSTGRES_[A-Z0-9_]*|PG[A-Z]+|DATABASE_URL)")
_PLACEHOLDER_HOST = "HOST"
_ENVIRONMENT_FIRST_ARGUMENT_CALLS = frozenset(
    {"getenv", "putenv", "unsetenv", "get", "pop", "setdefault", "setenv", "delenv"}
)


def _dump(source: str, mode: str = "exec") -> str:
    tree = ast.parse(source, mode=mode)
    return ast.dump(tree.body if mode == "eval" else tree.body[0])


_EXPECTED_TARGET_CALL = _dump("pg_live_target()", "eval")
_EXPECTED_DSN_VALUE = _dump("_PG.dsn", "eval")
_EXPECTED_MARK = _dump(
    'pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")', "eval"
)
_EXPECTED_DECORATOR = _dump("pytest.fixture(autouse=True)", "eval")
_EXPECTED_FIXTURE_BODY = _dump("if _PG.failure is not None:\n    pytest.fail(_PG.failure)\n")


# Shared readers.


def _docstring_ids(tree: ast.AST) -> set[int]:
    """ids of the Constant nodes that are docstrings (module, class, function)."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _fold(node: ast.AST) -> str | None:
    """The text of a string literal, a `+` of string literals, or an f-string's
    literal parts joined; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value for p in node.values if isinstance(p, ast.Constant))
    return None


def code_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, text) of every string the code holds outside a docstring: each maximal
    `+` of literals once, each f-string once (its literal parts joined), and each
    other literal."""
    skip = _docstring_ids(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _fold(node) if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Constant)) else None
        if text is None:
            continue
        found.append((node.lineno, text))
        skip.update(id(inner) for inner in ast.walk(node) if inner is not node)
    return found


def _url_names_a_database(rest: str) -> bool:
    """Whether the part of a Postgres URL after `://` names a host other than the
    placeholder (`user:password@host:port/db`, `host/db`, `[::1]:5432/db`)."""
    authority = rest.split("/", 1)[0].split("?", 1)[0].rsplit("@", 1)[-1]
    host = authority[1:].split("]", 1)[0] if authority.startswith("[") else authority.split(":")[0]
    return host != _PLACEHOLDER_HOST


def database_literals(source: str, *, reader: bool = False) -> list[str]:
    """Rule 2: what a live suite (or, with `reader`, the helper) holds outside its
    docstrings that names a database or the place a default could come from."""
    tree = ast.parse(source)
    problems = []
    for line, text in code_strings(tree):
        if any(_url_names_a_database(m.group("rest")) for m in _URL.finditer(text)):
            problems.append(f"line {line}: a Postgres URL {text!r}")
        if _LOOPBACK.search(text):
            problems.append(f"line {line}: a loopback address {text!r}")
        if _OLD_PORT.search(text):
            problems.append(f"line {line}: the old default port in {text!r}")
        if not reader and _ENV_NAME.fullmatch(text):
            problems.append(f"line {line}: the environment variable name {text!r}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and type(node.value) is int and node.value == 5433:
            problems.append(f"line {node.lineno}: the old default port 5433")
    return problems


def _bindings(tree: ast.Module) -> list[tuple[str, int, ast.AST | None]]:
    """(name, line, value) for every binding of a name in the file; value is the
    assigned expression for a module-level single-target `NAME = value`, the
    ImportFrom node for a module-level import, and None for any other binding."""
    module_assign = {}
    module_import = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            if isinstance(stmt.targets[0], ast.Name):
                module_assign[id(stmt.targets[0])] = stmt.value
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            module_import[id(stmt)] = stmt
    found: list[tuple[str, int, ast.AST | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append((node.id, node.lineno, module_assign.get(id(node))))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                found.append((name, node.lineno, module_import.get(id(node))))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend((name, node.lineno, None) for name in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node.lineno, None))
        elif isinstance(node, ast.arg):
            found.append((node.arg, node.lineno, None))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.append((node.name, node.lineno, None))
    return found


def helper_shape_problems(source: str) -> list[str]:
    """Rule 1: how a live suite's source departs from the one shape."""
    tree = ast.parse(source)
    problems: list[str] = []
    bound: dict[str, list[tuple[int, ast.AST | None]]] = {}
    for name, line, value in _bindings(tree):
        bound.setdefault(name, []).append((line, value))

    imports = bound.get("pg_live_target", [])
    sound_import = (
        len(imports) == 1
        and isinstance(imports[0][1], ast.ImportFrom)
        and imports[0][1].module == HELPER_MODULE
        and any(a.name == "pg_live_target" and a.asname is None for a in imports[0][1].names)
    )
    if not sound_import:
        problems.append(
            f"pg_live_target must be imported once, at module level, from {HELPER_MODULE} "
            f"and bound nowhere else (bindings on lines {[line for line, _ in imports]})"
        )

    targets = bound.get("_PG", [])
    if not (
        len(targets) == 1
        and targets[0][1] is not None
        and ast.dump(targets[0][1]) == _EXPECTED_TARGET_CALL
    ):
        problems.append(
            "_PG must be bound once, as a module-level `_PG = pg_live_target()` with no "
            f"arguments (bindings on lines {[line for line, _ in targets]})"
        )

    dsns = bound.get("PG_DSN", [])
    if dsns and not (
        len(dsns) == 1 and dsns[0][1] is not None and ast.dump(dsns[0][1]) == _EXPECTED_DSN_VALUE
    ):
        problems.append(
            "PG_DSN must be bound once, as a module-level `PG_DSN = _PG.dsn` "
            f"(bindings on lines {[line for line, _ in dsns]})"
        )

    marks = bound.get("pytestmark", [])
    if not (
        len(marks) == 1 and marks[0][1] is not None and ast.dump(marks[0][1]) == _EXPECTED_MARK
    ):
        problems.append(
            "pytestmark must be bound once, at module level, to exactly "
            '`pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")`'
        )

    fixture_found = any(
        isinstance(stmt, ast.FunctionDef)
        and len(stmt.decorator_list) == 1
        and ast.dump(stmt.decorator_list[0]) == _EXPECTED_DECORATOR
        and not stmt.args.args
        and len(stmt.body) == 1
        and ast.dump(stmt.body[0]) == _EXPECTED_FIXTURE_BODY
        for stmt in tree.body
    )
    if not fixture_found:
        problems.append(
            "no module-level `@pytest.fixture(autouse=True)` function whose body is exactly "
            "`if _PG.failure is not None: pytest.fail(_PG.failure)`"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for argument in node.args:
                if isinstance(argument, ast.Constant) and argument.value in ("PG_DSN", "_PG"):
                    problems.append(
                        f"line {node.lineno}: a call names {argument.value!r} as a string"
                    )
    return problems


def _variable_aliases(tree: ast.AST) -> set[str]:
    """Names this file binds to one of the variable names: `X = "HERD_TEST_PG_DSN"`
    at any depth, and the helper's DSN_VARIABLE / REQUIRED_VARIABLE imported."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Constant) and value.value in VARIABLE_NAMES:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ImportFrom) and node.module == HELPER_MODULE:
            aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in HELPER_NAME_CONSTANTS
            )
    return aliases


def environment_touches(source: str) -> list[str]:
    """Rule 3: every read or write of the two variables in the process environment."""
    tree = ast.parse(source)
    aliases = _variable_aliases(tree)

    def names_it(node: ast.AST | None) -> bool:
        if isinstance(node, ast.Constant):
            return node.value in VARIABLE_NAMES
        if isinstance(node, ast.Name):
            return node.id in aliases
        if isinstance(node, ast.Attribute):
            return node.attr in HELPER_NAME_CONSTANTS
        return False

    def dict_carries_it(node: ast.AST) -> bool:
        return isinstance(node, ast.Dict) and any(names_it(key) for key in node.keys)

    touches = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called in _ENVIRONMENT_FIRST_ARGUMENT_CALLS and node.args and names_it(node.args[0]):
                touches.append(f"line {node.lineno}: {called}() of the variable")
            if called == "update" and (
                any(k.arg in VARIABLE_NAMES for k in node.keywords)
                or any(dict_carries_it(a) for a in node.args)
            ):
                touches.append(f"line {node.lineno}: update() carrying the variable")
        elif isinstance(node, ast.Subscript) and names_it(node.slice):
            touches.append(f"line {node.lineno}: a subscript by the variable")
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.op, ast.BitOr)
            and dict_carries_it(node.value)
        ):
            touches.append(f"line {node.lineno}: |= of a dict carrying the variable")
    return touches


def gate_lines(makefile_lines: list[str]) -> range:
    """The line indexes of the `_gate-pg-live-tests` rule and its recipe: the rule
    line, then every line up to the next one that is neither blank, a recipe line (a
    leading tab), a continuation, nor a column-0 comment."""
    rule = re.compile(rf"^{re.escape(GATE_TARGET)}\s*:(?!=)")
    start = next((i for i, line in enumerate(makefile_lines) if rule.match(line)), None)
    if start is None:
        return range(0)
    end = start + 1
    continued = makefile_lines[start].endswith("\\")
    while end < len(makefile_lines):
        line = makefile_lines[end]
        if not (continued or not line.strip() or line.startswith(("\t", "#"))):
            break
        continued = line.endswith("\\")
        end += 1
    return range(start, end)


def _is_comment(line: str) -> bool:
    return line.lstrip(" \t@-+").startswith("#")


def makefile_mentions(makefile_text: str) -> list[int]:
    """Rule 4: 1-based lines of the Makefile that name HERD_TEST_PG_DSN outside the
    gate rule and outside a comment."""
    lines = makefile_text.splitlines()
    allowed = gate_lines(lines)
    return [
        number + 1
        for number, line in enumerate(lines)
        if DSN_NAME in line and number not in allowed and not _is_comment(line)
    ]


def config_mentions(text: str) -> list[int]:
    """Rule 4: 1-based lines of a TOML or dotenv file that name HERD_TEST_PG_DSN
    outside a comment line."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if DSN_NAME in line and not line.lstrip().startswith("#")
    ]


# The real tree.


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _live_suites() -> list[Path]:
    return sorted(
        p
        for p in SERVICES_DIR.glob("*/tests/**/*_live_pg.py")
        if not SKIPPED_DIRS & set(p.relative_to(REPO_ROOT).parts)
    )


def _python_files() -> list[Path]:
    found = []
    for root in PYTHON_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if not SKIPPED_DIRS & set(path.relative_to(REPO_ROOT).parts):
                found.append(path)
    root_conftest = REPO_ROOT / "conftest.py"
    if root_conftest.exists():
        found.append(root_conftest)
    return sorted(found)


def _config_files() -> list[Path]:
    candidates = [REPO_ROOT / ".env.example", REPO_ROOT / "pyproject.toml"]
    candidates += sorted(SERVICES_DIR.glob("*/pyproject.toml"))
    for name in ("pytest.ini", "setup.cfg", "tox.ini"):
        candidates += [REPO_ROOT / name, *sorted(SERVICES_DIR.glob(f"*/{name}"))]
    return [p for p in candidates if p.exists()]


def test_the_scans_find_the_live_suites_and_the_helper():
    """A guard over an empty set passes while proving nothing: the glob must find the
    suites the gate runs, and the helper must exist where the exemption names it."""
    assert len(_live_suites()) >= 20
    assert (REPO_ROOT / HELPER_PATH).is_file()
    assert all((REPO_ROOT / path).is_file() for path in READER_EXEMPT)
    assert len(_python_files()) > len(_live_suites())
    assert gate_lines((REPO_ROOT / "Makefile").read_text().splitlines())


@pytest.mark.parametrize("suite", _live_suites(), ids=_rel)
def test_every_live_suite_takes_its_database_from_the_helper(suite: Path):
    problems = helper_shape_problems(suite.read_text())
    assert not problems, f"{_rel(suite)}:\n" + "\n".join(problems)


@pytest.mark.parametrize("suite", _live_suites(), ids=_rel)
def test_no_live_suite_names_a_database_itself(suite: Path):
    problems = database_literals(suite.read_text())
    assert not problems, f"{_rel(suite)}:\n" + "\n".join(problems)


def test_the_helper_holds_no_default_database():
    problems = database_literals((REPO_ROOT / HELPER_PATH).read_text(), reader=True)
    assert not problems, "\n".join(problems)


def test_only_the_helper_touches_the_variables_in_the_environment():
    found = []
    for path in _python_files():
        if _rel(path) in READER_EXEMPT:
            continue
        found += [f"{_rel(path)} {touch}" for touch in environment_touches(path.read_text())]
    assert not found, "\n".join(found)


def test_no_makefile_line_outside_the_gate_supplies_the_dsn():
    lines = makefile_mentions((REPO_ROOT / "Makefile").read_text())
    assert not lines, f"Makefile lines {lines} name {DSN_NAME} outside {GATE_TARGET}"


def test_no_config_file_supplies_the_dsn():
    found = {_rel(p): config_mentions(p.read_text()) for p in _config_files()}
    assert not {path: lines for path, lines in found.items() if lines}


# The scans themselves, on synthetic sources.

SOUND_SUITE = '''"""A live suite.

Run it with HERD_TEST_PG_DSN=postgresql+asyncpg://herd:herd@127.0.0.1:5433/herd, or
the dead address postgresql+asyncpg://x:x@127.0.0.1:9/x to see the failure.
"""

import pytest
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy.ext.asyncio import create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn
pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture
async def engine():
    """Example: postgresql+asyncpg://user:pass@localhost:5433/db."""
    engine = create_async_engine(PG_DSN, connect_args={"server_settings": {"search_path": "x"}})
    yield engine
    await engine.dispose()


async def test_x(engine):
    assert "application_name" != "PG_live"
'''


def _swap(old: str, new: str) -> str:
    assert old in SOUND_SUITE, old
    return SOUND_SUITE.replace(old, new, 1)


def test_scans_accept_the_sound_suite():
    assert helper_shape_problems(SOUND_SUITE) == []
    assert database_literals(SOUND_SUITE) == []
    assert environment_touches(SOUND_SUITE) == []


_TARGET = "_PG = pg_live_target()\nPG_DSN = _PG.dsn\n"

SHAPE_BREAKS = {
    "the old default restored": _swap(
        _TARGET,
        'import os\nPG_DSN = os.getenv("HERD_TEST_PG_DSN", '
        '"postgresql+asyncpg://herd:herd@127.0.0.1:5433/herd")\n_PG = pg_live_target()\n',
    ),
    "a default after the helper": _swap(
        "PG_DSN = _PG.dsn\n", 'PG_DSN = _PG.dsn or "postgresql://h@db.internal/herd"\n'
    ),
    "PG_DSN rebound later": _swap("async def test_x", 'PG_DSN = "x"\n\n\nasync def test_x'),
    "PG_DSN rebound in a function": _swap(
        '    assert "application',
        "    global PG_DSN\n    PG_DSN = 'x'\n    assert \"application",
    ),
    "PG_DSN patched by name": _swap(
        '    assert "application',
        "    import sys\n"
        "    setattr(sys.modules[__name__], 'PG_DSN', 'x')\n"
        '    assert "application',
    ),
    "an injected environment": _swap(
        "_PG = pg_live_target()", '_PG = pg_live_target({"HERD_TEST_PG_DSN": "x"})'
    ),
    "an injected probe": _swap("_PG = pg_live_target()", "_PG = pg_live_target(probe=print)"),
    "the helper imported from elsewhere": _swap(
        "from herd_common.pg_live_suite import pg_live_target",
        "from tests._local import pg_live_target",
    ),
    "the helper aliased": _swap(
        "from herd_common.pg_live_suite import pg_live_target\n",
        "from herd_common.pg_live_suite import pg_live_target\n"
        "from herd_common import pg_live_suite as pg_live_target\n",
    ),
    "the skip widened": _swap("_PG.skip_reason is not None, reason", "True, reason"),
    "the skip inverted": _swap(
        "_PG.skip_reason is not None, reason", "_PG.skip_reason is None, reason"
    ),
    "the skip made unconditional on the failure": _swap(
        "_PG.skip_reason is not None, reason",
        "_PG.skip_reason is not None or _PG.failure is not None, reason",
    ),
    "the failure turned into a skip": _swap("pytest.fail(_PG.failure)", "pytest.skip(_PG.failure)"),
    "the failure fixture not autouse": _swap("@pytest.fixture(autouse=True)", "@pytest.fixture"),
    "the failure fixture guarded by REQUIRED only": _swap(
        "    if _PG.failure is not None:\n",
        "    if _PG.failure is not None and False:\n",
    ),
    "no skip mark": _swap(
        "pytestmark = pytest.mark.skipif(",
        "_unused = pytest.mark.skipif(",
    ),
    "_PG rebound": _swap("async def test_x", "_PG = None\n\n\nasync def test_x"),
}


@pytest.mark.parametrize("case", sorted(SHAPE_BREAKS))
def test_shape_scan_refuses(case: str):
    """Each break is refused by the shape scan alone; the ones that also write a
    database or a variable name are refused by the literal scan as well."""
    assert helper_shape_problems(SHAPE_BREAKS[case]), case


LITERAL_BREAKS = {
    "a URL default": 'X = "postgresql+asyncpg://herd:herd@db.internal:6543/herd"\n',
    "a plain postgres URL": 'X = "postgres://db.internal/herd"\n',
    "an upper-case scheme": 'X = "POSTGRESQL://db.internal/herd"\n',
    "a URL split by +": 'X = "postgres" + "ql+asyncpg://herd@db.internal/herd"\n',
    "a URL in an f-string": 'P = 6543\nX = f"postgresql+asyncpg://herd@db.internal:{P}/herd"\n',
    "a URL built from parts by an f-string": 'S = "ql"\nX = f"postgres{S}://h@db.internal/d"\n',
    "a loopback host": 'X = "127.0.0.1"\n',
    "another loopback address": 'X = "127.0.1.1"\n',
    "localhost": 'X = "localhost"\n',
    "ipv6 loopback": 'X = "::1"\n',
    "the old port as an int": "X = 5433\n",
    "the old port in a string": 'X = "5433"\n',
    "the old port variable": 'X = "POSTGRES_PORT"\n',
    "a libpq variable": 'X = "PGHOST"\n',
    "DATABASE_URL": 'X = "DATABASE_URL"\n',
    "the DSN variable named": 'X = "HERD_TEST_PG_DSN"\n',
    "a URL with an IPv6 host": 'X = "postgresql://u@[fd00::5]:5432/d"\n',
    "a URL whose host is not the placeholder": 'X = "postgresql://USER@Host:PORT/DB"\n',
}


@pytest.mark.parametrize("case", sorted(LITERAL_BREAKS))
def test_literal_scan_refuses(case: str):
    assert database_literals(SOUND_SUITE + LITERAL_BREAKS[case]), case


LITERAL_HONEST = {
    "a docstring example": '"""postgresql+asyncpg://herd:herd@127.0.0.1:5433/herd"""\n',
    "a function docstring example": (
        'def f():\n    """Use postgresql://x:x@127.0.0.1:9/x, PGHOST, or 5433."""\n'
    ),
    "the placeholder URL": 'X = "postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DB"\n',
    "a comment": "# postgresql+asyncpg://herd:herd@127.0.0.1:5433/herd\nX = 1\n",
    "another number": "X = 54330 + 15433\n",
    "a version string": 'X = "1.127.0.0.15"\n',
    "a word containing PG": 'X = "PG_live"\nY = "pg_dump"\n',
}


@pytest.mark.parametrize("case", sorted(LITERAL_HONEST))
def test_literal_scan_accepts(case: str):
    assert database_literals(LITERAL_HONEST[case]) == [], case


def test_the_helper_may_name_the_variables_it_reads():
    source = 'DSN_VARIABLE = "HERD_TEST_PG_DSN"\nREQUIRED_VARIABLE = "HERD_TEST_PG_REQUIRED"\n'
    assert database_literals(source, reader=True) == []
    assert database_literals(source)
    assert database_literals(source + 'X = "127.0.0.1"\n', reader=True)


ENVIRONMENT_BREAKS = {
    "getenv with a default": 'import os\nX = os.getenv("HERD_TEST_PG_DSN", "postgresql://h/d")\n',
    "environ.get": 'import os\nX = os.environ.get("HERD_TEST_PG_DSN")\n',
    "environ subscript": 'import os\nX = os.environ["HERD_TEST_PG_DSN"]\n',
    "environ store": 'import os\nos.environ["HERD_TEST_PG_DSN"] = "postgresql://h/d"\n',
    "environ delete": 'import os\ndel os.environ["HERD_TEST_PG_REQUIRED"]\n',
    "setdefault in a conftest": (
        'import os\nos.environ.setdefault("HERD_TEST_PG_DSN", "postgresql://h/d")\n'
    ),
    "putenv": 'import os\nos.putenv("HERD_TEST_PG_DSN", "postgresql://h/d")\n',
    "bare getenv": 'from os import getenv\nX = getenv("HERD_TEST_PG_DSN")\n',
    "monkeypatch.setenv in a fixture": (
        "def f(monkeypatch):\n    monkeypatch.setenv('HERD_TEST_PG_DSN', 'postgresql://h/d')\n"
    ),
    "monkeypatch.delenv": (
        "def f(monkeypatch):\n    monkeypatch.delenv('HERD_TEST_PG_REQUIRED')\n"
    ),
    "update with a keyword": 'import os\nos.environ.update(HERD_TEST_PG_DSN="postgresql://h/d")\n',
    "update with a dict": 'import os\nos.environ.update({"HERD_TEST_PG_DSN": "postgresql://h/d"})\n',
    "|= of a dict": 'import os\nos.environ |= {"HERD_TEST_PG_DSN": "postgresql://h/d"}\n',
    "the name held in a variable": 'import os\nK = "HERD_TEST_PG_DSN"\nos.environ[K] = "x"\n',
    "the helper's constant imported": (
        "import os\nfrom herd_common.pg_live_suite import DSN_VARIABLE as V\nos.environ[V] = 'x'\n"
    ),
    "the helper's constant as an attribute": (
        "import os\nfrom herd_common import pg_live_suite\n"
        "os.environ.setdefault(pg_live_suite.DSN_VARIABLE, 'x')\n"
    ),
    "the name held in an annotated variable": (
        'import os\nK: str = "HERD_TEST_PG_DSN"\nX = os.environ.pop(K)\n'
    ),
}


@pytest.mark.parametrize("case", sorted(ENVIRONMENT_BREAKS))
def test_environment_scan_refuses(case: str):
    assert environment_touches(ENVIRONMENT_BREAKS[case]), case


ENVIRONMENT_HONEST = {
    "a child process env": (
        "import os, subprocess\n"
        'subprocess.run(["pytest"], env={**os.environ, "HERD_TEST_PG_DSN": "postgresql://h/d"})\n'
    ),
    "dict() with a keyword": (
        'import os\nENV = dict(os.environ, HERD_TEST_PG_DSN="postgresql+asyncpg://x:x@127.0.0.1:9/x")\n'
    ),
    "the name in a set": 'ALLOWED = {"HERD_TEST_PG_REQUIRED", "HERD_TEST_PG_DSN"}\n',
    "the name in a docstring": 'def f():\n    """Set HERD_TEST_PG_DSN first."""\n',
    "another variable read": 'import os\nX = os.environ.get("HERD_TEST_LDAP_REQUIRED")\n',
    "a throwaway-cluster helper": (
        "import os, subprocess\n"
        "def run(port):\n"
        "    dsn = f'postgresql+asyncpg://herd@127.0.0.1:{port}/herd'\n"
        "    env = os.environ.copy()\n"
        "    env.pop('VIRTUAL_ENV', None)\n"
        "    return subprocess.run(['pytest'], env={**env, 'HERD_TEST_PG_DSN': dsn})\n"
    ),
}


@pytest.mark.parametrize("case", sorted(ENVIRONMENT_HONEST))
def test_environment_scan_accepts(case: str):
    assert environment_touches(ENVIRONMENT_HONEST[case]) == [], case


_MAKEFILE = (
    "# HERD_TEST_PG_DSN is passed by the gate only.\n"
    "test:\n"
    "\t@for svc in a b; do (cd services/$$svc && uv run pytest tests/ -v); done\n"
    "\n"
    f"{GATE_TARGET}:\n"
    '\t@dsn="postgresql+asyncpg://x@127.0.0.1:5433/x"; \\\n'
    '\t(cd services/a && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \\\n'
    "\t\tuv run pytest tests/test_a_live_pg.py -v)\n"
    "\t# a comment inside the recipe naming HERD_TEST_PG_DSN\n"
    "\n"
    "next:\n"
    "\t@true\n"
)


def test_makefile_scan_accepts_the_gate_and_comments():
    assert makefile_mentions(_MAKEFILE) == []


@pytest.mark.parametrize(
    "addition",
    [
        "export HERD_TEST_PG_DSN = postgresql+asyncpg://herd:herd@127.0.0.1:5433/herd\n",
        "HERD_TEST_PG_DSN ?= postgresql+asyncpg://herd:herd@127.0.0.1:5433/herd\n",
        "test: export HERD_TEST_PG_DSN = postgresql://h/d\n",
        "other:\n\tHERD_TEST_PG_DSN=postgresql://h/d uv run pytest tests/ -v\n",
        "other:\n\t@HERD_TEST_PG_DSN=x make test\n",
    ],
)
def test_makefile_scan_refuses_a_dsn_outside_the_gate(addition: str):
    for text in (addition + _MAKEFILE, _MAKEFILE + addition):
        assert makefile_mentions(text), addition


def test_makefile_scan_ends_the_gate_at_the_next_rule():
    text = _MAKEFILE.replace("next:\n\t@true\n", "next:\n\tHERD_TEST_PG_DSN=x pytest\n")
    assert makefile_mentions(text) == [len(text.splitlines())]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# HERD_TEST_PG_DSN=postgresql://h/d\n", []),
        ("[tool.pytest.ini_options]\nenv = ['HERD_TEST_PG_DSN=postgresql://h/d']\n", [2]),
        ("POSTGRES_DB=herd\nHERD_TEST_PG_DSN=postgresql://h/d\n", [2]),
    ],
)
def test_config_scan(text: str, expected: list[int]):
    assert config_mentions(text) == expected
