"""No test module reads the clock at import (issue #1561).

pytest imports every test module at collection, before the first test runs. A value
computed from the clock at import (`NOW = datetime.now(timezone.utc)` at module level,
and every window derived from it) is therefore as old, when a test reads it, as the
time the session has spent since collection. In #1561 the reservations suite booked
at such a `START`; the create schema refuses a start more than
`reservation_start_grace_seconds` (300) before now, and the suite took about 365 s on
the CI runner, so the tests that ran last answered 422 while every test passed on a
host that runs the suite in 150 s. A test reads the clock when it runs: in its body,
in a function fixture, or in an autouse `_clock` fixture that binds the module's
constants when each test starts (the pattern in `services/reservations/tests/`).

Pure AST, no imports of the scanned code: the scan reads every `*.py` file under
`services/*/tests/` and under the repo-root `tests/`, and fails on a clock read that
runs when the module is imported. A clock read is a call of:

- `now`, `utcnow`, or `today` on the `datetime` or `date` class, imported bare or under
  an alias (`from datetime import datetime as dt`), or reached through the module
  imported under any name (`import datetime as d` then `d.datetime.now()`);
- `time`, `time_ns`, `monotonic`, `monotonic_ns`, `perf_counter`, or `perf_counter_ns`
  of the `time` module imported under any name, or one of those functions imported
  from it bare or under an alias (`from time import time as t` then `t()`).

An import anywhere in the file binds the name for the whole file, a function-local
`import time` included; a relative import (`from .datetime import datetime`) and an
import from any other module bind nothing.

What runs at import: every module-level statement, including those inside a module-level
`if`, `try`, `with`, `for`, or `while`; a class body, with its decorators, bases, and
keywords; the decorators and default values of every function and lambda defined at
import; and every comprehension or generator expression there (a generator expression's
body runs only when it is consumed, so one is treated as running at import, a
conservative choice). A function BODY does not run at import, so a read there is not
flagged, unless the function is a module-level `def` that import-time code calls by
its bare name; then its body is scanned the same way, and so is every module-level
`def` it calls by bare name in turn. A lambda BODY is never treated as running at
import, not even when the lambda is called where it is defined (see Limits).

Limits. The scan reads the source; it does not run it:

- a clock function reached through another name is not seen: one bound by assignment
  (`now = datetime.now`), passed as a value (`functools.partial(datetime.now, tz)`), or
  called through an attribute of an object (`clock.now()` on a fake clock) or another
  library (`pendulum.now()`);
- a call into a function defined elsewhere (an imported helper that stamps a token or a
  row), a method called at import, or a class instantiated at import (its `__init__`)
  is not followed, only a module-level `def` of the same file called by bare name;
- argument and return annotations are not scanned;
- `time.localtime()`, `time.gmtime()`, `time.ctime()`, and `time.asctime()` with no
  argument also read the clock and are not in the set;
- the other clocks of the `time` module are not in the set: `process_time`,
  `process_time_ns`, `thread_time`, `thread_time_ns`, `clock_gettime`, and
  `clock_gettime_ns`, under any spelling;
- a star import (`from datetime import *`, `from time import *`) binds no name the scan
  follows, so a read through it is missed;
- a lambda that is called where it is defined, `(lambda: datetime.now())()`, is not
  followed: its body is never scanned;
- a value computed by a fixture of a broader scope than function (`scope="module"`,
  `"class"`, `"package"`, or `"session"`) at its first use goes stale the same way and
  is not scanned.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATETIME_READS = {"now", "utcnow", "today"}
DATETIME_CLASSES = {"datetime", "date"}
TIME_READS = {"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"}

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


class _Spellings:
    """The names a module binds to the datetime and time modules and their clocks."""

    def __init__(self, tree: ast.Module):
        self.datetime_modules: set[str] = set()
        self.datetime_classes: set[str] = set()
        self.time_modules: set[str] = set()
        self.time_functions: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "datetime":
                        self.datetime_modules.add(alias.asname or alias.name)
                    elif alias.name == "time":
                        self.time_modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    if node.module == "datetime" and alias.name in DATETIME_CLASSES:
                        self.datetime_classes.add(alias.asname or alias.name)
                    elif node.module == "time" and alias.name in TIME_READS:
                        self.time_functions.add(alias.asname or alias.name)

    def is_datetime_class(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in self.datetime_classes
        return (
            isinstance(expr, ast.Attribute)
            and expr.attr in DATETIME_CLASSES
            and isinstance(expr.value, ast.Name)
            and expr.value.id in self.datetime_modules
        )

    def is_clock_read(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self.time_functions
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr in DATETIME_READS and self.is_datetime_class(func.value):
            return True
        return (
            func.attr in TIME_READS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.time_modules
        )


def _import_time_nodes(roots: list[ast.AST]) -> list[ast.AST]:
    """Every node under `roots` that runs when the code holding them runs: function and
    lambda bodies are left out, their decorators and default values are kept."""
    found: list[ast.AST] = []
    stack = list(roots)
    while stack:
        node = stack.pop()
        found.append(node)
        if isinstance(node, _DEFS):
            stack.extend(node.decorator_list)
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
        elif isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
        else:
            stack.extend(ast.iter_child_nodes(node))
    return found


def find_import_time_clock_reads(source: str) -> list[int]:
    """Line numbers of the clock reads that run when this source is imported."""
    tree = ast.parse(source)
    spellings = _Spellings(tree)
    module_defs = {node.name: node for node in tree.body if isinstance(node, _DEFS)}
    hits: set[int] = set()
    followed: set[str] = set()
    pending: list[list[ast.AST]] = [list(tree.body)]
    while pending:
        for node in _import_time_nodes(pending.pop()):
            if not isinstance(node, ast.Call):
                continue
            if spellings.is_clock_read(node):
                hits.add(node.lineno)
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id in module_defs
                and node.func.id not in followed
            ):
                followed.add(node.func.id)
                pending.append(list(module_defs[node.func.id].body))
    return sorted(hits)


def _test_sources() -> list[Path]:
    roots = sorted((REPO_ROOT / "services").glob("*/tests")) + [REPO_ROOT / "tests"]
    return sorted(path for root in roots for path in root.rglob("*.py"))


def test_detector_flags_every_spelling_of_a_clock_read():
    src = (
        "import datetime as d\n"
        "import time\n"
        "import time as t\n"
        "from datetime import date, datetime, timezone\n"
        "from datetime import datetime as dt\n"
        "from time import monotonic as mono, time as wall\n"
        "NOW = datetime.now(timezone.utc)\n"
        "A = dt.now()\n"
        "B = d.datetime.utcnow()\n"
        "C = date.today()\n"
        "D = d.date.today()\n"
        "E = time.time()\n"
        "F = t.perf_counter_ns()\n"
        "G = wall()\n"
        "H = mono()\n"
        "I = datetime.today()\n"
        "J = time.time_ns() + time.monotonic_ns() + time.perf_counter()\n"
    )
    assert find_import_time_clock_reads(src) == [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]


def test_detector_flags_every_place_that_runs_at_import():
    src = (
        "from datetime import datetime\n"
        "def f(x=datetime.now()):\n"
        "    pass\n"
        "async def g(*, y=datetime.now()):\n"
        "    pass\n"
        "@pytest.mark.parametrize('when', [datetime.now()])\n"
        "def test_p(when):\n"
        "    pass\n"
        "class C(Base, at=datetime.now()):\n"
        "    STAMP = datetime.now()\n"
        "    def m(self, z=datetime.now()):\n"
        "        pass\n"
        "late = lambda w=datetime.now(): w\n"
        "if True:\n"
        "    T = datetime.now()\n"
        "try:\n"
        "    U = datetime.now()\n"
        "except Exception:\n"
        "    pass\n"
        "ROWS = [datetime.now() for _ in range(2)]\n"
        "LAZY = (datetime.now() for _ in range(2))\n"
        "@decorate(datetime.now())\n"
        "class D:\n"
        "    pass\n"
    )
    assert find_import_time_clock_reads(src) == [2, 4, 6, 9, 10, 11, 13, 15, 17, 20, 21, 22]


def test_detector_follows_module_level_functions_called_at_import():
    src = (
        "from datetime import datetime\n"
        "def _stamp():\n"
        "    return datetime.now()\n"
        "def _outer():\n"
        "    return _stamp()\n"
        "def _never_called_at_import():\n"
        "    return datetime.now()\n"
        "def _called_twice():\n"
        "    return datetime.utcnow()\n"
        "TOKEN = _outer()\n"
        "A, B = _called_twice(), _called_twice()\n"
        "@pytest.mark.parametrize('x', _cases())\n"
        "def test_x(x):\n"
        "    _never_called_at_import()\n"
        "def _cases():\n"
        "    return [datetime.today()]\n"
    )
    assert find_import_time_clock_reads(src) == [3, 9, 16]


def test_detector_ignores_reads_that_run_when_a_test_runs():
    src = (
        "import datetime as d\n"
        "import time\n"
        "from datetime import datetime, timedelta, timezone\n"
        "from sqlalchemy import func\n"
        "FIXED = datetime(2026, 5, 1, tzinfo=timezone.utc)\n"
        "LATER = FIXED + timedelta(hours=3)\n"
        "@pytest.fixture(autouse=True)\n"
        "def _clock():\n"
        "    global NOW\n"
        "    NOW = datetime.now(timezone.utc)\n"
        "async def test_body():\n"
        "    assert time.time() > 0\n"
        "    inner = lambda: d.datetime.now()\n"
        "    def nested():\n"
        "        return datetime.now()\n"
        "class TestGroup:\n"
        "    def test_method(self):\n"
        "        return datetime.now()\n"
        "late = lambda: datetime.now()\n"
        "FACTORY = Field(default_factory=datetime.now)\n"
        "column = Column(server_default=func.now())\n"
        "CLOCK.now()\n"
        "FIXED.time()\n"
        "FIXED.today()\n"
        "pendulum.today()\n"
        "def helper():\n"
        "    return datetime.now()\n"
        "REF = helper\n"
        "obj.helper()\n"
    )
    assert find_import_time_clock_reads(src) == []


def test_detector_does_not_treat_a_relative_or_other_module_as_the_clock():
    src = (
        "from .datetime import datetime\n"
        "from mylib import time\n"
        "from freezer import now\n"
        "A = datetime.now()\n"
        "B = time()\n"
        "C = now()\n"
    )
    assert find_import_time_clock_reads(src) == []


def test_the_scan_reads_the_suites_it_names():
    sources = _test_sources()
    named = {path.relative_to(REPO_ROOT).parts[:3] for path in sources}
    assert ("services", "reservations", "tests") in named
    assert ("services", "common", "tests") in named
    assert any(path.relative_to(REPO_ROOT).parts[:2] == ("tests", "unit") for path in sources)
    assert Path(__file__).resolve() in sources


def test_no_test_module_reads_the_clock_at_import():
    offenders = []
    for path in _test_sources():
        for line in find_import_time_clock_reads(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert offenders == [], (
        "These clock reads run when pytest imports the module, so the value is as old "
        "as the session when a test reads it (issue #1561). Read the clock when the "
        "test runs: in the test, in a function fixture, or in an autouse `_clock` "
        "fixture that binds the module's constants per test.\n" + "\n".join(offenders)
    )
