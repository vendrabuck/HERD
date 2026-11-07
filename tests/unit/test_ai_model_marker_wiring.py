"""Static pin: the model-dependent AI integration tests run in their own invocation.

No stack, no provider, no Docker: this parses `tests/integration/` with `ast`, the
root `pyproject.toml` with `tomllib`, and the Makefile with the recipe scanner of
tests/unit/test_pg_live_gate_wiring.py (`recipe_commands`), so a name in a comment
or an `echo`, a command whose failure is dropped, and a command behind an `exit`
word are read the same way here as there.

Why (issue #1270): the assistant integration tests that judge a live model's own
tool-call choices run only on a host with an AI provider, where a small local
model does not make the same choice every time. `make test-integration` runs
pytest with `-x`, so one such miss stopped the whole integration suite and the
gate. Those tests now carry `@pytest.mark.ai_model`; `test-integration` leaves them
out and `test-integration-ai` runs them without fail-fast, and both gates run both
targets, so a model miss still fails the gate but no longer hides the rest.

What it pins:

- A test that needs the marker carries it. A test NEEDS it when it is a `test*`
  function (sync or async) in a `tests/integration/test_*.py` file, at module level
  or in a module-level `Test*` class, and its body (decorators excluded, nested
  functions included) both CALLS `ai_provider_configured` (as a bare name or as an
  attribute, `_ai_helpers.ai_provider_configured()`) and holds the string constant
  `"tool_calls"` anywhere (a subscript key `body["tool_calls"]`, `.get("tool_calls")`,
  `"tool_calls" in body`). Any read of that response field counts as asserting on
  it, by decision: marking a test that reads it without asserting costs only
  isolation. A test CARRIES the marker when one of its own decorators, a decorator
  of its enclosing class, or a module-level `pytestmark` (a single value, a list,
  or a tuple) is `pytest.mark.ai_model` or `mark.ai_model`, called or not. The
  non-owner 404 test calls `ai_provider_configured` but never reads `tool_calls`,
  so it needs no marker: its answer is decided before any model call.
- At least one integration test carries the marker. `pytest -m ai_model` that
  selects nothing exits 5, which would fail `test-integration-ai` and both gates on
  every host.
- `ai_model` is registered under `[tool.pytest.ini_options] markers` in the root
  `pyproject.toml` (`filterwarnings = ["error"]` turns an unregistered marker into a
  collection error).
- `test-integration` runs pytest over `tests/integration/` with the mark expression
  exactly `not ai_model` (`-m "not ai_model"` or `-m'not ai_model'`, quotes removed
  by the scanner; also `-mnot ai_model` attached), and no other `-m`.
- `test-integration-ai` runs pytest over `tests/integration/` with the mark
  expression exactly `ai_model`, no other `-m`, and no fail-fast option: `-x`, a
  short-flag cluster holding `x` (`-vx`), `--exitfirst`, `--maxfail`, `--stepwise`,
  `--sw`, `--stepwise-skip`, `--sw-skip` are refused.
- Each of those two pytest commands passes its failure on (no `;`, `||`, `|`, `&`
  after it, no `-` line prefix) and has no `exit` or `exec` word ahead of it.
- `master` and `everything` each call `test-integration` and, LATER in the recipe,
  `test-integration-ai`, through `$(MAKE)`, `$(SUBMAKE)`, or `make`; each call passes
  its failure on, has no `exit` or `exec` word ahead of it and no variable assigned
  ahead of it, and carries only its target, options that neither stop it running nor
  hide its failure, and `COMPOSE_PROJECT_NAME=<value>` (the gate project the
  neighbouring sub-makes pass). Neither gate may carry a target-specific `SHELL`.

Limits:

- The marker predicate reads the source only. A test that reads `tool_calls`
  through a helper defined elsewhere (the body never holds the string), or calls
  the gate through an alias (`configured = ai_provider_configured; configured()`),
  is not seen; a class-level `pytestmark` attribute is not read (such a test is
  reported as unmarked, a false red); a marker applied in a conftest hook
  (`pytest_collection_modifyitems`) is not seen (also a false red).
- A test that carries the marker without needing it is accepted: isolation is the
  only cost, and a test may need it for a reason this predicate does not encode.
- Mark expressions are compared as text after the scanner removes quotes, so an
  equivalent spelling (`not (ai_model)`) is refused.
- A short-flag cluster is judged only when it is made of the flags `-v -q -x -s -l`;
  any other cluster that hides an `x` (one ending in a flag that takes a value) is
  not parsed. `PYTEST_ADDOPTS`, `-c` or `--override-ini` pointing at another
  configuration, a conftest that deselects, and environment or command-line
  variables are not read (the pg-live guard's scanner Limits apply as well).
- The scanner's own Limits in test_pg_live_gate_wiring.py apply: a `trap`, a second
  definition of a target, `.IGNORE`, a global `SHELL`, `.SHELLFLAGS`, `MAKEFLAGS` in
  the Makefile, and an `include` are not seen.
"""

