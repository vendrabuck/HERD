"""Static pin: the OIDC-mode stack phase really runs its three suites (ADR 0016 phase 5b).

No stack, no Docker, no network: this parses the Makefile, `.github/workflows/
nightly.yml`, and the three suites' source (as text, with `ast`; nothing is
imported, so no conftest, gevent, or locust import runs), reusing the recipe
scanner of tests/unit/test_pg_live_gate_wiring.py (`recipe_commands`) and the
workflow reader of tests/unit/test_gate_ci_parity_wiring.py (`shell_commands`).

Why: each suite selects itself with an environment variable and skips without
it, which is what keeps the ordinary local-mode runs green, and also what would
hide a broken phase. A misspelled `HERD_E2E_OIDC` in the recipe gives three
skips and exit 0, even under HERD_E2E_REQUIRE_NO_SKIP=1, because the module's
skip carries `seeded_skip_ok`; a misspelled `HERD_INTEGRATION_OIDC` gives seven
skips and exit 0. Nothing in the Makefile counts passed tests (the repository's
pattern is a selector or REQUIRED variable that turns a skip into a failure,
which these suites already follow once selected), so this guard is the
protection against an unselected suite.

The names come from the suites, not from this file. Each suite declares the
variables it reads as module-level string constants, and this guard reads them
with `ast`:

- `tests/integration/test_oidc_auth.py`: SELECTOR_ENV, OIDC_HOST_ENV, OIDC_PORT_ENV;
- `tests/e2e/test_oidc_login_playwright.py`: SELECTOR_ENV, plus
  `tests/e2e/conftest.py`'s OIDC_TEST_HOST_ENV (pw_oidc_browser's mapping);
- `tests/load/locustfile.py`: OIDC_LOAD_SELECTOR_ENV, OIDC_TEST_HOST_ENV,
  OIDC_TEST_PORT_ENV.

A constant counts only when it is a plain `NAME = "literal"` at module level AND
its name is the first argument of an `os.getenv(...)` or `os.environ.get(...)`
call in the same file, so a suite that goes back to reading a literal leaves its
constant unread and fails here.

What is pinned in the `_gate-oidc-stack-tests` recipe (commands as the scanner
splits them; a name in a comment or an `echo` is not a command):

- a `docker compose up` command naming `auth` with AUTH_METHOD=oidc and
  OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=false assigned on it, and a
  `trap restore ... EXIT ...` command ahead of it;
- after that recreate, a pytest command naming the integration file and one
  naming the e2e file (resolved against the directory the command runs in),
  each assigning EXACTLY its suite's names (no fewer, no more), the selector set
  to `1`, its failure failing the recipe, and no argument that narrows the run
  (`-k`, `-m`, `--co`, `--collect-only`, `--deselect`, `--ignore`, `--lf`,
  `--last-failed`, or a node id holding `::`);
- after that recreate, a locust command whose `-f` file resolves to the
  locustfile, naming the class `OidcLoginUser` (which must exist in that file),
  with `--headless`, assigning exactly the class's names, the selector `1`.

In `master` and `everything`: a `$(MAKE)`, `$(SUBMAKE)`, or `make` command naming
the phase, after the one naming `_gate-ldap-stack-tests`, whose failure fails the
recipe and which carries none of `-n`, `-q`, `-t`, `-i`, `--dry-run`, `--just-print`,
`--recon`, `--question`, `--touch`, `--ignore-errors` as a separate word (a bundled
short option such as `-in` is not judged). In nightly.yml's full-stack job: a step running
`make oidc-up` before the step running `make _gate-oidc-stack-tests`, which comes
after the step running `make _gate-ldap-stack-tests` and before the step running
`make test-e2e-seeded`, carries no `if:` and no `continue-on-error`, and whose
make command's failure fails the step; the diagnostics step (running
`make _collect-stack-diagnostics`) runs `docker compose -p herd-oidc-test ...
logs`; and a step running `make oidc-down` with `if: always()` comes after the
diagnostics step.

The detector tests at the bottom attack the scan in both directions against the
real files: broken edits (a renamed or zeroed selector, a dropped or narrowed
suite, a swallowed failure, a suite reading a literal again, a constant renamed
in a suite, the gate call dropped or moved before the LDAP phase, the nightly
step dropped, made optional, or moved, the diagnostics moved after the stop, the
stop made conditional) must each be reported; honest edits (a reflowed comment,
a comment line inside the recipe, reordered assignments on one command,
reordered unrelated nightly steps) must not.

Limits:

- The scanner's own Limits (test_pg_live_gate_wiring.py) apply: it reads the
  shell with a small tokenizer, not a shell.
- The locust command sits inside the `if` that OIDC_STACK_LOAD selects, and the
  scanner does not read through an `if`, so whether the load run's failure fails
  the recipe is NOT judged (as written, `if ... fi &&` passes it on), nor whether
  the `if` can be true; the master gate never runs it by design.
- An `exit` word ahead of a command is not judged (the recipe's own error
  branches hold several); a `-` line prefix, `;`, `||`, `|`, or `&` after a suite
  command is.
- Variables the suites read through the shared conftest or the Makefile's global
  exports (HERD_BASE_URL, E2E_HOST_BASE_URL, POSTGRES_USER) are not pinned here.
- Nightly steps are matched by the make command in their `run:` block, not by
  name; a step's `env:` block is not read.
"""

