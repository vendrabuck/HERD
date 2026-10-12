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
target (issue #1158). A suite counts only when the recipe holds a command for which
ALL of this is true:

- it is a pytest invocation: `pytest`, `uv run pytest`, `python -m pytest`, or
  `uv run python -m pytest`, optionally behind `env [-u NAME] [NAME=value]` or
  `timeout <duration>`;
- it names the suite's file, and runs in the suite's own service directory
  (`services/<svc>`, reached by `cd` in the same shell or subshell);
- HERD_TEST_PG_REQUIRED=1 is assigned on that command (or by its `env` wrapper).
  An `export` earlier in the line is NOT read: such a recipe is refused;
- every other argument is on a short allowlist of options that cannot narrow or
  skip a run (`-v -q -x -s -l` in any cluster, `-r<chars>`, `--tb`, `--durations`,
  `--durations-min`, `--maxfail`, `--color`, `-W`, `-p no:<plugin>`,
  `--strict-markers`, `--strict-config`). Anything else is refused, which covers
  `-k`, `-m`, `--collect-only`, `--deselect`, `-o`, a node id, and every clustered
  or abbreviated spelling of them without listing them;
- PYTEST_ADDOPTS appears nowhere in the recipe;
- its failure fails the recipe: no `;`, `||`, `|`, or `&` after it at any nesting
  level, it is not the right-hand side of `||` or `|`, and the line has no `-`
  prefix;
- no `exit` runs ahead of it in its shell or subshell (an `exit` directly after
  `||` is taken as an error exit and allowed).

A name in a comment (after the recipe, between its lines, or on a commented-out
line) or in an `echo` is not a command and does not count. The same reading is
applied to `master` and `everything`: each must hold a `$(MAKE)`, `$(SUBMAKE)`, or
`make` command for the phase with no option that stops it running or hides its
failure. The detector tests below feed every one of these spellings to the scans
the real-tree tests use.

Limits. The shell is read by a small scanner, not a shell:

- not seen: `trap 'exit 0' EXIT`, a later second definition of the target, `.IGNORE`,
  `.ONESHELL`, a changed `.RECIPEPREFIX`, a shell function that is defined and never
  called, a negated group such as `! (false && (...))`, a command behind `false &&`,
  and anything inside an `if` or loop whose condition is false;
- read conservatively (refused though it may be sound): an `export` of the required
  variable, an `exit` inside a `{ ...; }` group, a backtick substitution holding a
  pipe, `uv run` with options ahead of `pytest`, a redirect, any pytest option
  outside the allowlist, and, for the gates, the phase listed as a prerequisite of
  `master` or `everything` instead of called in its recipe.
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
# pytest arguments that cannot narrow or skip a run. Everything else is refused, so a
# narrowing option needs no entry here under any spelling.
_HARMLESS_FLAG_CLUSTER = re.compile(r"-[vqxsl]+")
_HARMLESS_ATTACHED = re.compile(
    r"-r[A-Za-z]+|-W.+|-pno:.+|--(?:tb|durations|durations-min|maxfail|color)=.+"
)
_HARMLESS_LONG = {"--strict-markers", "--strict-config"}
# Options whose value is the next word.
_HARMLESS_WITH_VALUE = {"--tb", "--durations", "--durations-min", "--maxfail", "--color", "-W"}
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
# make options that do not stop a sub-make running its target or hide its failure.
_HARMLESS_MAKE_OPTION = re.compile(r"--no-print-directory|-s|--silent|-k|--keep-going|-j\d*")
_MAKE_TARGET_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


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
    """One simple command of a recipe line. `words` is empty for a bare assignment."""

    words: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: str
    failure_fails_the_recipe: bool
    after_an_exit: bool


def recipe_commands(makefile_text: str, target: str) -> list[ShellCommand]:
    """Every simple command a target's recipe holds, with the directory its
    subshell changed into, the variables assigned on the command itself, whether
    its failure fails the recipe, and whether an `exit` runs ahead of it in its
    shell or subshell. Each logical line is its own shell."""
    commands: list[ShellCommand] = []
    for logical in recipe_lines(makefile_text, target):
        prefix = re.match(r"[@+\-\s]*", logical).group()
        text = _MAKE_VARIABLE.sub(r"${\1}", logical[len(prefix) :]).replace("$$", "$")
        tokens = _shell_tokens(text)
        cwd, exited = [""], [False]
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
            if words or env:
                commands.append(
                    ShellCommand(
                        words=tuple(words),
                        env=tuple(env),
                        cwd=cwd[-1],
                        failure_fails_the_recipe="-" not in prefix
                        and _failure_fails_the_line(tokens, start, index),
                        after_an_exit=exited[-1],
                    )
                )
            if words[:1] == ["exit"] and not (start > 0 and tokens[start - 1][0] == "||"):
                exited[-1] = True
            if index < len(tokens):
                if tokens[index][0] == "(":
                    cwd.append(cwd[-1])
                    exited.append(exited[-1])
                elif tokens[index][0] == ")" and len(cwd) > 1:
                    cwd.pop()
                    exited.pop()
            start = index + 1
    return commands


def _pytest_invocation(command: ShellCommand) -> tuple[dict[str, str], tuple[str, ...]] | None:
    """(the variables pytest is started with, pytest's arguments) for a command that
    runs pytest, or None when it does not (an `echo` that mentions pytest does not).

    A leading `env` or `timeout <duration>` wrapper is read through: `env -u NAME`
    and `env -i` remove variables, `env NAME=value` adds one. Any other wrapper, and
    `uv run` with anything between `run` and `pytest`, is not recognized."""
    env = dict(command.env)
    words = list(command.words)
    while words[:1] in (["env"], ["timeout"]):
        if words.pop(0) == "timeout":
            if not words or not _DURATION.fullmatch(words.pop(0)):
                return None
            continue
        while words:
            if words[0] in ("-i", "-", "--ignore-environment"):
                env.clear()
                words.pop(0)
            elif words[0] == "-u" and len(words) > 1:
                env.pop(words[1], None)
                del words[:2]
            elif _ASSIGNMENT.match(words[0]):
                name, value = _ASSIGNMENT.match(words.pop(0)).groups()
                env[name] = value
            else:
                break
    for runner in (
        ["pytest"],
        ["uv", "run", "pytest"],
        ["python", "-m", "pytest"],
        ["python3", "-m", "pytest"],
        ["uv", "run", "python", "-m", "pytest"],
    ):
        if words[: len(runner)] == runner:
            return env, tuple(words[len(runner) :])
    return None


def _split_pytest_arguments(arguments: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """(the test files named, every argument that is neither a plain file nor on the
    allowlist of options that cannot narrow or skip a run)."""
    files: list[str] = []
    refused: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-"):
            if argument.split("::")[0].endswith(".py"):
                files.append(argument.split("::")[0])
            if not argument.endswith(".py"):
                refused.append(argument)
        elif argument in _HARMLESS_WITH_VALUE and index + 1 < len(arguments):
            index += 1
        elif argument == "-p" and arguments[index + 1 : index + 2][:1]:
            index += 1
            if not arguments[index].startswith("no:"):
                refused.append(f"-p {arguments[index]}")
        elif not (
            argument in _HARMLESS_LONG
            or _HARMLESS_FLAG_CLUSTER.fullmatch(argument)
            or _HARMLESS_ATTACHED.fullmatch(argument)
        ):
            refused.append(argument)
        index += 1
    return files, refused


@dataclass(frozen=True)
class GateRun:
    """One pytest command of the gate recipe that names a test file."""

    suites: tuple[str, ...]
    problems: tuple[str, ...]


def gate_runs(makefile_text: str) -> list[GateRun]:
    """Every pytest command in the gate recipe that names a `.py` file, with the
    repo-relative path of each file (resolved against the directory the command
    runs in) and the reasons, if any, it is not a hard-required run of the whole
    file, from the suite's service directory, whose failure fails the gate."""
    commands = recipe_commands(makefile_text, GATE_TARGET)
    addopts = any(
        word.split("=")[0] == "PYTEST_ADDOPTS"
        for command in commands
        for word in (*command.words, *(name for name, _ in command.env))
    )
    runs: list[GateRun] = []
    for command in commands:
        invocation = _pytest_invocation(command)
        if invocation is None:
            continue
        env, arguments = invocation
        files, refused = _split_pytest_arguments(arguments)
        if not files:
            continue
        suites = tuple(posixpath.normpath(posixpath.join(command.cwd, f)) for f in files)
        problems = []
        if env.get("HERD_TEST_PG_REQUIRED") != "1":
            problems.append("HERD_TEST_PG_REQUIRED=1 is not set on the command")
        if not command.failure_fails_the_recipe:
            problems.append("its failure does not fail the recipe")
        if command.after_an_exit:
            problems.append("an exit runs ahead of it")
        if refused:
            problems.append(
                f"an argument that may narrow or skip the run ({' '.join(refused)}); only "
                "the file and options that cannot do that are accepted"
            )
        if addopts:
            problems.append("PYTEST_ADDOPTS is set in the recipe")
        if any(posixpath.dirname(posixpath.dirname(suite)) != command.cwd for suite in suites):
            problems.append("it does not run in the suite's service directory")
        runs.append(GateRun(suites=suites, problems=tuple(problems)))
    return runs


def soundly_run_suites(makefile_text: str) -> set[str]:
    """Repo-relative paths of the suites the gate recipe runs with no problem."""
    return {suite for run in gate_runs(makefile_text) if not run.problems for suite in run.suites}


def _runs_the_phase(command: ShellCommand) -> bool:
    """Whether a command is a sub-make of the gate phase that really runs it and
    whose failure fails the gate: `$(MAKE)`, `$(SUBMAKE)` (the Makefile's alias for
    it), or `make`, then only targets, `NAME=value`, `-C <dir>`, and options that
    neither stop the run nor hide its failure. `-n`, `-q`, `-t`, `-i`, `-o` and their
    long forms are therefore refused without being listed."""
    words = list(command.words)
    if words[:1] not in (["${MAKE}"], ["${SUBMAKE}"], ["make"]) or GATE_TARGET not in words:
        return False
    rest = words[1:]
    while rest:
        word = rest.pop(0)
        if word == "-C" and rest:
            rest.pop(0)
        elif not (
            _HARMLESS_MAKE_OPTION.fullmatch(word)
            or _ASSIGNMENT.match(word)
            or _MAKE_TARGET_WORD.fullmatch(word)
        ):
            return False
    return command.failure_fails_the_recipe and not command.after_an_exit


def gates_that_skip_the_phase(makefile_text: str) -> list[str]:
    """The gate targets whose recipe holds no sub-make of `_gate-pg-live-tests` that
    runs it and whose failure fails the gate."""
    return [
        gate
        for gate in GATES
        if not any(_runs_the_phase(command) for command in recipe_commands(makefile_text, gate))
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


SOUND_SPELLINGS = {
    "two cd steps and a bare pytest": "(cd services && cd b && HERD_TEST_PG_REQUIRED=1 pytest "
    "tests/test_two_live_pg.py)",
    "env -u wrapper": "(cd services/b && HERD_TEST_PG_REQUIRED=1 env -u VIRTUAL_ENV uv run "
    "pytest tests/test_two_live_pg.py -v)",
    "the variable given by the env wrapper": "(cd services/b && env HERD_TEST_PG_REQUIRED=1 "
    "uv run pytest tests/test_two_live_pg.py -v)",
    "timeout wrapper": "(cd services/b && HERD_TEST_PG_REQUIRED=1 timeout 900 uv run pytest "
    "tests/test_two_live_pg.py -v)",
    "python -m pytest": "(cd services/b && HERD_TEST_PG_REQUIRED=1 uv run python -m pytest "
    "tests/test_two_live_pg.py)",
    "options that cannot narrow the run": "(cd services/b && HERD_TEST_PG_REQUIRED=1 uv run "
    "pytest tests/test_two_live_pg.py -vv -x -rA --tb=short -p no:cacheprovider "
    "--durations=5 -W error --maxfail 1 --strict-markers)",
}


@pytest.mark.parametrize("spelling", sorted(SOUND_SPELLINGS))
def test_scan_accepts_every_sound_spelling_of_a_run(spelling: str):
    text = _makefile(_SETUP + _RUN_ONE + " && \\\n\t" + SOUND_SPELLINGS[spelling])
    assert [run.problems for run in gate_runs(text)] == [(), ()], spelling
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
    "named by a repo-root path, not run in its service directory": _makefile(
        _ONLY_ONE + " && \\\n"
        "\tHERD_TEST_PG_REQUIRED=1 uv run pytest services/b/tests/test_two_live_pg.py -v"
    ),
    "an exit ahead of it in its subshell": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("cd services/b &&", "cd services/b && exit 0 &&")
    ),
    "the variable cleared by env -i": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("uv run pytest", "env -i uv run pytest")
    ),
    "the variable unset by env -u": _makefile(
        _ONLY_ONE
        + " && \\\n"
        + _RUN_TWO.replace("uv run pytest", "env -u HERD_TEST_PG_REQUIRED uv run pytest")
    ),
    "uv run of an echo": _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace("uv run pytest", "uv run echo pytest")
    ),
    "PYTEST_ADDOPTS on the command": _makefile(
        _RUN_ONE.replace("\t(", "\t@(", 1)
        + "\n"
        + _RUN_TWO.replace(
            "HERD_TEST_PG_REQUIRED=1", 'PYTEST_ADDOPTS="--co" HERD_TEST_PG_REQUIRED=1'
        )
    ),
}
# Every argument real pytest accepts that selects less than the file or runs nothing
# (measured against pytest itself for issue #1158), in place of the trailing `-v`.
for _arguments in (
    "-vk test_x",
    "-ktest_x",
    "-qk test_x",
    "-mslow",
    "-vm slow",
    "-m slow",
    "--collectonly",
    "--fixtures",
    "--markers",
    "--help",
    "--version",
    "--setup-only",
    "--setup-plan",
    "--deselect tests/test_two_live_pg.py::test_x",
    "--ignore=tests/test_two_live_pg.py",
    "--ignore-glob=*_live_pg.py",
    "-o addopts=--co",
    "-o python_functions=test_x",
    "-p pytest_randomly",
    "--lf",
    "--sw",
    "-c other.ini",
    "tests/",
):
    FALSE_PASSES[f"pytest argument: {_arguments}"] = _makefile(
        _ONLY_ONE + " && \\\n" + _RUN_TWO.replace(" -v)", f" {_arguments})")
    )