from __future__ import annotations

import ast
import tomllib

import pytest

from .test_pg_live_gate_wiring import (
    _ASSIGNMENT,
    _HARMLESS_MAKE_OPTION,
    _MAKE_TARGET_WORD,
    MAKEFILE_PATH,
    REPO_ROOT,
    ShellCommand,
    _pytest_invocation,
    recipe_commands,
    target_specific_shell_lines,
)

INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MARKER = "ai_model"
GATE_FUNCTION = "ai_provider_configured"
TOOL_CALLS_KEY = "tool_calls"
DETERMINISTIC_TARGET = "test-integration"
AI_TARGET = "test-integration-ai"
GATES = ("master", "everything")
INTEGRATION_PATHS = {"tests/integration", "tests/integration/"}
_MAKE_WORDS = {"${MAKE}", "${SUBMAKE}", "make"}
# The one variable a gate's integration sub-make may carry: the gate project.
_ALLOWED_CALL_VARIABLES = {"COMPOSE_PROJECT_NAME"}
_FAIL_FAST_LONG = {"--exitfirst", "--stepwise", "--sw", "--stepwise-skip", "--sw-skip"}


# --- The marker on the tests ---------------------------------------------------


def _is_marker(node: ast.expr) -> bool:
    """`pytest.mark.ai_model` or `mark.ai_model`, called or not."""
    if isinstance(node, ast.Call):
        node = node.func
    if not (isinstance(node, ast.Attribute) and node.attr == MARKER):
        return False
    owner = node.value
    if isinstance(owner, ast.Attribute):
        return owner.attr == "mark"
    return isinstance(owner, ast.Name) and owner.id == "mark"


def _module_marks(tree: ast.Module) -> bool:
    """Whether a module-level `pytestmark` holds the marker."""
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        value = statement.value
        values = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        if any(_is_marker(v) for v in values):
            return True
    return False


def _test_functions(
    tree: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, list[ast.expr]]]:
    """(qualified name, function, decorators of its enclosing class) for every test."""
    found = []
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name.startswith("test"):
                found.append((statement.name, statement, []))
        elif isinstance(statement, ast.ClassDef) and statement.name.startswith("Test"):
            for member in statement.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if member.name.startswith("test"):
                        found.append(
                            (f"{statement.name}.{member.name}", member, statement.decorator_list)
                        )
    return found


def _body_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef):
    for statement in function.body:
        yield from ast.walk(statement)


def _calls_the_gate(function) -> bool:
    for node in _body_nodes(function):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == GATE_FUNCTION:
                return True
            if isinstance(func, ast.Attribute) and func.attr == GATE_FUNCTION:
                return True
    return False


def _reads_tool_calls(function) -> bool:
    return any(
        isinstance(node, ast.Constant) and node.value == TOOL_CALLS_KEY
        for node in _body_nodes(function)
    )


def needs_marker(function) -> bool:
    """The predicate of the module docstring: calls the AI gate and reads tool_calls."""
    return _calls_the_gate(function) and _reads_tool_calls(function)