from __future__ import annotations

import ast
import posixpath

import yaml

from .test_gate_ci_parity_wiring import shell_commands
from .test_pg_live_gate_wiring import MAKEFILE_PATH, REPO_ROOT, ShellCommand, recipe_commands

PHASE = "_gate-oidc-stack-tests"
LDAP_PHASE = "_gate-ldap-stack-tests"
GATES = ("master", "everything")
NIGHTLY_PATH = REPO_ROOT / ".github" / "workflows" / "nightly.yml"
NIGHTLY_JOB = "full-stack"

INTEGRATION = "tests/integration/test_oidc_auth.py"
E2E = "tests/e2e/test_oidc_login_playwright.py"
E2E_CONFTEST = "tests/e2e/conftest.py"
LOCUSTFILE = "tests/load/locustfile.py"
LOAD_CLASS = "OidcLoginUser"

# suite: ((file, constant names), ...), and the constant holding the selector.
SUITE_CONSTANTS = {
    INTEGRATION: ((INTEGRATION, ("SELECTOR_ENV", "OIDC_HOST_ENV", "OIDC_PORT_ENV")),),
    E2E: ((E2E, ("SELECTOR_ENV",)), (E2E_CONFTEST, ("OIDC_TEST_HOST_ENV",))),
    LOCUSTFILE: (
        (LOCUSTFILE, ("OIDC_LOAD_SELECTOR_ENV", "OIDC_TEST_HOST_ENV", "OIDC_TEST_PORT_ENV")),
    ),
}
SELECTOR_CONSTANT = {
    INTEGRATION: (INTEGRATION, "SELECTOR_ENV"),
    E2E: (E2E, "SELECTOR_ENV"),
    LOCUSTFILE: (LOCUSTFILE, "OIDC_LOAD_SELECTOR_ENV"),
}
SOURCE_FILES = (INTEGRATION, E2E, E2E_CONFTEST, LOCUSTFILE)

_MAKE_WORDS = ("${MAKE}", "${SUBMAKE}", "make")
_STOPPING_MAKE_OPTIONS = {
    "-n",
    "-q",
    "-t",
    "-i",
    "--dry-run",
    "--just-print",
    "--recon",
    "--question",
    "--touch",
    "--ignore-errors",
}
_NARROWING = ("-k", "-m", "--co", "--collect-only", "--deselect", "--ignore", "--lf")
_NARROWING += ("--last-failed",)


def real_sources() -> dict[str, str]:
    return {path: (REPO_ROOT / path).read_text() for path in SOURCE_FILES}


# --- the suites' own names ---


def _is_env_read(func: ast.expr) -> bool:
    """`os.getenv` or `os.environ.get`."""
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv":
        return isinstance(func.value, ast.Name) and func.value.id == "os"
    return (
        func.attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "os"
    )


def read_constants(source: str, names: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
    """The values of the module-level string constants `names`, and the problems:
    a constant missing or not a plain string literal, or never read through
    os.getenv / os.environ.get by name in this source."""
    tree = ast.parse(source)
    values: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values[node.targets[0].id] = node.value.value
    read = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Name)
        and _is_env_read(node.func)
    }
    problems = [
        f"{name} is not a module-level string constant" for name in names if name not in values
    ]
    problems += [
        f"{name} is never read through os.getenv or os.environ.get"
        for name in names
        if name in values and name not in read
    ]
    return values, problems