@pytest.mark.parametrize("spelling", sorted(FALSE_PASSES))
def test_scan_does_not_count_a_suite_that_is_named_but_not_run(spelling: str):
    text = FALSE_PASSES[spelling]
    assert "test_two_live_pg.py" in text, "the case must still name the suite"
    assert TWO not in soundly_run_suites(text), spelling
    if spelling != "PYTEST_ADDOPTS on the command":
        assert ONE in soundly_run_suites(text), "the case must break only the second suite"


def test_scan_accepts_an_error_exit_ahead_of_the_chain():
    text = _makefile("\ttest -f .env || exit 1; \\\n" + _RUN_ONE + " && \\\n" + _RUN_TWO)
    assert soundly_run_suites(text) == {ONE, TWO}


@pytest.mark.parametrize(
    "ahead",
    [
        "\texit 0; \\\n",
        "\texport PYTEST_ADDOPTS=--co; \\\n",
        '\tPYTEST_ADDOPTS="-k nothing"; export PYTEST_ADDOPTS; \\\n',
    ],
)
def test_scan_counts_no_suite_behind_an_exit_or_a_recipe_wide_addopts(ahead: str):
    text = _makefile(ahead + _RUN_ONE + " && \\\n" + _RUN_TWO)
    assert soundly_run_suites(text) == set(), ahead
    assert len(gate_runs(text)) == 2