def scan_sources(sources: dict[str, str]) -> tuple[list[str], list[str]]:
    """(tests that need the marker and lack it, tests that carry it), each as
    `<file>::<name>`, for a mapping of file name to source."""
    unmarked: list[str] = []
    marked: list[str] = []
    for name, source in sorted(sources.items()):
        tree = ast.parse(source, filename=name)
        module_marked = _module_marks(tree)
        for qualified, function, class_decorators in _test_functions(tree):
            carries = (
                module_marked
                or any(_is_marker(d) for d in function.decorator_list)
                or any(_is_marker(d) for d in class_decorators)
            )
            if carries:
                marked.append(f"{name}::{qualified}")
            elif needs_marker(function):
                unmarked.append(f"{name}::{qualified}")
    return unmarked, marked


def _integration_sources() -> dict[str, str]:
    return {
        f"tests/integration/{path.name}": path.read_text()
        for path in sorted(INTEGRATION_DIR.glob("test_*.py"))
    }


# --- The Makefile ---------------------------------------------------------------


def _mark_expressions(arguments: tuple[str, ...]) -> list[str]:
    found = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-m" and index + 1 < len(arguments):
            found.append(arguments[index + 1])
            index += 1
        elif argument.startswith("-m") and len(argument) > 2:
            found.append(argument[2:])
        index += 1
    return found


def _fail_fast_options(arguments: tuple[str, ...]) -> list[str]:
    found = []
    previous = None
    for argument in arguments:
        if previous == "-m":
            previous = argument
            continue
        if argument in _FAIL_FAST_LONG or argument.startswith("--maxfail"):
            found.append(argument)
        elif (
            argument.startswith("-")
            and not argument.startswith("--")
            and set(argument[1:]) <= set("vqxsl")
            and "x" in argument
        ):
            found.append(argument)
        previous = argument
    return found


def pytest_run_problems(makefile_text: str, target: str, expression: str) -> list[str]:
    """Why the target does not run pytest over tests/integration/ with exactly the
    mark expression (and, for the AI target, without fail-fast); [] when it does."""
    try:
        commands = recipe_commands(makefile_text, target)
    except StopIteration:
        return [f"there is no {target} target"]
    runs = []
    for command in commands:
        invocation = _pytest_invocation(command)
        if invocation is None:
            continue
        _env, arguments, _assigned = invocation
        if INTEGRATION_PATHS & set(arguments):
            runs.append((command, arguments))
    if not runs:
        return [f"{target} runs no pytest over tests/integration/"]
    problems = []
    for command, arguments in runs:
        expressions = _mark_expressions(arguments)
        if expressions != [expression]:
            problems.append(
                f"{target}'s pytest mark expressions are {expressions}, expected "
                f"exactly [{expression!r}]"
            )
        if target == AI_TARGET and _fail_fast_options(arguments):
            problems.append(
                f"{target} stops at the first failure ({_fail_fast_options(arguments)}); "
                "every model-dependent test must report"
            )
        if not command.failure_fails_the_recipe:
            problems.append(f"{target}'s pytest failure does not fail the recipe")
        if command.after_an_exit:
            problems.append(f"an exit or exec word stands ahead of {target}'s pytest")
    if target_specific_shell_lines(makefile_text, target):
        problems.append(f"{target} has a target-specific SHELL")
    return problems


def _calls(command: ShellCommand, target: str) -> bool:
    """Whether a command is a sub-make that really runs `target` and passes its
    failure on: `$(MAKE)`, `$(SUBMAKE)`, or `make`, no variable ahead of it, then
    the target, harmless options, and COMPOSE_PROJECT_NAME=<value> only."""
    words = command.words
    if not words or words[0] not in _MAKE_WORDS or target not in words[1:] or command.env:
        return False
    for word in words[1:]:
        assignment = _ASSIGNMENT.match(word)
        if assignment:
            if assignment.group(1) not in _ALLOWED_CALL_VARIABLES:
                return False
        elif not (_HARMLESS_MAKE_OPTION.fullmatch(word) or _MAKE_TARGET_WORD.fullmatch(word)):
            return False
    return command.failure_fails_the_recipe and not command.after_an_exit


