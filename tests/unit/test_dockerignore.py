"""The build contexts leave out local artifacts, and nothing a Dockerfile copies (issue #1301).

Before #1301 no `.dockerignore` existed, so the frontend image's `COPY . .` copied
the host's `node_modules`, `dist`, `coverage`, and `.env*` files over the image's own
`npm ci` install, and every service build sent the whole checkout (virtualenv, VCS
data, generated keys) as its context. The two ignore files fix that: the repository
root's lets back in only what the service Dockerfiles copy, and `frontend/`'s
excludes what the host installs or builds there.

The invariant pinned here: every source a Dockerfile copies from its build context
exists in the repository, and no tracked file under it is excluded by the ignore file
that applies to that build. A tracked file is what the image is meant to contain;
untracked files under a copied directory (caches, virtualenvs) may be excluded, and
that is the point. The behavior tests below also check that the two files exclude the
artifacts #1301 names.

No Docker: the ignore rules are evaluated here with Docker's documented semantics.
Patterns are anchored at the context root (no implicit `**/`), `*` and `?` stop at
`/`, `**` crosses any number of directories, a leading `/` and redundant path
elements are cleaned away, a pattern that matches a directory also excludes
everything under it, a line starting with `!` re-includes, and the last matching
line wins.

What is read: every `build:` stanza in the compose files `tests/unit/test_image_pins.py`
discovers (the tracked files, through `git ls-files`, inside a git work tree; a
filesystem walk only when git cannot list them, so an untracked local Dockerfile on a
developer host is not judged): its `context` relative to the compose file, its
`dockerfile` relative to the context (`Dockerfile` by default); for each, the ignore
file Docker applies (`<Dockerfile>.dockerignore` beside the Dockerfile when it exists, otherwise
`.dockerignore` at the context root); and in the Dockerfile every COPY and ADD
without `--from=`, each word but the last being a source (flags dropped, the JSON
form read as a list, globs matched against tracked files). Tracked files come from
`git ls-files`.

Limits:

- not seen: a build that no compose file declares (a plain `docker build` with a
  different context or ignore file), a `RUN --mount=type=bind` source, an ADD of a
  URL (skipped), a heredoc COPY, and a source spelled through a build argument
  (refused, not resolved);
- not judged: untracked files, which the invariant leaves to the ignore rules on
  purpose, and the size of the context.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit.test_image_pins import REPO_ROOT, _instructions, compose_files, dockerfiles


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p]


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!") or body.startswith("^"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))


def parse_ignore(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    """(negated, regex) per rule, in file order."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        line = posixpath.normpath(line.lstrip("/"))
        if line == ".":
            continue
        rules.append((negated, _pattern_regex(line)))
    return rules


