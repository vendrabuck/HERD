"""Static pin for the build-identifier plumbing (issue #846): HERD_BUILD and
HERD_BUILD_DATE, computed on the host by the Makefile, must reach every image the
stack builds as Docker build args, and every Dockerfile must declare them as late as
possible so a changed build string invalidates only a metadata layer, never the slow
dependency-install layers above it.

No stack, no Docker: this only parses docker-compose.yml and every checked-in
Dockerfile as text, in the same style as tests/unit/test_compose_settings_wiring.py
and tests/unit/test_nos_lab_ci_wiring.py.

The Dockerfile checks pin POSITION, not presence (issue #1158). Each file is read as
instructions grouped by build stage (keyword and arguments split on any whitespace,
continuations joined, comments dropped). For a service, in the stage that ships: each
of the two variables is declared by exactly one ARG instruction, with the pinned
text; exactly one ENV instruction sets them, with the pinned text, after both ARG
lines; and no RUN, COPY, or ADD follows the first ARG line, which covers an install
under any spelling. A RUN that needs the value after the ARG is refused by design:
the ENV line already puts the value in the image. For the frontend, in the stage
that runs `npm run build`: the same ARG and ENV rules, all three lines ahead of the
build, and after that stage's `npm ci` or `npm install` when it has one. The detector
tests at the end feed the same two scans a clean Dockerfile and every arrangement
that keeps all the expected lines while breaking what they are for.

Limits. The file is read as text, not built:

- not seen: an ARG block inside a heredoc body, a shell-form CMD or ENTRYPOINT that
  overrides or unsets the variable, `RUN VITE_HERD_BUILD=dev npm run build`, a
  build run through a wrapper script, a served frontend stage that copies `dist`
  from another stage, a parser `escape` directive, and anything compose sets at
  run time;
- read conservatively (refused though it may be sound), exact text kept from the
  original guard: a quoted default (`ARG HERD_BUILD="dev"`), both variables on one
  ARG instruction, an ENV line that carries a third variable or is split in two, an
  `${VAR:-default}` expansion in the ENV line, and any COPY or RUN after the ARG.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
SERVICE_DOCKERFILES = sorted((REPO_ROOT / "services").glob("*/Dockerfile"))

# Exact defaults the interface contract pins: unset means the literal "dev" for the
# build string and empty for the date.
BACKEND_ARGS = {
    "HERD_BUILD": "${HERD_BUILD:-dev}",
    "HERD_BUILD_DATE": "${HERD_BUILD_DATE:-}",
}
FRONTEND_ARGS = {
    "VITE_HERD_BUILD": "${HERD_BUILD:-dev}",
    "VITE_HERD_BUILD_DATE": "${HERD_BUILD_DATE:-}",
}


def _compose_services() -> dict:
    data = yaml.safe_load(COMPOSE_PATH.read_text())
    return data["services"]


def _services_with_build_stanza() -> dict:
    return {
        name: definition
        for name, definition in _compose_services().items()
        if isinstance(definition, dict) and "build" in definition
    }


def test_every_build_service_is_covered_by_this_test():
    """Guards against a new service being added with no build: stanza noticed, or an
    existing one being renamed and silently dropped out of the checks below."""
    services = _services_with_build_stanza()
    assert len(services) == 13, (
        f"expected 12 backend services plus frontend (13 build stanzas), found "
        f"{len(services)}: {sorted(services)}. If a service was added or removed, "
        "update this count and the checks in this file deliberately."
    )
    assert "frontend" in services


def test_every_compose_build_stanza_carries_the_build_args():
    services = _services_with_build_stanza()
    missing = []
    wrong_default = []
    for name, definition in services.items():
        args = definition["build"].get("args") or {}
        expected = FRONTEND_ARGS if name == "frontend" else BACKEND_ARGS
        for key, expected_value in expected.items():
            if key not in args:
                missing.append(f"{name}:{key}")
            elif str(args[key]) != expected_value:
                wrong_default.append(f"{name}:{key} is {args[key]!r}, expected {expected_value!r}")
    assert not missing, (
        "these compose services are missing a build arg the frontend/backend "
        f"images need to be stamped with a build identifier: {missing}"
    )
    assert not wrong_default, (
        "these compose build args do not carry the exact ${VAR:-default} the "
        f"interface contract pins (unset must mean dev / empty): {wrong_default}"
    )


def dockerfile_stages(text: str) -> list[list[tuple[int, str, str]]]:
    """The build stages of a Dockerfile, each a list of (line, INSTRUCTION, arguments).

    Backslash continuations are joined, comments and blank lines dropped (also
    inside a continued instruction, as Docker does), the keyword split from its
    arguments at the first run of whitespace (a space or a tab) and upper-cased, and
    runs of whitespace in the arguments collapsed. A new stage starts at every FROM;
    instructions ahead of the first FROM (global ARGs) form no stage."""
    stages: list[list[tuple[int, str, str]]] = []
    logical: list[tuple[int, str]] = []
    pending, first_line = "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not pending:
            first_line = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical.append((first_line, pending + line))
        pending = ""
    if pending.strip():
        # A continuation that runs off the end of the file is still an instruction.
        logical.append((first_line, pending))
    for first_line, text in logical:
        keyword, *rest = text.split(None, 1)
        arguments = " ".join(rest[0].split()) if rest else ""
        if keyword.upper() == "FROM":
            stages.append([])
        if stages:
            stages[-1].append((first_line, keyword.upper(), arguments))
    return stages


def _ordering_problems(
    stage: list[tuple[int, str, str]], args: tuple[str, str], env: str
) -> tuple[list[str], list[int]]:
    """Problems with one stage's two ARG lines and its ENV line, and the positions
    of the two ARG instructions when both were found exactly once.

    The ENV line is what carries the values into the stage's environment, so it
    must come after BOTH ARG lines (ahead of an ARG its `${...}` expands empty),
    and nothing else may set either variable."""
    problems: list[str] = []
    arg_positions: list[int] = []
    for arg in args:
        name = arg.split("=")[0]
        declared = [
            i
            for i, (_, keyword, value) in enumerate(stage)
            if keyword == "ARG" and name in (word.split("=")[0] for word in value.split())
        ]
        positions = [i for i in declared if stage[i][2] == arg]
        if len(positions) != 1 or len(declared) != 1:
            problems.append(
                f"expected exactly one ARG instruction declaring {name}, reading 'ARG {arg}'; "
                f"found {[f'line {stage[i][0]}: ARG {stage[i][2]}' for i in declared]}"
            )
        arg_positions += positions
    names = [arg.split("=")[0] for arg in args]
    setters = [
        i
        for i, (_, keyword, value) in enumerate(stage)
        if keyword == "ENV" and any(re.search(rf"(^|\s){name}[=\s]", value) for name in names)
    ]
    env_positions = [i for i in setters if stage[i][2] == env]
    if len(env_positions) != 1 or len(setters) != 1:
        problems.append(
            f"expected exactly one ENV instruction setting {' and '.join(names)}, reading "
            f"'ENV {env}'; found {[f'line {stage[i][0]}: ENV {stage[i][2]}' for i in setters]}"
        )
    elif len(arg_positions) == 2 and env_positions[0] < max(arg_positions):
        problems.append(
            f"the ENV line (line {stage[env_positions[0]][0]}) must come after both ARG "
            "lines; ahead of an ARG it stores an empty value"
        )
    return problems, arg_positions if len(arg_positions) == 2 else []


_BACKEND_ARG_LINES = ("HERD_BUILD=dev", "HERD_BUILD_DATE=")
_BACKEND_ENV_LINE = "HERD_BUILD=${HERD_BUILD} HERD_BUILD_DATE=${HERD_BUILD_DATE}"
_FRONTEND_ARG_LINES = ("VITE_HERD_BUILD=dev", "VITE_HERD_BUILD_DATE=")
_FRONTEND_ENV_LINE = (
    "VITE_HERD_BUILD=${VITE_HERD_BUILD} VITE_HERD_BUILD_DATE=${VITE_HERD_BUILD_DATE}"
)
# `npm run build`, not `npm run build:docs` or `npm run build-storybook`.
_NPM_RUN_BUILD = re.compile(r"\bnpm run build(?![\w:.-])")
# Instructions that build a filesystem layer. One of these after the build ARGs is
# rebuilt on every commit, because the ARG value (and the ENV that follows) is part
# of its cache key.
_LAYER_INSTRUCTIONS = {"RUN", "COPY", "ADD"}


def service_build_arg_problems(text: str) -> list[str]:
    """Why a service Dockerfile does not stamp the build identifier into the image
    it ships, as late as possible; empty when it does (issue #846).

    Judged on the FINAL stage, the one that becomes the image: an ARG or ENV in an
    earlier stage does not reach it. The stage must declare each ARG once, carry
    them into ENV after both, set neither variable anywhere else, and build no layer
    (RUN, COPY, ADD, under any spelling of the command) after the first ARG. Where
    the dependencies are installed is not asked: with no layer allowed after the
    ARG lines, every install is ahead of them, whatever it is called and whichever
    stage it runs in."""
    stages = dockerfile_stages(text)
    if not stages:
        return ["no FROM instruction"]
    stage = stages[-1]
    problems, arg_positions = _ordering_problems(stage, _BACKEND_ARG_LINES, _BACKEND_ENV_LINE)
    if not arg_positions:
        return problems
    first_arg = min(arg_positions)
    for line, keyword, value in stage[first_arg + 1 :]:
        if keyword in _LAYER_INSTRUCTIONS:
            problems.append(
                f"line {line}: '{keyword} {value[:60]}' comes after the HERD_BUILD ARG lines "
                f"(line {stage[first_arg][0]}), so a build-string change rebuilds it; the "
                "ARG lines must come after the last install and every other layer"
            )
    return problems


def frontend_build_arg_problems(text: str) -> list[str]:
    """Why the frontend Dockerfile does not bake the build identifier into the Vite
    build while keeping the dependency install cached; empty when it does.

    Judged on the stage that runs `npm run build`: each ARG once, the ENV line
    after both, all three ahead of the build (Vite reads the environment then) and
    after that stage's `npm ci`/`npm install` RUN when it has one (a stage that
    copies `node_modules` from a deps stage has none)."""
    builds = [
        (stage, index)
        for stage in dockerfile_stages(text)
        for index, (_, keyword, value) in enumerate(stage)
        if keyword == "RUN" and _NPM_RUN_BUILD.search(value)
    ]
    if len(builds) != 1:
        return [f"expected exactly one 'RUN npm run build', found {len(builds)}"]
    stage, build = builds[0]
    problems, arg_positions = _ordering_problems(stage, _FRONTEND_ARG_LINES, _FRONTEND_ENV_LINE)
    if problems:
        return problems
    env = next(i for i, (_, k, v) in enumerate(stage) if (k, v) == ("ENV", _FRONTEND_ENV_LINE))
    if not env < build:
        problems.append(
            "the VITE_HERD_BUILD ARG and ENV lines must come before 'RUN npm run build', "
            "or Vite never sees them"
        )
    installs = [
        i
        for i, (_, k, v) in enumerate(stage)
        if k == "RUN" and re.search(r"\bnpm (ci|install)\b", v)
    ]
    if installs and max(installs) > min(arg_positions):
        problems.append(
            "the VITE_HERD_BUILD ARG lines must come after the npm ci/install layer, or a "
            "build-string change reinstalls the dependencies"
        )
    return problems


def test_every_service_dockerfile_declares_the_build_args_and_env():
    assert len(SERVICE_DOCKERFILES) == 12, (
        f"expected 12 services/*/Dockerfile files, found {len(SERVICE_DOCKERFILES)}: "
        f"{[str(p) for p in SERVICE_DOCKERFILES]}"
    )
    problems = [
        f"{dockerfile.relative_to(REPO_ROOT)}: {problem}"
        for dockerfile in SERVICE_DOCKERFILES
        for problem in service_build_arg_problems(dockerfile.read_text())
    ]
    assert not problems, "\n".join(problems)


def test_frontend_dockerfile_declares_vite_build_args_before_npm_run_build():
    problems = frontend_build_arg_problems(FRONTEND_DOCKERFILE.read_text())
    assert not problems, "frontend/Dockerfile: " + "\n".join(problems)


def test_compose_builds_the_dockerfiles_this_test_reads():
    """The position checks above mean nothing for a service compose builds from
    another file, or from an earlier stage of the same file."""
    checked = {p.resolve() for p in (*SERVICE_DOCKERFILES, FRONTEND_DOCKERFILE)}
    problems = []
    for name, definition in _services_with_build_stanza().items():
        build = definition["build"]
        if not isinstance(build, dict):
            build = {"context": build}
        dockerfile = (
            COMPOSE_PATH.parent / build.get("context", ".") / build.get("dockerfile", "Dockerfile")
        ).resolve()
        if dockerfile not in checked:
            problems.append(f"{name} builds from {dockerfile}, which this test does not read")
        if "target" in build:
            problems.append(f"{name} builds stage {build['target']!r}, not the final stage")
    assert not problems, "\n".join(problems)


# The two scans on synthetic Dockerfiles (issue #1158): a clean file passes, and
# each way of keeping every expected line while breaking what the lines are for
# must be flagged.

_ARGS = "ARG HERD_BUILD=dev\nARG HERD_BUILD_DATE=\n"
_ENV = "ENV HERD_BUILD=${HERD_BUILD} HERD_BUILD_DATE=${HERD_BUILD_DATE}\n"
_INSTALL = (
    "RUN uv export --frozen -o requirements.txt \\\n"
    "    && uv pip install --system -r requirements.txt\n"
)
GOOD_SERVICE = (
    "# `RUN uv pip install` is described in this comment only.\n"
    "FROM python:3.12-slim\n"
    "WORKDIR /app\n"
    + _INSTALL
    + "COPY services/x/app/ app/\n"
    + "RUN uv pip install --system --no-deps -e .\n"
    + "RUN useradd appuser\n"
    + "# Build identifier, as late as possible.\n"
    + _ARGS
    + _ENV
    + "USER appuser\n"
    + "EXPOSE 8000\n"
    + 'CMD ["uvicorn", "app.main:app"]\n'
)


def _swap(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def test_service_scan_accepts_a_clean_dockerfile():
    assert service_build_arg_problems(GOOD_SERVICE) == []
    lowercase = _swap(GOOD_SERVICE, _ARGS + _ENV, (_ARGS + _ENV).replace("ARG", "arg"))
    assert service_build_arg_problems(lowercase) == []
    builder = "FROM python:3.12-slim AS builder\nRUN make wheels\n" + GOOD_SERVICE
    assert service_build_arg_problems(builder) == []
    tabs = _swap(GOOD_SERVICE, _ARGS + _ENV, (_ARGS + _ENV).replace("ARG ", "ARG\t"))
    assert service_build_arg_problems(tabs.replace("ENV ", "ENV \t")) == []
    metadata = _swap(GOOD_SERVICE, "USER appuser\n", "LABEL a=b\nWORKDIR /app\nUSER appuser\n")
    assert service_build_arg_problems(metadata) == []


def test_service_scan_does_not_ask_where_the_install_runs():
    """With no layer allowed after the ARG lines, a slim final stage whose
    dependencies were installed in a builder, or with `uv sync`, is as late as any."""
    slim = (
        "FROM python:3.12-slim AS builder\nRUN uv pip install --system -r requirements.txt\n"
        "FROM python:3.12-slim\nCOPY --from=builder /usr/local /usr/local\n" + _ARGS + _ENV
    )
    assert service_build_arg_problems(slim) == []
    synced = _swap(GOOD_SERVICE, _INSTALL, "RUN uv sync --frozen\n")
    synced = _swap(synced, "RUN uv pip install --system --no-deps -e .\n", "")
    assert service_build_arg_problems(synced) == []


SERVICE_FALSE_PASSES = {
    "ENV ahead of both ARG lines": _swap(
        _swap(GOOD_SERVICE, _ENV, ""), "WORKDIR /app\n", "WORKDIR /app\n" + _ENV
    ),
    "ENV between the two ARG lines": _swap(
        GOOD_SERVICE, _ARGS + _ENV, "ARG HERD_BUILD=dev\n" + _ENV + "ARG HERD_BUILD_DATE=\n"
    ),
    "install after the ARG lines, RUN flag spelling": _swap(
        GOOD_SERVICE,
        _ENV,
        _ENV + "RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system -e .\n",
    ),
    "install after the ARG lines, on a continuation line": _swap(
        GOOD_SERVICE, _ENV, _ENV + "RUN set -e; \\\n    uv pip install --system -e .\n"
    ),
    "install after the ARG lines, plain pip": _swap(
        GOOD_SERVICE, _ENV, _ENV + "RUN python -m pip install -e .\n"
    ),
    "install after the ARG lines, uv sync": _swap(GOOD_SERVICE, _ENV, _ENV + "RUN uv sync\n"),
    "source COPY after the ARG lines": _swap(
        GOOD_SERVICE, _ENV, _ENV + "COPY services/x/migrations/ migrations/\n"
    ),
    "ARG lines ahead of the last install": _swap(
        _swap(GOOD_SERVICE, _ARGS, ""),
        "COPY services/x/app/ app/\n",
        _ARGS + "COPY services/x/app/ app/\n",
    ),
    "ARG and ENV in a builder stage only": _swap(
        GOOD_SERVICE,
        "USER appuser\n",
        "FROM python:3.12-slim\nCOPY --from=0 /app /app\nUSER appuser\n",
    ),
    "ARG and ENV only in a comment": _swap(
        GOOD_SERVICE, _ARGS + _ENV, "".join(f"# {line}\n" for line in (_ARGS + _ENV).splitlines())
    ),
    "a later ENV overrides the value": _swap(
        GOOD_SERVICE, "USER appuser\n", "ENV HERD_BUILD=dev\nUSER appuser\n"
    ),
    "a later ENV overrides the value, space form": _swap(
        GOOD_SERVICE, "USER appuser\n", "ENV HERD_BUILD_DATE unknown\nUSER appuser\n"
    ),
    "the ARG is declared twice": _swap(
        GOOD_SERVICE, "WORKDIR /app\n", "WORKDIR /app\nARG HERD_BUILD=dev\n"
    ),
    "ENV carries only the build string": _swap(
        GOOD_SERVICE, _ENV, "ENV HERD_BUILD=${HERD_BUILD}\n"
    ),
    "install after the ARG lines, a tab after RUN": _swap(
        GOOD_SERVICE, _ENV, _ENV + "RUN\tuv pip install --system extra\n"
    ),
    "install after the ARG lines, lowercase keyword": _swap(
        GOOD_SERVICE, _ENV, _ENV + "run uv pip install --system extra\n"
    ),
    "install as the last line, ending in a backslash": _swap(
        GOOD_SERVICE,
        'CMD ["uvicorn", "app.main:app"]\n',
        'CMD ["uvicorn", "app.main:app"]\nRUN uv pip install --system extra \\\n',
    ),
    "the ARG declared again with another default before the ENV": _swap(
        GOOD_SERVICE, _ENV, "ARG HERD_BUILD=release\n" + _ENV
    ),
    "the ARG declared only ahead of the first FROM": _swap(
        _swap(GOOD_SERVICE, _ARGS, ""), "FROM python:3.12-slim\n", _ARGS + "FROM python:3.12-slim\n"
    ),
}


@pytest.mark.parametrize("spelling", sorted(SERVICE_FALSE_PASSES))
def test_service_scan_flags_every_misplaced_build_arg(spelling: str):
    assert service_build_arg_problems(SERVICE_FALSE_PASSES[spelling]), spelling


_FE_ARGS = "ARG VITE_HERD_BUILD=dev\nARG VITE_HERD_BUILD_DATE=\n"
_FE_ENV = "ENV VITE_HERD_BUILD=${VITE_HERD_BUILD} VITE_HERD_BUILD_DATE=${VITE_HERD_BUILD_DATE}\n"
_FE_INSTALL = "RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi\n"
GOOD_FRONTEND = (
    "FROM node:22-alpine AS build\n"
    "WORKDIR /app\n"
    "COPY package.json package-lock.json* ./\n"
    + _FE_INSTALL
    + "COPY . .\n"
    + _FE_ARGS
    + _FE_ENV
    + "RUN npm run build\n"
    + "FROM nginx:1.25-alpine\n"
    + "COPY --from=build /app/dist /usr/share/nginx/html\n"
)


def test_frontend_scan_accepts_a_clean_dockerfile():
    assert frontend_build_arg_problems(GOOD_FRONTEND) == []
    docs = _swap(
        GOOD_FRONTEND, "RUN npm run build\n", "RUN npm run build\nRUN npm run build:docs\n"
    )
    assert frontend_build_arg_problems(docs) == []
    split = (
        "FROM node:22-alpine AS deps\n"
        + _FE_INSTALL
        + "FROM node:22-alpine AS build\nCOPY --from=deps /app/node_modules ./node_modules\n"
        + "COPY . .\n"
        + _FE_ARGS
        + _FE_ENV
        + "RUN npm run build -- --mode production\n"
    )
    assert frontend_build_arg_problems(split) == []


FRONTEND_FALSE_PASSES = {
    "ENV ahead of ARG VITE_HERD_BUILD": _swap(
        GOOD_FRONTEND,
        _FE_ARGS + _FE_ENV,
        "ARG VITE_HERD_BUILD_DATE=\n" + _FE_ENV + "ARG VITE_HERD_BUILD=dev\n",
    ),
    "ARG and ENV in a stage the build does not start from": _swap(
        _swap(GOOD_FRONTEND, "COPY . .\n", ""),
        "RUN npm run build\n",
        "FROM node:22-alpine AS real\nCOPY --from=build /app/node_modules ./node_modules\n"
        "COPY . .\nRUN npm run build\n",
    ),
    "ARG lines ahead of the install": _swap(
        _swap(GOOD_FRONTEND, _FE_ARGS, ""), _FE_INSTALL, _FE_ARGS + _FE_INSTALL
    ),
    "ARG and ENV after the build": _swap(
        _swap(GOOD_FRONTEND, _FE_ARGS + _FE_ENV, ""),
        "RUN npm run build\n",
        "RUN npm run build\n" + _FE_ARGS + _FE_ENV,
    ),
    "a later ENV overrides the value": _swap(
        GOOD_FRONTEND, "RUN npm run build\n", "ENV VITE_HERD_BUILD=dev\nRUN npm run build\n"
    ),
    "the build is commented out": _swap(
        GOOD_FRONTEND, "RUN npm run build\n", "# RUN npm run build\nRUN npx vite build\n"
    ),
    "only a build:docs script is run": _swap(
        GOOD_FRONTEND, "RUN npm run build\n", "RUN npm run build:docs\n"
    ),
    "the build moved above the ARG lines, a tab after RUN": _swap(
        _swap(GOOD_FRONTEND, "RUN npm run build\n", ""),
        "COPY . .\n",
        "COPY . .\nRUN\tnpm run build\n",
    ),
}


@pytest.mark.parametrize("spelling", sorted(FRONTEND_FALSE_PASSES))
def test_frontend_scan_flags_every_misplaced_build_arg(spelling: str):
    assert frontend_build_arg_problems(FRONTEND_FALSE_PASSES[spelling]), spelling
