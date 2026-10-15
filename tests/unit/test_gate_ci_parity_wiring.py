"""Static pin: the local gates run the ci.yml checks they are meant to mirror.

No stack, no Docker, no network: this parses the Makefile and
`.github/workflows/ci.yml` only, reusing the recipe scanner of
tests/unit/test_pg_live_gate_wiring.py (`recipe_commands`), so a name in a
comment or an `echo`, a command whose failure is dropped, and a command behind an
`exit` word are read the same way here as there.

What it pins, each check read from ci.yml rather than restated:

- `master-quick` and `everything` run ci.yml's `uv lock --check` BEFORE any
  `uv sync`. Without `--locked`, `uv sync` rewrites a stale uv.lock in place, so
  a gate that synced first would repair the drift CI fails on and go green.
- `everything` runs the image-versus-lock check after its `docker compose build`
  and before the gate stack starts: ci.yml's `uv export` command word for word
  (up to its redirect), a `docker run --rm <image>` of ci.yml's `pip list`
  command, and `scripts/check_image_matches_lock.py` with ci.yml's `--allow` set.
- `everything` runs ci.yml's package-lock sync pair (`npm install`, then
  `git diff --exit-code -- package-lock.json`) in `frontend/`, after its
  `npm run build`.
- `everything` runs ci.yml's second vitest command (the frontend step that sets
  `TZ` in its `env:`) in the same directory with the same `TZ` assigned on the
  command.

A check counts when its command is in the target's recipe or in the recipe of a
target the recipe calls through `$(MAKE)`, `$(SUBMAKE)`, or `make` (followed two
levels down), in recipe order, and when every link counts: the command and each
sub-make call fail the recipe on failure (no `;`, `||`, `|`, `&` after them, no
`-` line prefix), no `exit` or `exec` word stands ahead of them in their recipe,
a sub-make call assigns no variable and carries only targets and options that
cannot stop it running or hide its failure (`-n`, `-q`, `-t`, `-i`, `-o`, `-C`
are refused), and no target on the path has a target-specific `SHELL`.

Limits (the scanner's own Limits in test_pg_live_gate_wiring.py apply too):

- The image the check runs is not judged: any word after `docker run --rm` is
  accepted as the image, so a check against the wrong image passes.
- The `TZ` value must be assigned on the vitest command itself; an `export TZ`
  earlier in the line, a target-specific `export TZ := ...`, or a `TZ` from the
  caller's environment is not seen (refused).
- Commands are matched by their words, so a spelling that does the same thing
  differently (`npm run test` for `npm test`) is refused, and options are compared
  in ci.yml's order. The one exception is the lock script's runner: `python3`,
  `python`, and `uv run python` are all accepted.
- Whether the uv.lock check precedes `uv sync` is judged only inside the gate's
  own recipe and the targets it calls; a `uv sync` in a prerequisite (gate-clean,
  for example) is not seen.
- The UTC half of ci.yml's pair is not pinned: `everything` runs vitest a first
  time under the host's own zone through `coverage-frontend`.
"""

from __future__ import annotations

import pytest
import yaml

from .test_pg_live_gate_wiring import (
    _ASSIGNMENT,
    _HARMLESS_MAKE_OPTION,
    _MAKE_TARGET_WORD,
    MAKEFILE_PATH,
    REPO_ROOT,
    ShellCommand,
    recipe_commands,
    target_specific_shell_lines,
)

CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCK_SCRIPT = "scripts/check_image_matches_lock.py"
_MAKE_WORDS = (("${MAKE}",), ("${SUBMAKE}",), ("make",))
_MAX_DEPTH = 2


# ci.yml, read once per call so a detector test and the real tree see the same values.


def _ci_steps(job: str) -> list[dict]:
    data = yaml.safe_load(CI_PATH.read_text())
    return data["jobs"][job]["steps"]