def suite_names(sources: dict[str, str]) -> tuple[dict[str, tuple[str, set[str]]], list[str]]:
    """suite: (its selector's name, every name it reads), and the problems."""
    result: dict[str, tuple[str, set[str]]] = {}
    problems: list[str] = []
    for suite, parts in SUITE_CONSTANTS.items():
        names: set[str] = set()
        selector = ""
        for path, constants in parts:
            values, found = read_constants(sources[path], constants)
            problems += [f"{path}: {problem}" for problem in found]
            names |= set(values.values())
            if SELECTOR_CONSTANT[suite] == (path, SELECTOR_CONSTANT[suite][1]):
                selector = values.get(SELECTOR_CONSTANT[suite][1], selector)
        result[suite] = (selector, names)
    return result, problems


def _load_class_exists(source: str) -> bool:
    return any(
        isinstance(node, ast.ClassDef) and node.name == LOAD_CLASS
        for node in ast.parse(source).body
    )


# --- the recipe ---


def _env_problems(command: ShellCommand, selector: str, names: set[str], what: str) -> list[str]:
    env = dict(command.env)
    problems = []
    if set(env) != names:
        missing = sorted(names - set(env))
        extra = sorted(set(env) - names)
        problems.append(
            f"{what} assigns {sorted(env)}, not exactly the names the suite reads "
            f"{sorted(names)} (missing {missing}, unread {extra})"
        )
    if selector and env.get(selector) != "1":
        problems.append(f"{what} does not set {selector}=1")
    return problems


def _pytest_file(command: ShellCommand) -> str | None:
    words = command.words
    if "pytest" not in words:
        return None
    files = [w for w in words[words.index("pytest") + 1 :] if w.endswith(".py")]
    if len(files) != 1:
        return None
    return posixpath.normpath(posixpath.join(command.cwd, files[0]))


def phase_problems(makefile_text: str, sources: dict[str, str]) -> list[str]:
    names, problems = suite_names(sources)
    if not _load_class_exists(sources[LOCUSTFILE]):
        problems.append(f"{LOCUSTFILE} defines no class {LOAD_CLASS}")
    commands = recipe_commands(makefile_text, PHASE)
    recreate = next(
        (
            i
            for i, c in enumerate(commands)
            if c.words[:3] == ("docker", "compose", "up")
            and "auth" in c.words
            and dict(c.env).get("AUTH_METHOD") == "oidc"
        ),
        None,
    )
    if recreate is None:
        return [*problems, "no `docker compose up ... auth` with AUTH_METHOD=oidc in the recipe"]
    if dict(commands[recreate].env).get("OIDC_BREAK_GLASS_LOCAL_SUPERADMIN") != "false":
        problems.append("the recreate does not set OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=false")
    if not any(
        c.words[:2] == ("trap", "restore") and "EXIT" in c.words for c in commands[:recreate]
    ):
        problems.append("no `trap restore ... EXIT` ahead of the recreate")
    after = commands[recreate + 1 :]
    for suite in (INTEGRATION, E2E):
        selector, wanted = names[suite]
        runs = [c for c in after if _pytest_file(c) == suite]
        if not runs:
            problems.append(f"no pytest command runs {suite} after the recreate")
        for run in runs:
            problems += _env_problems(run, selector, wanted, f"the pytest command for {suite}")
            if not run.failure_fails_the_recipe:
                problems.append(f"the failure of the pytest command for {suite} does not fail")
            narrowing = [w for w in run.words if w.startswith(_NARROWING) or "::" in w]
            if narrowing:
                problems.append(f"the pytest command for {suite} narrows the run: {narrowing}")
    selector, wanted = names[LOCUSTFILE]
    loads = [
        c
        for c in after
        if "locust" in c.words
        and "-f" in c.words[:-1]
        and posixpath.normpath(posixpath.join(c.cwd, c.words[c.words.index("-f") + 1]))
        == LOCUSTFILE
    ]
    if not loads:
        problems.append(f"no locust command runs {LOCUSTFILE} after the recreate")
    for load in loads:
        problems += _env_problems(load, selector, wanted, "the locust command")
        if LOAD_CLASS not in load.words:
            problems.append(f"the locust command does not name {LOAD_CLASS}")
        if "--headless" not in load.words:
            problems.append("the locust command is not --headless")
    return problems