def is_excluded(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    """Whether Docker leaves `path` (relative to the context, `/`-separated) out."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for negated, regex in rules:
        if any(regex.fullmatch(prefix) for prefix in prefixes):
            excluded = not negated
    return excluded


def _builds() -> list[tuple[Path, Path]]:
    """(context directory, Dockerfile) for every compose build stanza."""
    found: set[tuple[Path, Path]] = set()
    for compose in compose_files():
        data = yaml.safe_load(compose.read_text()) or {}
        for spec in (data.get("services") or {}).values():
            if not isinstance(spec, dict) or "build" not in spec:
                continue
            build = spec["build"]
            if isinstance(build, str):
                build = {"context": build}
            context = (compose.parent / build.get("context", ".")).resolve()
            dockerfile = (context / build.get("dockerfile", "Dockerfile")).resolve()
            found.add((context, dockerfile))
    return sorted(found)


def _ignore_rules(context: Path, dockerfile: Path) -> list[tuple[bool, re.Pattern[str]]]:
    beside = dockerfile.with_name(dockerfile.name + ".dockerignore")
    for candidate in (beside, context / ".dockerignore"):
        if candidate.is_file():
            return parse_ignore(candidate.read_text())
    return []


def copy_sources(text: str) -> list[tuple[int, str]]:
    """(line, source) for every COPY/ADD source read from the build context."""
    found = []
    for line, keyword, arguments in _instructions(text):
        if keyword not in ("COPY", "ADD"):
            continue
        words = arguments.split()
        if any(w.startswith("--from=") for w in words):
            continue
        words = [w for w in words if not w.startswith("--")]
        rest = " ".join(words)
        if rest.startswith("["):
            words = json.loads(rest)
        for source in words[:-1]:
            if keyword == "ADD" and re.match(r"[a-z]+://", source):
                continue
            found.append((line, source))
    return found


def source_problems(
    context_rel: str, sources: list[tuple[int, str]], rules, tracked: list[str]
) -> list[str]:
    """Problems with the sources one Dockerfile copies from one context."""
    prefix = "" if context_rel in ("", ".") else context_rel.rstrip("/") + "/"
    in_context = [p[len(prefix) :] for p in tracked if p.startswith(prefix)]
    problems = []
    for line, source in sources:
        if "$" in source:
            problems.append(f"line {line}: {source!r} is spelled through a variable")
            continue
        clean = posixpath.normpath(source.lstrip("/"))
        if clean == ".":
            selected = in_context
        elif any(ch in clean for ch in "*?["):
            selected = [
                p
                for p in in_context
                if fnmatch.fnmatchcase(p, clean) or fnmatch.fnmatchcase(p.split("/")[0], clean)
            ]
        else:
            selected = [p for p in in_context if p == clean or p.startswith(clean + "/")]
        if not selected:
            problems.append(f"line {line}: COPY source {source!r} matches no tracked file")
            continue
        excluded = [p for p in selected if is_excluded(p, rules)]
        if excluded:
            problems.append(
                f"line {line}: COPY source {source!r} is excluded by the ignore file: "
                f"{excluded[:5]}{' ...' if len(excluded) > 5 else ''}"
            )
    return problems


def test_every_dockerfile_is_built_by_a_compose_file():
    built = {dockerfile for _, dockerfile in _builds()}
    missing = [str(p.relative_to(REPO_ROOT)) for p in dockerfiles() if p.resolve() not in built]
    assert not missing, f"Dockerfiles no compose build declares, so unchecked here: {missing}"


def test_no_copy_source_is_excluded_by_its_dockerignore():
    tracked = _tracked_files()
    problems = []
    for context, dockerfile in _builds():
        rules = _ignore_rules(context, dockerfile)
        context_rel = str(context.relative_to(REPO_ROOT)) if context != REPO_ROOT else ""
        for problem in source_problems(
            context_rel, copy_sources(dockerfile.read_text()), rules, tracked
        ):
            problems.append(f"{dockerfile.relative_to(REPO_ROOT)}: {problem}")
    assert not problems, "\n".join(problems)


ROOT_RULES = parse_ignore((REPO_ROOT / ".dockerignore").read_text())
FRONTEND_RULES = parse_ignore((REPO_ROOT / "frontend" / ".dockerignore").read_text())


@pytest.mark.parametrize(
    "path",
    [
        ".git/HEAD",
        ".venv/bin/python",
        ".env",
        ".env.local",
        "infra/traefik/certs/server.key",
        "infra/traefik/certs/server-chain.crt",
        "frontend/node_modules/react/index.js",
        "htmlcov/index.html",
        "coverage.xml",
        ".coverage",
        "services/auth/.venv/bin/python",
        "services/auth/app/__pycache__/main.cpython-312.pyc",
        "services/common/herd_common/.pytest_cache/v/x",
        "services/auth/.env",
        "services/auth/htmlcov/index.html",
        "services/auth/tests/certs/server.key",
        "docs/manual/node_modules/x.js",
    ],
)
def test_root_context_excludes_local_artifacts(path):
    assert is_excluded(path, ROOT_RULES)


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "services/auth/app/main.py",
        "services/common/herd_common/database.py",
        "services/inventory/.coveragerc",
        "docs/manual/index.html",
    ],
)
def test_root_context_keeps_what_the_services_copy(path):
    assert not is_excluded(path, ROOT_RULES)


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/.herd-marker",
        "node_modules/react/index.js",
        "dist/index.html",
        "coverage/lcov.info",
        ".env",
        ".env.local",
        ".env.production.local",
        "tsconfig.tsbuildinfo",
    ],
)
def test_frontend_context_excludes_host_installs_and_builds(path):
    assert is_excluded(path, FRONTEND_RULES)


@pytest.mark.parametrize(
    "path",
    [".npmrc", "package.json", "package-lock.json", "src/main.tsx", "nginx.conf", ".env.example"],
)
def test_frontend_context_keeps_sources_and_npmrc(path):
    assert not is_excluded(path, FRONTEND_RULES)


def test_matcher_follows_docker_semantics():
    rules = parse_ignore("# c\n*\n!keep\n**/junk\n/abs/x\nsub/*.tmp\n!sub/ok.tmp\n")
    assert is_excluded("anything", rules)
    assert is_excluded("dir/deep/file", rules)  # the top-level dir matches `*`
    assert not is_excluded("keep/file", rules)
    assert is_excluded("keep/a/junk/file", rules)
    assert is_excluded("abs/x/y", rules)
    anchored = parse_ignore("node_modules\nsub/*.tmp\n!sub/ok.tmp\n")
    assert is_excluded("node_modules/a", anchored)
    assert not is_excluded("frontend/node_modules/a", anchored)  # no implicit **/
    assert is_excluded("sub/a.tmp", anchored)
    assert not is_excluded("sub/ok.tmp", anchored)
    assert not is_excluded("sub/deeper/a.tmp", anchored)  # * stops at /


def test_source_check_reports_excluded_missing_and_globbed_sources():
    tracked = ["ctx/app/main.py", "ctx/app/x.pyc", "ctx/package.json", "ctx/package-lock.json"]
    rules = parse_ignore("**/*.pyc\n")
    sources = [(1, "app/"), (2, "package.json"), (3, "package-lock.json*"), (4, "gone/")]
    assert source_problems("ctx", sources, rules, tracked) == [
        "line 1: COPY source 'app/' is excluded by the ignore file: ['app/x.pyc']",
        "line 4: COPY source 'gone/' matches no tracked file",
    ]
    assert source_problems("ctx", [(1, "app/main.py")], rules, tracked) == []