def gate_problems(makefile_text: str) -> list[str]:
    problems = []
    for gate in GATES:
        if target_specific_shell_lines(makefile_text, gate):
            problems.append(f"{gate} has a target-specific SHELL")
            continue
        commands = recipe_commands(makefile_text, gate)
        deterministic = [i for i, c in enumerate(commands) if _calls(c, DETERMINISTIC_TARGET)]
        ai = [i for i, c in enumerate(commands) if _calls(c, AI_TARGET)]
        if not deterministic:
            problems.append(f"{gate} does not run {DETERMINISTIC_TARGET}")
        if not ai:
            problems.append(f"{gate} does not run {AI_TARGET}")
        if deterministic and ai and max(ai) < min(deterministic):
            problems.append(f"{gate} runs {AI_TARGET} before {DETERMINISTIC_TARGET}")
    return problems


def all_makefile_problems(makefile_text: str) -> list[str]:
    return (
        pytest_run_problems(makefile_text, DETERMINISTIC_TARGET, f"not {MARKER}")
        + pytest_run_problems(makefile_text, AI_TARGET, MARKER)
        + gate_problems(makefile_text)
    )


def registered_markers(pyproject_text: str) -> set[str]:
    data = tomllib.loads(pyproject_text)
    entries = data["tool"]["pytest"]["ini_options"].get("markers", [])
    return {entry.split(":", 1)[0].split("(", 1)[0].strip() for entry in entries}


# --- The real tree ----------------------------------------------------------------


def test_every_model_dependent_integration_test_carries_the_marker():
    unmarked, _marked = scan_sources(_integration_sources())
    assert not unmarked, (
        f"{unmarked} call {GATE_FUNCTION}() and read the assistant's tool_calls, so they "
        f"judge a live model's own tool-call choices; mark them @pytest.mark.{MARKER} so "
        f"`make {DETERMINISTIC_TARGET}` (-x) leaves them out and `make {AI_TARGET}` runs "
        "them without fail-fast (issue #1270)"
    )


def test_the_ai_invocation_selects_at_least_one_test():
    _unmarked, marked = scan_sources(_integration_sources())
    assert marked, (
        f"no integration test carries @pytest.mark.{MARKER}, so `pytest -m {MARKER}` "
        f"selects nothing and exits 5, failing `make {AI_TARGET}` and both gates"
    )


def test_the_non_owner_404_test_needs_no_marker():
    """The deterministic AI-gated test stays in the -x suite: the predicate must not
    sweep in a test whose answer is decided before any model call."""
    source = (INTEGRATION_DIR / "test_ai_assistant_tools.py").read_text()
    tree = ast.parse(source)
    function = next(
        f
        for name, f, _ in _test_functions(tree)
        if name == "test_assistant_reservation_404_for_non_owner"
    )
    assert _calls_the_gate(function)
    assert not needs_marker(function)


def test_the_marker_is_registered():
    assert MARKER in registered_markers(PYPROJECT_PATH.read_text()), (
        f"register `{MARKER}` under [tool.pytest.ini_options] markers in pyproject.toml; "
        'filterwarnings = ["error"] makes an unregistered marker a collection error'
    )


def test_the_makefile_splits_the_integration_suite_and_both_gates_run_both_halves():
    problems = all_makefile_problems(MAKEFILE_PATH.read_text())
    assert not problems, problems


# --- Detector tests: the scans on synthetic input ------------------------------------

_GATED_READER = """
import pytest
from _ai_helpers import ai_provider_configured

{decorator}
async def test_reads_tool_calls(base_url):
    if not ai_provider_configured():
        pytest.skip("no provider")
    body = {{}}
    assert {read}
"""


@pytest.mark.parametrize(
    "read",
    [
        'body["tool_calls"]',
        'body.get("tool_calls")',
        '"tool_calls" in body',
        '[c for c in body["tool_calls"]]',
    ],
)
def test_scan_flags_every_spelling_of_a_tool_calls_read_without_the_marker(read: str):
    source = _GATED_READER.format(decorator="", read=read)
    assert scan_sources({"t.py": source})[0] == ["t.py::test_reads_tool_calls"]


