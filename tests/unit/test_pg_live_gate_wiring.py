"""Static pin: every live-Postgres suite is actually run by the gate phase.

No database, no stack, no Docker: this only parses the Makefile, in the style of
tests/unit/test_nos_lab_ci_wiring.py.

A `*_live_pg.py` suite skips itself when no Postgres is reachable, which is what
makes it safe to leave in the tree, and also what makes it invisible when nobody
wires it up: it would be written, reviewed, merged, and then silently never run.
The `_gate-pg-live-tests` phase of `make master` and `make everything` is the only
place these suites execute with HERD_TEST_PG_REQUIRED=1, so membership in that
recipe is the thing worth pinning.

Membership means the BEHAVIOR, not the file name appearing somewhere near the
target (issue #1158). A suite counts only when the recipe holds a pytest command
that names the file, runs from the suite's own service directory, carries
HERD_TEST_PG_REQUIRED=1 on that command, selects nothing narrower than the whole
file, and sits where its failure fails the recipe. A name in a comment (after the
recipe, between its lines, or on a commented-out line), in an `echo`, or on a
command whose status is dropped does not count; the detector tests below feed each
of those spellings to the same scan the real-tree tests use.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
SERVICES_DIR = REPO_ROOT / "services"

GATE_TARGET = "_gate-pg-live-tests"
# The gates that must call the phase; master-clean and everything-noload reach it
# through these two.
GATES = ("master", "everything")

# A simple `$(NAME)` or `${NAME}` make variable; kept as one shell word.
_MAKE_VARIABLE = re.compile(r"(?<!\$)\$[({]([A-Za-z_][A-Za-z0-9_.-]*)[)}]")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_TWO_CHAR_OPERATORS = ("&&", "||", ";;", ">>", "<<", ">&", "<&", "&>", "|&")
# Words that open and close a shell compound command, whose inner `;` separators
# do not end the list the compound sits in.
_COMPOUND_OPEN = {"if", "for", "while", "until", "case"}
_COMPOUND_CLOSE = {"fi", "done", "esac"}
# pytest options that select less than the whole file, or run nothing.
_NOT_A_FULL_RUN = (
    "--collect-only",
    "--co",
    "--setup-only",
    "--setup-plan",
    "--deselect",
    "--ignore",
    "-k",
    "-m",
)


def recipe_lines(makefile_text: str, target: str) -> list[str]:
    """The logical lines of a target's recipe, as make hands them to the shell.

    A recipe line starts with a tab. A backslash at the end of a line continues it
    onto the next physical line whatever that line starts with. Between recipe
    lines make skips blank lines and column-0 `#` comments; they are not part of the
    recipe and are not returned. Any other line ends the recipe, so the comment
    block that introduces the NEXT target is never read.
    """
    lines = makefile_text.splitlines()
    rule = re.compile(rf"^{re.escape(target)}\s*:(?!=)")
    start = next(i for i, line in enumerate(lines) if rule.match(line))
    logical: list[str] = []
    pending: list[str] | None = None
    for line in lines[start + 1 :]:
        if pending is None:
            if not line.startswith("\t"):
                if not line.strip() or line.startswith("#"):
                    continue
                break
            pending = []
        body = line[1:] if line.startswith("\t") else line
        if body.endswith("\\"):
            pending.append(body[:-1])
            continue
        pending.append(body)
        logical.append("\n".join(pending))
        pending = None
    if pending:
        logical.append("\n".join(pending))
    return logical


def _shell_tokens(text: str) -> list[tuple[str, bool]]:
    """(token, is_operator) for one shell line: quotes are removed, a `#` at the
    start of a word comments out the rest of its physical line, and the list
    operators `&& || ; | & ( )` are split from the words around them. Redirections
    stay ordinary words."""
    tokens: list[tuple[str, bool]] = []
    word: list[str] | None = None

    def flush() -> None:
        nonlocal word
        if word is not None:
            tokens.append(("".join(word), False))
            word = None

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            word = (word or []) + [text[i + 1]]
            i += 2
        elif ch == "'":
            end = text.index("'", i + 1)
            word = (word or []) + [text[i + 1 : end]]
            i = end + 1
        elif ch == '"':
            end = i + 1
            while text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            word = (word or []) + [text[i + 1 : end]]
            i = end + 1
        elif ch in " \t\n":
            flush()
            i += 1
        elif ch == "#" and word is None:
            newline = text.find("\n", i)
            i = n if newline == -1 else newline
        elif ch in "()&|;<>":
            flush()
            operator = text[i : i + 2] if text[i : i + 2] in _TWO_CHAR_OPERATORS else ch
            tokens.append((operator, "<" not in operator and ">" not in operator))
            i += len(operator)
        else:
            word = (word or []) + [ch]
            i += 1
    flush()
    return tokens


def _starts_a_command(tokens: list[tuple[str, bool]], index: int) -> bool:
    return index == 0 or tokens[index - 1][1]


def _failure_fails_the_line(tokens: list[tuple[str, bool]], start: int, end: int) -> bool:
    """Whether the command at tokens[start:end] both runs unconditionally with
    respect to failures before it and passes its own failure out of the line.

    Backward: neither the command nor any subshell around it may be the right-hand
    side of `||` (runs only after a failure) or of `|`. Forward: in the list that
    holds the command, and in every list around it, each later separator must be
    `&&`; a `;`, `||`, `|`, or `&` after it drops or replaces its status. Separators
    inside a later subshell or a later `if ... fi` belong to that compound, not to
    the list.
    """
    immediately_before, depth = True, 0
    for index in range(start - 1, -1, -1):
        token, is_operator = tokens[index]
        if immediately_before:
            if is_operator and token in ("||", "|", "|&"):
                return False
            immediately_before = is_operator and token == "("
        elif is_operator and token == ")":
            depth += 1
        elif is_operator and token == "(":
            if depth == 0:
                immediately_before = True
            else:
                depth -= 1
    depth = 0
    for index in range(end, len(tokens)):
        token, is_operator = tokens[index]
        if not is_operator:
            if _starts_a_command(tokens, index):
                depth += token in _COMPOUND_OPEN
                depth -= token in _COMPOUND_CLOSE
        elif token == "(":
            depth += 1
        elif token == ")":
            depth = max(depth - 1, 0)
        elif depth == 0 and token != "&&":
            return False
    return True


@dataclass(frozen=True)
class ShellCommand:
    """One simple command of a recipe line."""

    words: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: str
    failure_fails_the_recipe: bool


def recipe_commands(makefile_text: str, target: str) -> list[ShellCommand]:
    """Every simple command a target's recipe runs, with the directory its
    subshell changed into, the variables assigned on the command itself, and
    whether its failure fails the recipe. Each logical line is its own shell."""
    commands: list[ShellCommand] = []
    for logical in recipe_lines(makefile_text, target):
        prefix = re.match(r"[@+\-\s]*", logical).group()
        text = _MAKE_VARIABLE.sub(r"${\1}", logical[len(prefix) :]).replace("$$", "$")
        tokens = _shell_tokens(text)
        cwd = [""]
        start = 0
        for index in range(len(tokens) + 1):
            if index < len(tokens) and not tokens[index][1]:
                continue
            words = [token for token, _ in tokens[start:index]]
            env: list[tuple[str, str]] = []
            while words and _ASSIGNMENT.match(words[0]):
                env.append(_ASSIGNMENT.match(words.pop(0)).groups())
            if words[:1] == ["cd"] and len(words) > 1:
                cwd[-1] = posixpath.normpath(posixpath.join(cwd[-1], words[1]))
            if words:
                commands.append(
                    ShellCommand(
                        words=tuple(words),
                        env=tuple(env),
                        cwd=cwd[-1],
                        failure_fails_the_recipe="-" not in prefix
                        and _failure_fails_the_line(tokens, start, index),
                    )
                )
            if index < len(tokens):
                if tokens[index][0] == "(":
                    cwd.append(cwd[-1])
                elif tokens[index][0] == ")" and len(cwd) > 1:
                    cwd.pop()
            start = index + 1
    return commands


def _pytest_arguments(words: tuple[str, ...]) -> tuple[str, ...] | None:
    """The arguments of a pytest invocation, or None when the command is not one
    (an `echo` that mentions pytest is not)."""
    if words[:1] == ("pytest",):
        return words[1:]
    if words[:2] == ("uv", "run") and "pytest" in words[2:]:
        return words[words.index("pytest", 2) + 1 :]
    if words[:1] in (("python",), ("python3",)) and words[1:3] == ("-m", "pytest"):
        return words[3:]
    return None


@dataclass(frozen=True)
class GateRun:
    """One pytest command of the gate recipe that names a test file."""

    suites: tuple[str, ...]
    problems: tuple[str, ...]


def gate_runs(makefile_text: str) -> list[GateRun]:
    """Every pytest command in the gate recipe that names a `.py` file, with the
    repo-relative path of each file (resolved against the directory the command
    runs in) and the reasons, if any, it is not a hard-required full run whose
    failure fails the gate."""
    runs: list[GateRun] = []
    for command in recipe_commands(makefile_text, GATE_TARGET):
        arguments = _pytest_arguments(command.words)
        if arguments is None:
            continue
        files = [a for a in arguments if not a.startswith("-") and a.split("::")[0].endswith(".py")]
        if not files:
            continue
        problems = []
        if dict(command.env).get("HERD_TEST_PG_REQUIRED") != "1":
            problems.append("HERD_TEST_PG_REQUIRED=1 is not set on the command")
        if not command.failure_fails_the_recipe:
            problems.append("its failure does not fail the recipe")
        narrowing = [
            a for a in arguments if a.split("=")[0] in _NOT_A_FULL_RUN or ("::" in a and a in files)
        ]
        if narrowing:
            problems.append(f"it does not run the whole file ({' '.join(narrowing)})")
        runs.append(
            GateRun(
                suites=tuple(
                    posixpath.normpath(posixpath.join(command.cwd, f.split("::")[0])) for f in files
                ),
                problems=tuple(problems),
            )
        )
    return runs


def soundly_run_suites(makefile_text: str) -> set[str]:
    """Repo-relative paths of the suites the gate recipe runs with no problem."""
    return {suite for run in gate_runs(makefile_text) if not run.problems for suite in run.suites}


def gates_that_skip_the_phase(makefile_text: str) -> list[str]:
    """The gate targets whose recipe holds no `$(MAKE) _gate-pg-live-tests` command
    whose failure fails the gate."""
    return [
        gate
        for gate in GATES
        if not any(
            command.words[0] in ("${MAKE}", "make")
            and GATE_TARGET in command.words[1:]
            and command.failure_fails_the_recipe
            for command in recipe_commands(makefile_text, gate)
        )
    ]


def _live_pg_suites() -> list[Path]:
    return sorted(SERVICES_DIR.glob("*/tests/*_live_pg.py"))


# The real Makefile.


def test_every_live_pg_suite_is_in_the_gate_phase():
    wired = soundly_run_suites(MAKEFILE_PATH.read_text())
    missing = [
        p for p in _live_pg_suites() if p.relative_to(SERVICES_DIR.parent).as_posix() not in wired
    ]
    assert not missing, (
        "these live-Postgres suites are run by no hard-required command of the gate "
        "phase, so they never run with a real database: "
        + ", ".join(str(p.relative_to(SERVICES_DIR.parent)) for p in missing)
    )


def test_the_gate_phase_names_only_suites_that_exist():
    named = {suite for run in gate_runs(MAKEFILE_PATH.read_text()) for suite in run.suites}
    on_disk = {p.relative_to(SERVICES_DIR.parent).as_posix() for p in _live_pg_suites()}
    assert named - on_disk == set(), "the gate phase names a live-pg suite that is gone"


def test_every_gate_suite_runs_hard_required():
    """HERD_TEST_PG_REQUIRED=1 is what turns "no Postgres" from a silent skip into a
    gate failure; a command without it, one that only collects, or one whose status
    is dropped would pass the gate while proving nothing."""
    runs = gate_runs(MAKEFILE_PATH.read_text())
    assert runs, "the gate phase runs no live-pg suites at all"
    unsound = [
        f"{', '.join(run.suites)}: {'; '.join(run.problems)}" for run in runs if run.problems
    ]
    assert not unsound, "\n".join(unsound)


def test_master_and_everything_run_the_gate_phase():
    """The recipe is only a gate while both gates call it and stop on its failure."""
    skipping = gates_that_skip_the_phase(MAKEFILE_PATH.read_text())
    assert not skipping, f"these gates do not run {GATE_TARGET}: {skipping}"


# The scan itself, on synthetic Makefiles: one way of naming a suite without
# running it per case (issue #1158). ONE and TWO stand for two suites in two
# services; each case breaks only TWO.

ONE = "services/a/tests/test_one_live_pg.py"
TWO = "services/b/tests/test_two_live_pg.py"
_SETUP = '\t@dsn="postgresql://$${pguser:-herd}@127.0.0.1/$${pgdb:-herd}"; \\\n'
_RUN_ONE = (
    '\t(cd services/a && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \\\n'
    "\t\tuv run pytest tests/test_one_live_pg.py -v)"
)
_RUN_TWO = (
    '\t(cd services/b && HERD_TEST_PG_REQUIRED=1 HERD_TEST_PG_DSN="$$dsn" \\\n'
    "\t\tuv run pytest tests/test_two_live_pg.py -v)"
)


def _makefile(recipe: str, after: str = "") -> str:
    return (
        "# tests/test_two_live_pg.py is described here, above the target.\n"
        f"{GATE_TARGET}:\n{recipe}\n{after}\n"
        "next-target:\n\t@true\n"
    )


def test_scan_reads_a_sound_recipe():
    text = _makefile(_SETUP + _RUN_ONE + " && \\\n" + _RUN_TWO)
    assert soundly_run_suites(text) == {ONE, TWO}
    assert [run.problems for run in gate_runs(text)] == [(), ()]


def test_scan_follows_cd_out_of_a_subshell_and_a_repo_root_path():
    text = _makefile(
        "\tHERD_TEST_PG_REQUIRED=1 uv run pytest services/a/tests/test_one_live_pg.py && \\\n"
        "\t(cd services && cd b && HERD_TEST_PG_REQUIRED=1 pytest tests/test_two_live_pg.py)"
    )
    assert soundly_run_suites(text) == {ONE, TWO}


_ONLY_ONE = _SETUP + _RUN_ONE
_TWO_COMMENTED = "".join(f"\t# {line.strip()}\n" for line in _RUN_TWO.splitlines()).rstrip("\n")

FALSE_PASSES = {
    "comment after the recipe": _makefile(
        _ONLY_ONE,
        after="\n# was: (cd services/b && HERD_TEST_PG_REQUIRED=1 "
        "uv run pytest tests/test_two_live_pg.py -v)\n# The next target.",
    ),
    "commented out inside the recipe": _makefile(
        _SETUP + _TWO_COMMENTED.replace(" -v)", " -v) && \\") + "\n" + _RUN_ONE
    ),
    "trailing shell comment": _makefile(
        _ONLY_ONE + "  # then tests/test_two_live_pg.py HERD_TEST_PG_REQUIRED=1"
    ),
    "makefile comment between recipe lines": _makefile(
        _ONLY_ONE
        + "\n# (cd services/b && HERD_TEST_PG_REQUIRED=1 uv run pytest tests/test_two_live_pg.py)"
        + "\n\t@echo done"
    ),
    "named in an echo": _makefile(
        _ONLY_ONE + " && \\\n"
        '\techo "HERD_TEST_PG_REQUIRED=1 uv run pytest services/b/tests/test_two_live_pg.py"'
    ),
    "echo of the command itself": _makefile(
        _ONLY_ONE + " && \\\n"
        "\t(cd services/b && HERD_TEST_PG_REQUIRED=1 echo uv run pytest tests/test_two_live_pg.py)"
    ),
    "in the next target": _makefile(_ONLY_ONE, after="other-target:\n" + _RUN_TWO + "\n"),
    "failure swallowed by || true": _makefile(
        _SETUP + _RUN_TWO.replace(" -v)", " -v || true)") + " && \\\n" + _RUN_ONE
    ),
    "subshell failure swallowed by || true": _makefile(
        _SETUP + _RUN_TWO + " || true; \\\n" + _RUN_ONE
    ),
    "status dropped by a semicolon": _makefile(_SETUP + _RUN_TWO + "; \\\n" + _RUN_ONE),
    "status replaced by a pipe": _makefile(
        _SETUP + _RUN_TWO.replace(" -v)", " -v | tee pg.log)") + " && \\\n" + _RUN_ONE
    ),
    "run in the background": _makefile(_SETUP + _RUN_TWO + " & \\\n" + _RUN_ONE),
    "runs only after a failure": _makefile(
        _SETUP + "\ttrue || \\\n" + _RUN_TWO + " && \\\n" + _RUN_ONE
    ),
    "errors ignored by make": _makefile(_ONLY_ONE + "\n" + _RUN_TWO.replace("\t(", "\t-(", 1)),
    "collect only": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace(" -v)", " --collect-only)")
    ),
    "collect only, short form": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace(" -v)", " --co -q)")
    ),
    "one test selected by -k": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace(" -v)", " -k nothing -v)")
    ),
    "one test selected by node id": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("_pg.py -v)", "_pg.py::test_x -v)")
    ),
    "tests deselected": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace(" -v)", " --deselect=tests/x.py::test_y)")
    ),
    "not hard-required": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("HERD_TEST_PG_REQUIRED=1 ", "")
    ),
    "hard-required switched off": _makefile(
        _ONLY_ONE
        + " && \\\n"
        + _RUN_TWO.replace("HERD_TEST_PG_REQUIRED=1", "HERD_TEST_PG_REQUIRED=0")
    ),
    "hard-required set on another command only": _makefile(
        _ONLY_ONE + " && \\\n"
        "\t(cd services/b && HERD_TEST_PG_REQUIRED=1 true && \\\n"
        "\t\tuv run pytest tests/test_two_live_pg.py)"
    ),
    "run from another service": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("cd services/b", "cd services/a")
    ),
    "a longer file name that contains the suite's": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("tests/test_two", "tests/not_test_two")
    ),
}


@pytest.mark.parametrize("spelling", sorted(FALSE_PASSES))
def test_scan_does_not_count_a_suite_that_is_named_but_not_run(spelling: str):
    text = FALSE_PASSES[spelling]
    assert "test_two_live_pg.py" in text, "the case must still name the suite"
    assert TWO not in soundly_run_suites(text), spelling
    assert ONE in soundly_run_suites(text), "the case must break only the second suite"


def _gates(master_tail: str, everything_tail: str) -> str:
    chain = "\t@trap '$(MAKE) _master-stack-down' EXIT INT TERM; \\\n\t\t$(MAKE) test-e2e && \\\n"
    return f"master:\n{chain}{master_tail}\n\neverything:\n{chain}{everything_tail}\n"


_CALL = "\t\t$(MAKE) _gate-pg-live-tests"
_LOAD_TAIL = (
    " && \\\n"
    '\t\tif [ "$(EVERYTHING_LOAD)" != "0" ]; then \\\n'
    "\t\t\t$(MAKE) test-load; \\\n"
    "\t\telse \\\n"
    '\t\t\techo "skipping"; \\\n'
    "\t\tfi && \\\n"
    "\t\tok=1"
)


def test_gate_scan_accepts_the_call_as_the_last_link_and_before_an_if_block():
    assert gates_that_skip_the_phase(_gates(_CALL, _CALL + _LOAD_TAIL)) == []


@pytest.mark.parametrize(
    "broken",
    [
        "\t\t# $(MAKE) _gate-pg-live-tests",
        '\t\techo "skipping $(MAKE) _gate-pg-live-tests"',
        "\t\t$(MAKE) _gate-pg-live-tests || true",
        "\t\t$(MAKE) _gate-pg-live-tests; \\\n\t\ttrue",
        "\t\ttrue\n\n# $(MAKE) _gate-pg-live-tests runs after the stack is up",
    ],
)
def test_gate_scan_flags_a_gate_that_names_the_phase_without_running_it(broken: str):
    assert gates_that_skip_the_phase(_gates(broken, _CALL)) == ["master"]
    assert gates_that_skip_the_phase(_gates(_CALL, broken)) == ["everything"]