def gate_problems(makefile_text: str) -> list[str]:
    problems = []
    for gate in GATES:
        commands = recipe_commands(makefile_text, gate)

        def calls(target: str) -> list[int]:
            return [
                i
                for i, c in enumerate(commands)
                if c.words[:1] and c.words[0] in _MAKE_WORDS and target in c.words[1:]
            ]

        ldap, oidc = calls(LDAP_PHASE), calls(PHASE)
        if not oidc:
            problems.append(f"{gate} does not call {PHASE}")
            continue
        if not ldap or min(oidc) < min(ldap):
            problems.append(f"{gate} does not call {PHASE} after {LDAP_PHASE}")
        for index in oidc:
            call = commands[index]
            if not call.failure_fails_the_recipe:
                problems.append(f"{gate}'s call of {PHASE} can fail without failing {gate}")
            stopping = sorted(_STOPPING_MAKE_OPTIONS & set(call.words))
            if stopping:
                problems.append(f"{gate}'s call of {PHASE} carries {stopping}")
    return problems


# --- nightly.yml ---


def _step_index(steps: list[dict], words: tuple[str, ...]) -> list[int]:
    return [
        i
        for i, step in enumerate(steps)
        if any(c.words[: len(words)] == words for c in shell_commands(str(step.get("run", ""))))
    ]


def nightly_problems(nightly_text: str) -> list[str]:
    steps = yaml.safe_load(nightly_text)["jobs"][NIGHTLY_JOB]["steps"]
    found = {
        key: _step_index(steps, words)
        for key, words in {
            "start": ("make", "oidc-up"),
            "stack": ("make", PHASE),
            "ldap": ("make", LDAP_PHASE),
            "seeded": ("make", "test-e2e-seeded"),
            "diagnostics": ("make", "_collect-stack-diagnostics"),
            "stop": ("make", "oidc-down"),
        }.items()
    }
    problems = [f"nightly.yml has no step running {key!r}" for key, idx in found.items() if not idx]
    if problems:
        return problems
    start, stack, ldap, seeded = (found[k][0] for k in ("start", "stack", "ldap", "seeded"))
    diagnostics, stop = found["diagnostics"][0], found["stop"][0]
    if not start < stack:
        problems.append("the OIDC lab starts after the OIDC-mode stack step")
    if not ldap < stack < seeded:
        problems.append(
            "the OIDC-mode stack step is not between the LDAP-mode step and the seeded e2e"
        )
    step = steps[stack]
    if "if" in step or step.get("continue-on-error"):
        problems.append("the OIDC-mode stack step is conditional or may fail without failing")
    calls = [c for c in shell_commands(str(step["run"])) if c.words[:2] == ("make", PHASE)]
    if not all(c.failure_fails_the_recipe for c in calls):
        problems.append("the OIDC-mode stack step's make command can fail without failing it")
    if not any(
        c.words[:4] == ("docker", "compose", "-p", "herd-oidc-test") and "logs" in c.words
        for c in shell_commands(str(steps[diagnostics]["run"]))
    ):
        problems.append("the diagnostics step does not collect the OIDC lab's logs")
    if not diagnostics < stop:
        problems.append("the OIDC lab is stopped before the diagnostics are collected")
    if str(steps[stop].get("if", "")).replace(" ", "") != "always()":
        problems.append("the OIDC lab's stop step is not `if: always()`")
    return problems


# --- the real tree ---


def test_the_phase_runs_each_suite_with_exactly_the_names_it_reads():
    assert phase_problems(MAKEFILE_PATH.read_text(), real_sources()) == []


def test_master_and_everything_run_the_phase_after_the_ldap_phase():
    assert gate_problems(MAKEFILE_PATH.read_text()) == []


def test_nightly_starts_runs_and_stops_the_phase_with_diagnostics_first():
    assert nightly_problems(NIGHTLY_PATH.read_text()) == []


def test_the_suites_name_the_selectors_the_phase_sets():
    names, problems = suite_names(real_sources())
    assert problems == []
    assert {suite: selector for suite, (selector, _) in names.items()} == {
        INTEGRATION: "HERD_INTEGRATION_OIDC",
        E2E: "HERD_E2E_OIDC",
        LOCUSTFILE: "HERD_LOAD_OIDC",
    }


# --- detectors: the scans against edited copies of the real files ---