@pytest.mark.parametrize(
    "decorator",
    ["@pytest.mark.ai_model", "@pytest.mark.ai_model()", "@mark.ai_model"],
)
def test_scan_accepts_every_spelling_of_the_marker_decorator(decorator: str):
    source = _GATED_READER.format(decorator=decorator, read='body["tool_calls"]')
    unmarked, marked = scan_sources({"t.py": source})
    assert unmarked == [] and marked == ["t.py::test_reads_tool_calls"]


@pytest.mark.parametrize(
    "pytestmark",
    [
        "pytestmark = pytest.mark.ai_model",
        "pytestmark = [pytest.mark.asyncio, pytest.mark.ai_model]",
        "pytestmark = (pytest.mark.ai_model,)",
    ],
)
def test_scan_accepts_a_module_level_pytestmark(pytestmark: str):
    source = _GATED_READER.format(decorator="", read='body["tool_calls"]') + pytestmark + "\n"
    assert scan_sources({"t.py": source})[0] == []


def test_scan_reads_methods_of_a_test_class_and_the_class_decorator():
    body = """
import pytest

class TestAssistant:
    async def test_one(self):
        if ai_provider_configured():
            assert {}["tool_calls"]

@pytest.mark.ai_model
class TestMarked:
    async def test_two(self):
        if ai_provider_configured():
            assert {}["tool_calls"]
"""
    unmarked, marked = scan_sources({"t.py": body})
    assert unmarked == ["t.py::TestAssistant.test_one"]
    assert marked == ["t.py::TestMarked.test_two"]


@pytest.mark.parametrize(
    "body",
    [
        # Reads tool_calls but never asks whether a provider is configured.
        'def test_x():\n    assert {}["tool_calls"]\n',
        # Asks, but never reads tool_calls (the non-owner 404 shape).
        "def test_x():\n    if not ai_provider_configured():\n        return\n    assert 1\n",
        # The string sits only in a decorator, not in the body.
        '@pytest.mark.parametrize("k", ["tool_calls"])\n'
        "def test_x(k):\n    assert ai_provider_configured()\n",
        # A helper, not a test.
        'def helper():\n    ai_provider_configured()\n    return {}["tool_calls"]\n',
    ],
)
def test_scan_does_not_require_the_marker_outside_the_predicate(body: str):
    assert scan_sources({"t.py": "import pytest\n" + body})[0] == []


_SOUND_MAKEFILE = """\
master: gate-clean
\t@trap '$(MAKE) _master-stack-down' EXIT INT TERM; \\
\t\t$(MAKE) _master-stack-up && \\
\t\t$(MAKE) test-integration COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\
\t\techo "" && echo "=== AI ===" && \\
\t\t$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\
\t\t$(MAKE) test-e2e COMPOSE_PROJECT_NAME=$(GATE_PROJECT)

everything: gate-clean
\t@ok=0; trap 'true' EXIT INT TERM; \\
\t\t$(MAKE) test-integration COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\
\t\t$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\
\t\tok=1

test-integration:  ## help
\tuv run pytest tests/integration/ -v --timeout=30 -x -m "not ai_model"

test-integration-ai:  ## help
\tuv run pytest tests/integration/ -v --timeout=30 -m ai_model
"""


def test_scan_accepts_the_sound_synthetic_makefile():
    assert all_makefile_problems(_SOUND_MAKEFILE) == []