def shell_commands(run: str) -> list[ShellCommand]:
    """The simple commands of a workflow `run:` block, read by the Makefile scanner:
    each physical line becomes a recipe line, so a backslash continuation joins as
    it does in a recipe."""
    body = "\n".join(f"\t{line}" for line in run.strip().splitlines())
    return recipe_commands(f"_ci_step:\n{body}\n", "_ci_step")


def _step_commands(job: str, predicate) -> tuple[dict, list[ShellCommand]]:
    for step in _ci_steps(job):
        commands = shell_commands(str(step.get("run", "")))
        if any(predicate(c) for c in commands):
            return step, commands
    raise AssertionError(f"ci.yml's {job} job has no step the guard expects")


def _until_redirect(words: tuple[str, ...]) -> tuple[str, ...]:
    for index, word in enumerate(words):
        if word.startswith((">", "<")):
            return words[:index]
    return words


def ci_lock_check() -> tuple[str, ...]:
    _, commands = _step_commands("backend", lambda c: c.words[:2] == ("uv", "lock"))
    return next(c.words for c in commands if c.words[:2] == ("uv", "lock"))


def ci_image_check() -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
    """(the uv export words, the command run inside the image, the --allow set)."""
    # The lint steps name the script too; the check is the step that runs uv export.
    _, commands = _step_commands("backend", lambda c: c.words[:2] == ("uv", "export"))
    export = next(_until_redirect(c.words) for c in commands if c.words[:2] == ("uv", "export"))
    run = next(c.words for c in commands if c.words[:2] == ("docker", "run"))
    inside = _until_redirect(run[run.index("--rm") + 2 :])
    script = next(c.words for c in commands if LOCK_SCRIPT in c.words)
    return export, inside, _allowed(script)


def ci_package_lock_check() -> tuple[ShellCommand, ShellCommand]:
    _, commands = _step_commands("frontend", lambda c: c.words[:2] == ("git", "diff"))
    install = next(c for c in commands if c.words[:2] == ("npm", "install"))
    diff = next(c for c in commands if c.words[:2] == ("git", "diff"))
    return install, diff


def ci_tz_run() -> tuple[str, ShellCommand]:
    """(the zone, the vitest command) of ci.yml's frontend step that sets TZ."""
    for step in _ci_steps("frontend"):
        zone = (step.get("env") or {}).get("TZ")
        if zone:
            commands = shell_commands(str(step["run"]))
            return str(zone), next(c for c in commands if c.words[:1] == ("npm",))
    raise AssertionError("ci.yml's frontend job has no step that sets TZ")


def _allowed(words: tuple[str, ...]) -> frozenset[str]:
    return frozenset(words[i + 1] for i, w in enumerate(words[:-1]) if w == "--allow")


# The Makefile.


def _called_target(command: ShellCommand) -> str | None:
    """The single target a sub-make call runs, when the call really runs it and
    passes its failure on; None for anything else."""
    words = command.words
    if not any(words[: len(m)] == m for m in _MAKE_WORDS) or command.env:
        return None
    targets = []
    for word in words[1:]:
        if _ASSIGNMENT.match(word):
            return None
        if _HARMLESS_MAKE_OPTION.fullmatch(word):
            continue
        if not _MAKE_TARGET_WORD.fullmatch(word) or word.startswith("-"):
            return None
        targets.append(word)
    return targets[0] if len(targets) == 1 else None


def _counts(command: ShellCommand) -> bool:
    return command.failure_fails_the_recipe and not command.after_an_exit


def reachable_commands(text: str, target: str, depth: int = 0) -> list[ShellCommand]:
    """Every command that counts, in the order the target runs it, each counting
    sub-make call followed by what its target runs (two levels down). A target with a
    target-specific SHELL, or with no explicit rule, contributes nothing."""
    if target_specific_shell_lines(text, target):
        return []
    try:
        commands = recipe_commands(text, target)
    except StopIteration:
        return []
    found: list[ShellCommand] = []
    for command in commands:
        if not _counts(command):
            continue
        found.append(command)
        called = _called_target(command)
        if called is not None and depth < _MAX_DEPTH:
            found.extend(reachable_commands(text, called, depth + 1))
    return found