def _edit(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"anchor found {text.count(old)} times: {old!r}"
    return text.replace(old, new)


_INTEGRATION_RUN = "uv run pytest tests/integration/test_oidc_auth.py -v --timeout=60"
_E2E_RUN = "uv run pytest tests/e2e/test_oidc_login_playwright.py -v --tb=short"

BROKEN_RECIPES = {
    "integration selector misspelled": (
        "\tHERD_INTEGRATION_OIDC=1 \\",
        "\tHERD_INTEGRATON_OIDC=1 \\",
    ),
    "e2e selector misspelled": ("\tHERD_E2E_OIDC=1 \\", "\tHERD_E2E_ODIC=1 \\"),
    "e2e selector zero": ("\tHERD_E2E_OIDC=1 \\", "\tHERD_E2E_OIDC=0 \\"),
    "load selector misspelled": ("HERD_LOAD_OIDC=1 \\", "HERD_LOAD_ODIC=1 \\"),
    "integration file dropped": (_INTEGRATION_RUN, "true"),
    "e2e file dropped": (_E2E_RUN, "uv run pytest tests/e2e/ -v --tb=short"),
    "integration narrowed": (_INTEGRATION_RUN, _INTEGRATION_RUN + " -k nothing"),
    "e2e failure swallowed": (_E2E_RUN, _E2E_RUN + " || true"),
    "load class dropped": ("locust -f locustfile.py OidcLoginUser", "locust -f locustfile.py"),
    "lab host dropped from the integration run": (
        "\t\tHERD_TEST_OIDC_HOST=$(HERD_TEST_OIDC_HOST) \\\n"
        "\t\tHERD_TEST_OIDC_PORT=$(HERD_TEST_OIDC_PORT) \\\n"
        "\t\tuv run pytest tests/integration",
        "\t\tHERD_TEST_OIDC_PORT=$(HERD_TEST_OIDC_PORT) \\\n\t\tuv run pytest tests/integration",
    ),
    "unread name set on the e2e run": (
        "\tHERD_E2E_OIDC=1 \\",
        "\tHERD_E2E_OIDC=1 HERD_TEST_OIDC_PORT=$(HERD_TEST_OIDC_PORT) \\",
    ),
    "break-glass on": (
        "OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=false",
        "OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=true",
    ),
    "recreated in local mode": ("\tAUTH_METHOD=oidc \\", "\tAUTH_METHOD=local \\"),
}

BROKEN_SOURCES = {
    "integration constant renamed": (
        INTEGRATION,
        'SELECTOR_ENV = "HERD_INTEGRATION_OIDC"',
        'SELECTOR_ENV = "HERD_INTEGRATION_OIDC_X"',
    ),
    "integration reads a literal again": (
        INTEGRATION,
        "os.getenv(SELECTOR_ENV)",
        'os.getenv("HERD_INTEGRATION_OIDC")',
    ),
    "e2e selector constant gone": (
        E2E,
        'SELECTOR_ENV = "HERD_E2E_OIDC"',
        'SELECTOR = "HERD_E2E_OIDC"',
    ),
    "load class renamed": (LOCUSTFILE, "class OidcLoginUser(", "class OidcSignInUser("),
}

_MASTER_CALL = "\t\t$(MAKE) _gate-oidc-stack-tests COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"


def test_each_broken_recipe_is_reported():
    real = MAKEFILE_PATH.read_text()
    for name, (old, new) in BROKEN_RECIPES.items():
        assert phase_problems(_edit(real, old, new), real_sources()), name


def test_each_broken_suite_source_is_reported():
    real = MAKEFILE_PATH.read_text()
    for name, (path, old, new) in BROKEN_SOURCES.items():
        sources = real_sources()
        sources[path] = _edit(sources[path], old, new)
        assert phase_problems(real, sources), name


def test_a_dropped_or_early_gate_call_is_reported():
    real = MAKEFILE_PATH.read_text()
    oidc_header = (
        '\t\techo "" && echo "=== OIDC-mode stack tests (gate auth switched to OIDC) ===" && \\\n'
    )
    dropped = _edit(real, oidc_header + _MASTER_CALL, "")
    assert any(p.startswith("master does not call") for p in gate_problems(dropped))
    ldap_call = "\t\t$(MAKE) _gate-ldap-stack-tests COMPOSE_PROJECT_NAME=$(GATE_PROJECT) && \\\n"
    swapped = _edit(real, ldap_call + oidc_header + _MASTER_CALL, _MASTER_CALL + ldap_call)
    assert "master does not call _gate-oidc-stack-tests after _gate-ldap-stack-tests" in (
        gate_problems(swapped)
    )
    dry = _edit(real, _MASTER_CALL, _MASTER_CALL.replace("$(MAKE) ", "$(MAKE) -n "))
    assert gate_problems(dry)


def _nightly_without(text: str, name: str) -> tuple[str, str]:
    """The text with step `name` removed, and the removed block."""
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name:", start + 1)
    end = len(text) if end == -1 else end + 1
    # Any comment block directly above the next step stays where it is.
    return text[:start] + text[end:], text[start:end]


def test_each_broken_nightly_is_reported():
    real = NIGHTLY_PATH.read_text()
    no_stack, _ = _nightly_without(real, "OIDC-mode stack tests")
    no_stop, _ = _nightly_without(real, "Stop OIDC test provider")
    optional = _edit(
        real,
        "      - name: OIDC-mode stack tests\n",
        "      - name: OIDC-mode stack tests\n        continue-on-error: true\n",
    )
    conditional_stop = _edit(
        real,
        "      - name: Stop OIDC test provider\n        if: always()\n",
        "      - name: Stop OIDC test provider\n        if: success()\n",
    )
    without_stop, stop_block = _nightly_without(real, "Stop OIDC test provider")
    stop_first = _edit(
        without_stop,
        "      - name: Collect stack diagnostics\n",
        stop_block + "      - name: Collect stack diagnostics\n",
    )
    no_lab_logs = _edit(
        real,
        "          docker compose -p herd-oidc-test -f infra/oidc-test/docker-compose.yml \\\n"
        "            logs --tail 400 > oidc-lab-logs.txt 2>&1 || true\n",
        "",
    )
    swallowed = _edit(
        real,
        "run: make _gate-oidc-stack-tests OIDC_STACK_LOAD=1",
        "run: make _gate-oidc-stack-tests OIDC_STACK_LOAD=1 || true",
    )
    for name, text in {
        "stack step dropped": no_stack,
        "stop step dropped": no_stop,
        "stack step continue-on-error": optional,
        "stop step conditional": conditional_stop,
        "stop before diagnostics": stop_first,
        "diagnostics without the lab's logs": no_lab_logs,
        "stack step failure swallowed": swallowed,
    }.items():
        assert nightly_problems(text), name


def test_honest_edits_pass():
    real = MAKEFILE_PATH.read_text()
    reflowed = _edit(
        real,
        "# Gate phase used by master and everything right after _gate-ldap-stack-tests,\n",
        "# Gate phase used by master and everything right\n# after _gate-ldap-stack-tests,\n",
    )
    noted = _edit(
        real,
        '\techo "" && echo "=== Recreating gate auth in OIDC mode ===" && \\\n',
        "# A note between recipe lines.\n"
        '\techo "" && echo "=== Recreating gate auth in OIDC mode ===" && \\\n',
    )
    reordered = _edit(
        real,
        "\tHERD_INTEGRATION_OIDC=1 \\\n"
        "\t\tHERD_TEST_OIDC_HOST=$(HERD_TEST_OIDC_HOST) \\\n"
        "\t\tHERD_TEST_OIDC_PORT=$(HERD_TEST_OIDC_PORT) \\\n",
        "\tHERD_TEST_OIDC_PORT=$(HERD_TEST_OIDC_PORT) \\\n"
        "\t\tHERD_TEST_OIDC_HOST=$(HERD_TEST_OIDC_HOST) \\\n"
        "\t\tHERD_INTEGRATION_OIDC=1 \\\n",
    )
    for text in (reflowed, noted, reordered):
        assert phase_problems(text, real_sources()) == []
        assert gate_problems(text) == []
    nightly = NIGHTLY_PATH.read_text()
    without_ldap, ldap_block = _nightly_without(nightly, "Start LDAP test server")
    swapped = _edit(
        without_ldap,
        "      - name: Generate the gateway TLS certificate\n",
        ldap_block + "      - name: Generate the gateway TLS certificate\n",
    )
    recommented = nightly.replace(
        "      # After the LDAP-mode phase, which restored local mode, and before the\n",
        "      # After the LDAP-mode phase (which restored local mode) and before the\n",
    )
    for text in (swapped, recommented):
        assert nightly_problems(text) == []