@pytest.mark.parametrize(
    "spelling",
    [
        "-m 'not ai_model'",
        "-m'not ai_model'",
        '-m"not ai_model"',
    ],
)
def test_scan_accepts_every_quoting_of_the_exclusion(spelling: str):
    text = _SOUND_MAKEFILE.replace('-m "not ai_model"', spelling)
    assert all_makefile_problems(text) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # The exclusion dropped, or a different expression.
        (' -m "not ai_model"', ""),
        ('-m "not ai_model"', "-m ai_model"),
        ('-m "not ai_model"', '-m "not ai_model" -m slow'),
        # The AI run narrowed away, or made fail-fast.
        ("--timeout=30 -m ai_model", "--timeout=30"),
        ("--timeout=30 -m ai_model", "--timeout=30 -x -m ai_model"),
        ("--timeout=30 -m ai_model", "--timeout=30 -vx -m ai_model"),
        ("--timeout=30 -m ai_model", "--timeout=30 --exitfirst -m ai_model"),
        ("--timeout=30 -m ai_model", "--timeout=30 --maxfail=1 -m ai_model"),
        ("--timeout=30 -m ai_model", "--timeout=30 --sw -m ai_model"),
        # A pytest whose failure is dropped, or that runs behind an exit.
        ("-v --timeout=30 -m ai_model", "-v --timeout=30 -m ai_model || true"),
        (
            "\tuv run pytest tests/integration/ -v --timeout=30 -m ai_model",
            "\t-uv run pytest tests/integration/ -v --timeout=30 -m ai_model",
        ),
        (
            "\tuv run pytest tests/integration/ -v --timeout=30 -m ai_model",
            "\texit 0; uv run pytest tests/integration/ -v --timeout=30 -m ai_model",
        ),
        # The AI target missing, or naming another directory.
        ("test-integration-ai:  ## help\n", "test-integration-other:  ## help\n"),
        ("tests/integration/ -v --timeout=30 -m ai_model", "tests/e2e/ -v -m ai_model"),
    ],
)
def test_scan_refuses_a_broken_target(old: str, new: str):
    assert old in _SOUND_MAKEFILE
    text = _SOUND_MAKEFILE.replace(old, new, 1)
    assert all_makefile_problems(text), new


@pytest.mark.parametrize(
    ("gate_line", "replacement"),
    [
        # The AI call removed from master.
        (
            "\t\t$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
            "\t\t$(MAKE) test-e2e",
            "\t\t$(MAKE) test-e2e",
        ),
        # The AI call in everything made unable to fail the gate.
        (
            "\t\t$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n\t\tok=1",
            "\t\t{ $(MAKE) test-integration-ai || true; } && \\\n\t\tok=1",
        ),
        # A dry run, or a variable that changes what runs.
        (
            "$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
            "\t\t$(MAKE) test-e2e",
            "$(MAKE) -n test-integration-ai && \\\n\t\t$(MAKE) test-e2e",
        ),
        (
            "$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
            "\t\t$(MAKE) test-e2e",
            "$(MAKE) test-integration-ai SHELL=true && \\\n\t\t$(MAKE) test-e2e",
        ),
        # The AI call only in an echo.
        (
            "$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
            "\t\t$(MAKE) test-e2e",
            'echo "$(MAKE) test-integration-ai" && \\\n\t\t$(MAKE) test-e2e',
        ),
    ],
)
def test_gate_scan_refuses_a_gate_that_does_not_run_the_ai_half(gate_line: str, replacement: str):
    assert gate_line in _SOUND_MAKEFILE
    text = _SOUND_MAKEFILE.replace(gate_line, replacement, 1)
    assert gate_problems(text), replacement


def test_gate_scan_refuses_the_ai_half_before_the_deterministic_half():
    text = _SOUND_MAKEFILE.replace(
        "\t\t$(MAKE) test-integration COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
        "\t\t$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
        "\t\tok=1",
        "\t\t$(MAKE) test-integration-ai COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
        "\t\t$(MAKE) test-integration COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
        "\t\tok=1",
    )
    assert gate_problems(text) == ["everything runs test-integration-ai before test-integration"]


def test_registered_markers_reads_the_name_before_the_colon_and_any_argument():
    text = '[tool.pytest.ini_options]\nmarkers = ["seeded_skip_ok(reason): x", "ai_model: y"]\n'
    assert registered_markers(text) == {"seeded_skip_ok", "ai_model"}
    assert MARKER not in registered_markers('[tool.pytest.ini_options]\nmarkers = ["other: z"]\n')