def _first(commands: list[ShellCommand], predicate) -> int | None:
    return next((i for i, c in enumerate(commands) if predicate(c)), None)


def lock_check_problems(text: str, target: str) -> list[str]:
    commands = reachable_commands(text, target)
    lock = _first(commands, lambda c: c.words == ci_lock_check() and not c.env)
    sync = _first(commands, lambda c: c.words[:2] == ("uv", "sync"))
    if lock is None:
        return [f"{target} does not run `{' '.join(ci_lock_check())}`"]
    if sync is not None and sync < lock:
        return [f"{target} runs `uv sync` before `{' '.join(ci_lock_check())}`"]
    return []


def image_check_problems(text: str) -> list[str]:
    export, inside, allowed = ci_image_check()
    commands = reachable_commands(text, "everything")
    build = _first(commands, lambda c: c.words[:3] == ("docker", "compose", "build"))
    stack = _first(commands, lambda c: _called_target(c) == "_master-stack-up")

    def runs_image(c: ShellCommand) -> bool:
        if c.words[:2] != ("docker", "run") or "--rm" not in c.words:
            return False
        rest = c.words[c.words.index("--rm") + 2 :]
        return _until_redirect(rest) == inside

    def runs_script(c: ShellCommand) -> bool:
        runner = c.words[: c.words.index(LOCK_SCRIPT)] if LOCK_SCRIPT in c.words else None
        return runner in (("python3",), ("python",), ("uv", "run", "python")) and (
            _allowed(c.words) == allowed
        )

    steps = {
        "uv export": _first(commands, lambda c: _until_redirect(c.words) == export),
        "docker run": _first(commands, runs_image),
        LOCK_SCRIPT: _first(commands, runs_script),
    }
    problems = [
        f"everything does not run ci.yml's {name} step" for name, i in steps.items() if i is None
    ]
    if problems:
        return problems
    if build is None:
        return ["everything runs no `docker compose build`"]
    if min(steps.values()) < build:
        problems.append("the image check runs before everything's `docker compose build`")
    if stack is not None and max(steps.values()) > stack:
        problems.append("the image check runs after the gate stack starts")
    if not steps["uv export"] < steps["docker run"] < steps[LOCK_SCRIPT]:
        problems.append("the image check's three commands are out of order")
    return problems


def package_lock_problems(text: str) -> list[str]:
    install, diff = ci_package_lock_check()
    commands = reachable_commands(text, "everything")
    build = _first(commands, lambda c: c.words == ("npm", "run", "build") and c.cwd == install.cwd)
    found_install = _first(commands, lambda c: c.words == install.words and c.cwd == install.cwd)
    found_diff = _first(commands, lambda c: c.words == diff.words and c.cwd == diff.cwd)
    if found_install is None or found_diff is None:
        return [
            f"everything does not run `{' '.join(install.words)}` then "
            f"`{' '.join(diff.words)}` in {install.cwd}/"
        ]
    if build is None:
        return ["everything runs no `npm run build`"]
    if not build < found_install < found_diff:
        return ["the package-lock check does not run after the frontend build, install first"]
    return []


def tz_problems(text: str) -> list[str]:
    zone, vitest = ci_tz_run()
    commands = reachable_commands(text, "everything")
    if (
        _first(
            commands,
            lambda c: (
                c.words == vitest.words and c.cwd == vitest.cwd and dict(c.env).get("TZ") == zone
            ),
        )
        is None
    ):
        return [f"everything does not run `{' '.join(vitest.words)}` with TZ={zone} on it"]
    return []


def all_problems(text: str) -> list[str]:
    return [
        *lock_check_problems(text, "master-quick"),
        *lock_check_problems(text, "everything"),
        *image_check_problems(text),
        *package_lock_problems(text),
        *tz_problems(text),
    ]


# The real tree.