def test_scan_reads_the_required_variable_on_the_command_only():
    """By decision: an `export` once for the line is a sound recipe the scan does not
    read, so it is refused and the recipe must keep the variable on each command."""
    text = _makefile(
        "\texport HERD_TEST_PG_REQUIRED=1; \\\n"
        + _RUN_ONE
        + " && \\\n"
        + _RUN_TWO.replace("HERD_TEST_PG_REQUIRED=1 ", "")
    )
    assert soundly_run_suites(text) == {ONE}


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
    "call",
    [
        "\t\t$(SUBMAKE) _gate-pg-live-tests",
        "\t\t${MAKE} _gate-pg-live-tests",
        "\t\tmake _gate-pg-live-tests",
        "\t\t$(MAKE) -C . _gate-pg-live-tests",
        "\t\t$(MAKE) _gate-pg-live-tests COMPOSE_PROJECT_NAME=$(GATE_PROJECT)",
        "\t\t$(MAKE) --no-print-directory -k _gate-pg-live-tests",
    ],
)
def test_gate_scan_accepts_every_spelling_of_a_real_call(call: str):
    assert gates_that_skip_the_phase(_gates(call, call + _LOAD_TAIL)) == []


@pytest.mark.parametrize(
    "broken",
    [
        "\t\t# $(MAKE) _gate-pg-live-tests",
        '\t\techo "skipping $(MAKE) _gate-pg-live-tests"',
        "\t\t$(MAKE) _gate-pg-live-tests || true",
        "\t\t$(MAKE) _gate-pg-live-tests; \\\n\t\ttrue",
        "\t\ttrue\n\n# $(MAKE) _gate-pg-live-tests runs after the stack is up",
        "\t\ttrue\n\t-$(MAKE) _gate-pg-live-tests",
        "\t\t-$(MAKE) _gate-pg-live-tests",
        "\t\t$(MAKE) -n _gate-pg-live-tests",
        "\t\t$(MAKE) --dry-run _gate-pg-live-tests",
        "\t\t$(MAKE) -q _gate-pg-live-tests",
        "\t\t$(MAKE) -t _gate-pg-live-tests",
        "\t\t$(MAKE) -i _gate-pg-live-tests",
        "\t\t$(MAKE) -o _gate-pg-live-tests _gate-pg-live-tests",
        "\t\texit 0 && $(MAKE) _gate-pg-live-tests",
        "\t\t$(OTHER) _gate-pg-live-tests",
    ],
)
def test_gate_scan_flags_a_gate_that_names_the_phase_without_running_it(broken: str):
    assert gates_that_skip_the_phase(_gates(broken, _CALL)) == ["master"]
    assert gates_that_skip_the_phase(_gates(_CALL, broken)) == ["everything"]
