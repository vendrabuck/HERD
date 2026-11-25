"""No tracked path is a generated artifact (issue #1520).

The repository's `.gitignore` is deliberately untracked, so a clone or a fork has no
ignore rules at all, and `git add -A` there stages whatever the host generated. On
2026-11-23 an external pull request (#1503, head bc274d23) committed 154 compiled
`__pycache__` files in 28 directories: in eleven of the twelve services (all but
`config`), in `services/common/`, and in the repo-root `tests/`. Its only red check
was the format step, and nothing that step skipped would have refused a tracked
generated file either. This guard does.

The invariant: no path that `git ls-files` lists is a generated artifact. A path is
one when it matches a rule of `RULES`, judged on whole path components and exact
names, never on substrings:

- a path component (a directory, or the file name itself) named exactly
  `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `node_modules`,
  `.venv`, `.env`, or `htmlcov` (so a `.env` file at any depth, and a virtualenv
  named `.env`, are both refused);
- a path component ending in `.egg-info`;
- a file name ending in `.pyc`, `.pyo`, or `.pyd`;
- a file named exactly `.coverage`, or `.coverage.` followed by at least one more
  character (the parallel-mode data files), at any depth;
- a file named exactly `.DS_Store` at any depth.

The last three rules read the file name only: a directory named `x.pyc`, `.coverage`,
`.coverage.ci`, or `.DS_Store` is not refused for its name.

So `docs/pyc_notes.md`, `services/x/cache/y.py`, `.env.example`, `my.pycx`,
`node_modules_notes.md`, `services/inventory/.coveragerc`, and a file named `build`
all pass.

Deliberately not in the set: `dist` and `build` as directory names, which are too
generic to ban by name (a source tree may hold a real `build` package or a `dist`
fixture), and the frontend's `coverage` directory for the same reason.

What is read: `git -C <repo root> ls-files -z` (the index, so a staged file counts and
an untracked one does not), decoded as UTF-8 with surrogate escapes, so a name with
spaces, newlines, or non-ASCII characters is judged as written. The guard never
passes vacuously: it FAILS, and does not skip, when git is not on PATH, when the
repository root is not the top level of a git work tree, or when `ls-files` lists no
path. The `backend` CI job checks the repository out with `actions/checkout`, which
makes a git work tree with a shallow history (`ls-files` reads only the index) when
git is on the runner and an archive without `.git` otherwise, which this guard then
refuses; it runs this file through `make coverage-parallel`, whose `test-root`
prerequisite runs `tests/unit/`.

Limits:

- not seen: a generated file whose name no rule names (a `dist` or `build` output, a
  compiled extension other than `.pyd` such as `.so`, an editor swap file), and a
  generated file committed and then removed, which history keeps but the index does
  not;
- not refused by design: `.env.local`, `.env.production`, `.env.example`, and every
  other `.env.<name>` file, since only a component named exactly `.env` is a rule;
- names are matched case-sensitively, so `x.PYC`, `.ENV`, and `__PYCACHE__` pass;
- the git environment of the caller is used as given, so `GIT_DIR` or
  `GIT_INDEX_FILE` set by a hook changes which index is read.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

GENERATED_DIRECTORY_NAMES = (
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".venv",
    ".env",
    "htmlcov",
)
BYTECODE_SUFFIXES = (".pyc", ".pyo", ".pyd")
COVERAGE_DATA_NAME = ".coverage"
LISTED_OFFENDERS_CAP = 50

REMEDY = (
    "Remove them from the index with `git rm -r --cached <path>` (the files stay on "
    "disk) and amend the commit that added them (then `git push --force-with-lease` "
    "if it was already pushed), or commit the removal. This repository tracks no "
    "`.gitignore`, so add local patterns such as `__pycache__/` to `.git/info/exclude` "
    "in your clone (in a linked worktree, the path `git rev-parse --git-path "
    "info/exclude` prints) to keep them from being staged again."
)


def _any_component_named(name: str) -> Callable[[list[str]], bool]:
    return lambda parts: name in parts


def _file_name_is(name: str) -> Callable[[list[str]], bool]:
    return lambda parts: parts[-1] == name


def _file_suffix(suffix: str) -> Callable[[list[str]], bool]:
    return lambda parts: parts[-1].endswith(suffix)


def _egg_info(parts: list[str]) -> bool:
    return any(part.endswith(".egg-info") for part in parts)


def _parallel_coverage_data(parts: list[str]) -> bool:
    prefix = COVERAGE_DATA_NAME + "."
    return parts[-1].startswith(prefix) and len(parts[-1]) > len(prefix)


# (label, predicate over the path's `/`-separated components), checked in order; the
# first match names the rule a path broke.
RULES: tuple[tuple[str, Callable[[list[str]], bool]], ...] = (
    *((f"a `{name}` component", _any_component_named(name)) for name in GENERATED_DIRECTORY_NAMES),
    ("a component ending `.egg-info`", _egg_info),
    *((f"a `{suffix}` file", _file_suffix(suffix)) for suffix in BYTECODE_SUFFIXES),
    ("a `.coverage` data file", _file_name_is(COVERAGE_DATA_NAME)),
    ("a `.coverage.<suffix>` data file", _parallel_coverage_data),
    ("a `.DS_Store` file", _file_name_is(".DS_Store")),
)


class GuardCannotRun(RuntimeError):
    """The tracked paths could not be listed, so the guard would judge nothing."""


def generated_artifacts(paths: Sequence[str]) -> list[tuple[str, str]]:
    """(path, label of the first rule it breaks) for every generated path, in order."""
    found = []
    for path in paths:
        parts = path.split("/")
        for label, matches in RULES:
            if matches(parts):
                found.append((path, label))
                break
    return found


def tracked_paths(
    root: Path, *, git: str = "git", env: Mapping[str, str] | None = None
) -> list[str]:
    """Every path `git ls-files` lists for `root`; raises GuardCannotRun, never returns []."""

    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [git, "-C", str(root), *args], capture_output=True, env=env, check=False
            )
        except OSError as exc:
            raise GuardCannotRun(
                f"cannot run {git!r} ({exc.__class__.__name__}), so the tracked files "
                "cannot be listed; this guard needs git on PATH and a git work tree"
            ) from exc

    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        reason = top.stderr.decode(errors="replace").strip()
        raise GuardCannotRun(
            f"{root} is not inside a git work tree ({reason}); "
            "this guard needs a checkout made by git, not an exported archive"
        )
    toplevel = Path(top.stdout.decode("utf-8", "surrogateescape").strip())
    if toplevel.resolve() != root.resolve():
        raise GuardCannotRun(
            f"{root} is not the top level of its git work tree ({toplevel} is), so "
            "`git ls-files` would list only part of what is tracked"
        )
    listed = run("ls-files", "-z")
    if listed.returncode != 0:
        raise GuardCannotRun(
            f"`git ls-files` failed: {listed.stderr.decode(errors='replace').strip()}"
        )
    paths = [p for p in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    if not paths:
        raise GuardCannotRun(f"`git ls-files` lists no tracked path under {root}")
    return paths


def failure_message(offenders: Sequence[tuple[str, str]]) -> str:
    total = len(offenders)
    lines = [f"{total} tracked path(s) are generated artifacts:"]
    lines += [f"  {path}  ({label})" for path, label in offenders[:LISTED_OFFENDERS_CAP]]
    if total > LISTED_OFFENDERS_CAP:
        lines.append(f"  ... and {total - LISTED_OFFENDERS_CAP} more")
    lines.append(REMEDY)
    return "\n".join(lines)


def test_no_tracked_path_is_a_generated_artifact():
    try:
        paths = tracked_paths(REPO_ROOT)
    except GuardCannotRun as exc:
        pytest.fail(f"the tracked-files guard cannot run: {exc}", pytrace=False)
    offenders = generated_artifacts(paths)
    assert not offenders, failure_message(offenders)


# The guard itself, in both directions. Each refused path below breaks exactly one
# rule, so removing any one rule from RULES fails its case.
@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("services/auth/app/__pycache__/README", "a `__pycache__` component"),
        ("__pycache__", "a `__pycache__` component"),
        ("services/auth/.pytest_cache/v/cache/nodeids", "a `.pytest_cache` component"),
        (".ruff_cache/0.15.0/123", "a `.ruff_cache` component"),
        ("services/x/.mypy_cache/3.12/app.json", "a `.mypy_cache` component"),
        ("frontend/node_modules/react/index.js", "a `node_modules` component"),
        ("docs/manual/node_modules/x/package.json", "a `node_modules` component"),
        (".venv/bin/python", "a `.venv` component"),
        ("services/auth/.venv/lib/site.py", "a `.venv` component"),
        ("htmlcov/index.html", "a `htmlcov` component"),
        ("services/auth/htmlcov/status.json", "a `htmlcov` component"),
        ("services/common/herd_common.egg-info/PKG-INFO", "a component ending `.egg-info`"),
        ("herd.egg-info", "a component ending `.egg-info`"),
        ("services/auth/app/main.cpython-314.pyc", "a `.pyc` file"),
        ("m.pyc", "a `.pyc` file"),
        ("services/auth/app/main.pyo", "a `.pyo` file"),
        ("lib/speedups.cp312-win_amd64.pyd", "a `.pyd` file"),
        (".coverage", "a `.coverage` data file"),
        ("services/auth/.coverage", "a `.coverage` data file"),
        (".coverage.host.1234.567890", "a `.coverage.<suffix>` data file"),
        ("services/auth/.coverage.x", "a `.coverage.<suffix>` data file"),
        (".env", "a `.env` component"),
        ("services/auth/.env", "a `.env` component"),
        (".env/lib/python3.12/site-packages/x.py", "a `.env` component"),
        ("services/auth/.env/bin/python", "a `.env` component"),
        (".DS_Store", "a `.DS_Store` file"),
        ("frontend/src/.DS_Store", "a `.DS_Store` file"),
    ],
)
def test_each_rule_refuses_its_generated_path(path, label):
    assert generated_artifacts([path]) == [(path, label)]


@pytest.mark.parametrize(
    "path",
    [
        # near misses of the component rules: a substring is not the component
        "docs/pyc_notes.md",
        "services/x/cache/y.py",
        "services/x/__pycache__.md",
        "services/x/pycache/y.py",
        "pytest_cache/notes.md",
        "docs/.ruff_cache_notes.md",
        "mypy_cache.py",
        "node_modules_notes.md",
        "docs/node_module/x.js",
        "venv/notes.md",
        ".venv.example",
        "htmlcov.md",
        "docs/htmlcov-guide/index.md",
        # near misses of `.egg-info`
        "docs/egg-info.md",
        "herd.egg-info.md",
        "egg_info/x.py",
        # near misses of the bytecode suffixes
        "my.pycx",
        "docs/pyc",
        "docs/notes.pyc.md",
        "app/main.py",
        "app/a.pyi",
        "doc.pyo.txt",
        "pyd",
        # the file-name rules read the file name only, never a directory's
        "x.pyc/notes.md",
        "x.pyo/notes.md",
        "x.pyd/notes.md",
        ".coverage/x",
        ".coverage.ci/x",
        ".DS_Store/x",
        # near misses of the coverage data files
        "services/inventory/.coveragerc",
        ".coverage.",
        "coverage",
        "coverage.xml",
        "frontend/coverage/lcov.info",
        "services/auth/tests/test_coverage_gaps.py",
        # near misses of `.env`
        ".env.example",
        "frontend/.env.example",
        ".env.local.example",
        "env",
        "services/auth/app/.envrc",
        "docs/.env-notes.md",
        ".env.local",
        ".env.production",
        "services/auth/.env.test",
        # names are matched case-sensitively
        "x.PYC",
        ".ENV",
        "__PYCACHE__/x.py",
        # near misses of `.DS_Store`
        "DS_Store",
        ".DS_Store.md",
        # deliberately not in the set
        "build",
        "dist",
        "frontend/dist/index.html",
        "services/x/build/lib.py",
        # spaces and non-ASCII characters are just characters
        "docs/a file with spaces.md",
        "docs/résumé.md",
    ],
)
def test_near_miss_passes(path):
    assert generated_artifacts([path]) == []


def test_every_offender_is_reported_in_order():
    paths = ["a/__pycache__/x.pyc", "ok.py", ".env", "b/m.pyc", ".env.example"]
    assert generated_artifacts(paths) == [
        ("a/__pycache__/x.pyc", "a `__pycache__` component"),
        (".env", "a `.env` component"),
        ("b/m.pyc", "a `.pyc` file"),
    ]


def test_failure_message_caps_the_list_and_gives_the_total_and_the_remedy():
    offenders = [
        (f"services/s/__pycache__/m{i}.pyc", "a `__pycache__` component") for i in range(154)
    ]
    message = failure_message(offenders)
    assert message.startswith("154 tracked path(s) are generated artifacts:")
    assert "services/s/__pycache__/m0.pyc  (a `__pycache__` component)" in message
    assert f"m{LISTED_OFFENDERS_CAP - 1}.pyc" in message
    assert f"m{LISTED_OFFENDERS_CAP}.pyc" not in message
    assert f"... and {154 - LISTED_OFFENDERS_CAP} more" in message
    assert "git rm -r --cached" in message
    assert ".git/info/exclude" in message


def test_failure_message_lists_a_short_list_whole():
    message = failure_message([(".env", "a `.env` component")])
    assert message.splitlines()[:2] == [
        "1 tracked path(s) are generated artifacts:",
        "  .env  (a `.env` component)",
    ]
    assert "more" not in message


def test_the_cap_is_fifty_as_documented():
    assert LISTED_OFFENDERS_CAP == 50


@pytest.mark.parametrize(("count", "listed", "more_line"), [(50, 50, None), (51, 50, "1 more")])
def test_failure_message_lists_up_to_the_cap_exactly(count, listed, more_line):
    offenders = [(f"p{i}.pyc", "a `.pyc` file") for i in range(count)]
    lines = failure_message(offenders).splitlines()
    path_lines = [line for line in lines if line.startswith("  p")]
    assert path_lines == [f"  p{i}.pyc  (a `.pyc` file)" for i in range(listed)]
    more = [line for line in lines if line.startswith("  ... and ")]
    assert more == ([] if more_line is None else [f"  ... and {more_line}"])


# Reading the tracked paths: a throwaway repository in tmp_path, with a git
# environment that cannot reach the real checkout.
def _git_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["HOME"] = str(tmp_path)
    return env


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def _write(repo: Path, rel: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")


def test_guard_refuses_a_tracked_pyc_in_a_real_repository(tmp_path):
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q")
    names = [
        "pkg/mod.py",
        "pkg/__pycache__/mod.cpython-314.pyc",
        "docs/pyc_notes.md",
        ".env.example",
        "notes with spaces/résumé.md",
        "dir with spaces/café.pyc",
    ]
    for name in names:
        _write(repo, name)
    _git(repo, env, "add", "--", *names)
    paths = tracked_paths(repo, env=env)
    assert sorted(paths) == sorted(names)
    assert sorted(generated_artifacts(paths)) == [
        ("dir with spaces/café.pyc", "a `.pyc` file"),
        ("pkg/__pycache__/mod.cpython-314.pyc", "a `__pycache__` component"),
    ]


def test_a_tracked_name_that_is_not_utf8_is_judged_and_named_exactly(tmp_path):
    # A Latin-1 name, put in the index without creating it on disk. Strict decoding
    # would crash the guard; replacement decoding would still refuse the `.pyc` but
    # report a name that is not the tracked one, so the test reads the bytes back.
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q")
    blob = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=b"x",
            env=env,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    raw = b"pkg/caf\xe9.pyc"
    subprocess.run(
        [b"git", b"-C", os.fsencode(repo), b"update-index", b"--add", b"--cacheinfo"]
        + [b"100644," + blob.encode() + b"," + raw],
        env=env,
        check=True,
        capture_output=True,
    )
    _write(repo, "ok.py")
    _git(repo, env, "add", "--", "ok.py")
    offenders = generated_artifacts(tracked_paths(repo, env=env))
    assert [(path.encode("utf-8", "surrogateescape"), label) for path, label in offenders] == [
        (raw, "a `.pyc` file")
    ]


def test_an_untracked_generated_file_is_not_judged(tmp_path):
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q")
    _write(repo, "app.py")
    _write(repo, "__pycache__/app.cpython-314.pyc")
    _git(repo, env, "add", "--", "app.py")
    assert tracked_paths(repo, env=env) == ["app.py"]


def test_guard_fails_when_git_is_missing(tmp_path):
    with pytest.raises(GuardCannotRun, match="cannot run .* needs git on PATH"):
        tracked_paths(tmp_path, git=str(tmp_path / "no-such-git"), env=_git_env(tmp_path))


def test_guard_fails_outside_a_work_tree(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GuardCannotRun, match="is not inside a git work tree"):
        tracked_paths(plain, env=_git_env(tmp_path))


def test_guard_fails_below_the_top_level_of_a_work_tree(tmp_path):
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    _git(repo, env, "init", "-q")
    _write(repo, "sub/ok.py")
    _write(repo, "__pycache__/m.pyc")
    _git(repo, env, "add", "--", "sub/ok.py", "__pycache__/m.pyc")
    with pytest.raises(GuardCannotRun, match="is not the top level of its git work tree"):
        tracked_paths(repo / "sub", env=env)


def test_guard_fails_when_ls_files_fails(tmp_path):
    stub = tmp_path / "git"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$3" in\n'
        '  rev-parse) echo "$2"; exit 0 ;;\n'
        '  ls-files) echo "fatal: index file corrupt" >&2; exit 128 ;;\n'
        "esac\n"
        "exit 2\n"
    )
    stub.chmod(0o755)
    with pytest.raises(GuardCannotRun, match="`git ls-files` failed: fatal: index file corrupt"):
        tracked_paths(tmp_path, git=str(stub), env=_git_env(tmp_path))


def test_guard_fails_when_nothing_is_tracked(tmp_path):
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q")
    _write(repo, "__pycache__/m.pyc")
    with pytest.raises(GuardCannotRun, match="lists no tracked path"):
        tracked_paths(repo, env=env)


def test_the_repository_check_fails_rather_than_skips_when_it_cannot_list(monkeypatch):
    def cannot(root, **kwargs):
        raise GuardCannotRun("simulated")

    monkeypatch.setattr(f"{__name__}.tracked_paths", cannot)
    # A skip is an outcome exception too (a BaseException); one escaping here would mark
    # this test skipped rather than failed, so catch every outcome and name it.
    with pytest.raises(BaseException) as raised:
        test_no_tracked_path_is_a_generated_artifact()
    assert raised.type is pytest.fail.Exception, f"got {raised.type.__name__}, not a failure"
    assert "the tracked-files guard cannot run: simulated" in str(raised.value)


def test_the_repository_check_fails_on_an_offender_anywhere_in_the_listing(monkeypatch):
    monkeypatch.setattr(
        f"{__name__}.tracked_paths", lambda root, **kwargs: ["ok.py", "x/__pycache__/m.pyc"]
    )
    with pytest.raises(AssertionError) as raised:
        test_no_tracked_path_is_a_generated_artifact()
    message = str(raised.value)
    assert "1 tracked path(s) are generated artifacts:" in message
    assert "  x/__pycache__/m.pyc  (a `__pycache__` component)" in message
    assert REMEDY in message


def test_the_repository_check_passes_a_clean_listing(monkeypatch):
    monkeypatch.setattr(
        f"{__name__}.tracked_paths", lambda root, **kwargs: ["ok.py", ".env.example"]
    )
    test_no_tracked_path_is_a_generated_artifact()