def test_ci_still_has_every_check_this_guard_mirrors():
    assert ci_lock_check() == ("uv", "lock", "--check")
    export, inside, allowed = ci_image_check()
    assert export[:4] == ("uv", "export", "--frozen", "--package")
    assert inside == ("pip", "list", "--format=freeze")
    assert allowed == {"herd-ai-orchestrator", "herd-common", "pip"}
    install, diff = ci_package_lock_check()
    assert (install.cwd, diff.words[-1]) == ("frontend", "package-lock.json")
    assert ci_tz_run()[0] == "America/Los_Angeles"


def test_master_quick_checks_the_lock_before_syncing():
    assert lock_check_problems(MAKEFILE_PATH.read_text(), "master-quick") == []


def test_everything_checks_the_lock_before_syncing():
    assert lock_check_problems(MAKEFILE_PATH.read_text(), "everything") == []


def test_everything_checks_the_built_image_against_the_lock():
    assert image_check_problems(MAKEFILE_PATH.read_text()) == []


def test_everything_checks_package_lock_after_the_frontend_build():
    assert package_lock_problems(MAKEFILE_PATH.read_text()) == []


def test_everything_runs_vitest_in_ci_non_utc_zone():
    assert tz_problems(MAKEFILE_PATH.read_text()) == []


# The scan on synthetic Makefiles: a sound one, then one way per case of naming a
# check without running it.

_LOCK = "\tuv lock --check\n"
_SYNC = "\tuv sync --all-extras\n"
_REST = (
    "\tcd frontend && npm run build\n"
    "\tcd frontend && npm install && git diff --exit-code -- package-lock.json\n"
    "\t$(MAKE) test-frontend-tz\n"
    "\tdocker compose build\n"
    "\t$(MAKE) check-image-lock\n"
    "\t@ok=0; $(MAKE) _master-stack-up && ok=1\n"
)
_HELPERS = (
    "test-frontend-tz:\n"
    "\tcd frontend && TZ=America/Los_Angeles npm test\n"
    "\n"
    "check-image-lock:\n"
    "\t@img=$$(docker compose config --images ai-orchestrator); \\\n"
    "\t\ttmp=$$(mktemp -d); \\\n"
    "\t\tuv export --frozen --package herd-ai-orchestrator --no-hashes \\\n"
    "\t\t\t--no-emit-workspace --no-default-groups --no-header --no-annotate \\\n"
    '\t\t\t> "$$tmp/a.txt" && \\\n'
    '\t\tdocker run --rm "$$img" pip list --format=freeze > "$$tmp/b.txt" && \\\n'
    f'\t\tuv run python {LOCK_SCRIPT} "$$tmp/a.txt" "$$tmp/b.txt" \\\n'
    "\t\t\t--allow herd-ai-orchestrator --allow herd-common --allow pip\n"
    "\n"
    "_master-stack-up:\n"
    "\tdocker compose -p gate up -d --build\n"
)


def _synthetic(everything: str = _LOCK + _SYNC + _REST, quick: str = _LOCK + _SYNC) -> str:
    return (
        f"master-quick:  ## fast\n{quick}\n"
        f"everything: gate-clean  ## full\n{everything}\n"
        f"{_HELPERS}\n"
        "gate-clean:\n\t@true\n"
    )


def test_scan_accepts_the_sound_synthetic_makefile():
    assert all_problems(_synthetic()) == []


def test_scan_accepts_the_tz_run_inline():
    rest = _REST.replace(
        "\t$(MAKE) test-frontend-tz\n", "\tcd frontend && TZ=America/Los_Angeles npm test\n"
    )
    assert tz_problems(_synthetic(everything=_LOCK + _SYNC + rest)) == []


FALSE_PASSES = {
    "master-quick: no lock check": _synthetic(quick=_SYNC),
    "master-quick: lock check after sync": _synthetic(quick=_SYNC + _LOCK),
    "master-quick: lock check commented out": _synthetic(quick="\t# uv lock --check\n" + _SYNC),
    "master-quick: lock check in an echo": _synthetic(quick='\techo "uv lock --check"\n' + _SYNC),
    "everything: no lock check": _synthetic(everything=_SYNC + _REST),
    "everything: lock check after sync": _synthetic(everything=_SYNC + _LOCK + _REST),
    "everything: lock check failure dropped": _synthetic(
        everything="\tuv lock --check || true\n" + _SYNC + _REST
    ),
    "everything: lock check errors ignored": _synthetic(
        everything="\t-uv lock --check\n" + _SYNC + _REST
    ),
    "everything: lock check behind an exit": _synthetic(
        everything="\ttrue || exit 0; uv lock --check\n" + _SYNC + _REST
    ),
    "image check not called": _synthetic(
        everything=_LOCK + _SYNC + _REST.replace("\t$(MAKE) check-image-lock\n", "")
    ),
    "image check called as a dry run": _synthetic(
        everything=_LOCK + _SYNC + _REST.replace("check-image-lock", "-n check-image-lock")
    ),
    "image check call assigns SHELL": _synthetic(
        everything=_LOCK + _SYNC + _REST.replace("check-image-lock", "check-image-lock SHELL=true")
    ),
    "image check before the build": _synthetic(
        everything=_LOCK
        + _SYNC
        + _REST.replace("\tdocker compose build\n\t$(MAKE) check-image-lock\n", "").replace(
            "\t@ok=0", "\t$(MAKE) check-image-lock\n\tdocker compose build\n\t@ok=0"
        )
    ),
    "image check after the stack starts": _synthetic(
        everything=_LOCK
        + _SYNC
        + _REST.replace("\t$(MAKE) check-image-lock\n", "")
        + "\t$(MAKE) check-image-lock\n"
    ),
    "image check drops an --allow": _synthetic().replace(" --allow pip", ""),
    "image check with a target-specific SHELL": _synthetic()
    + "check-image-lock: SHELL := /bin/true\n",
    "image check script status dropped": _synthetic().replace(
        "--allow pip\n", "--allow pip || true\n"
    ),
    "no package-lock diff": _synthetic().replace(
        " && git diff --exit-code -- package-lock.json", ""
    ),
    "package-lock diff without --exit-code": _synthetic().replace(
        "git diff --exit-code --", "git diff --"
    ),
    "package-lock check outside frontend/": _synthetic().replace(
        "\tcd frontend && npm install", "\tnpm install"
    ),
    "package-lock check before the build": _synthetic(
        everything=_LOCK
        + _SYNC
        + _REST.replace("\tcd frontend && npm run build\n", "").replace(
            "\t$(MAKE) test-frontend-tz\n",
            "\t$(MAKE) test-frontend-tz\n\tcd frontend && npm run build\n",
        )
    ),
    "tz run not called": _synthetic(
        everything=_LOCK + _SYNC + _REST.replace("\t$(MAKE) test-frontend-tz\n", "")
    ),
    "tz run under UTC": _synthetic().replace("TZ=America/Los_Angeles", "TZ=UTC"),
    "tz exported, not on the command": _synthetic().replace(
        "\tcd frontend && TZ=America/Los_Angeles npm test",
        "\texport TZ=America/Los_Angeles; cd frontend && npm test",
    ),
    "tz run is watch mode": _synthetic().replace(
        "TZ=America/Los_Angeles npm test", "TZ=America/Los_Angeles npm run test:watch"
    ),
}


# The words each case's refusal must carry, so a case cannot pass on an unrelated
# problem the synthetic edit happened to cause.
_EXPECTED = {
    "master-quick": "master-quick",
    "everything": "uv lock --check",
    "image check": "image check|uv export|docker run|" + LOCK_SCRIPT,
    "package-lock": "package-lock",
    "no package-lock": "package-lock",
    "tz": "TZ=America/Los_Angeles",
}


@pytest.mark.parametrize("case", sorted(FALSE_PASSES))
def test_scan_refuses_a_check_that_does_not_run(case: str):
    problems = all_problems(FALSE_PASSES[case])
    needles = next(v for k, v in _EXPECTED.items() if case.startswith(k)).split("|")
    assert any(needle in problem for problem in problems for needle in needles), (case, problems)
