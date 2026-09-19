"""Static pin for the build-identifier plumbing (issue #846): HERD_BUILD and
HERD_BUILD_DATE, computed on the host by the Makefile, must reach every image the
stack builds as Docker build args, and every Dockerfile must declare them as late as
possible so a changed build string invalidates only a metadata layer, never the slow
dependency-install layers above it.

No stack, no Docker: this only parses docker-compose.yml and every checked-in
Dockerfile as text, in the same style as tests/unit/test_compose_ai_env_wiring.py and
tests/unit/test_nos_lab_ci_wiring.py.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def _dockerfile_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _last_index(lines: list[str], predicate) -> int:
    for index in range(len(lines) - 1, -1, -1):
        if predicate(lines[index]):
            return index
    return -1


def _first_index(lines: list[str], predicate) -> int:
    for index, line in enumerate(lines):
        if predicate(line):
            return index
    return -1


_ENV_LINE_RE = re.compile(
    r"^ENV\s+HERD_BUILD=\$\{HERD_BUILD\}\s+HERD_BUILD_DATE=\$\{HERD_BUILD_DATE\}\s*$"
)


def test_every_service_dockerfile_declares_the_build_args_and_env():
    assert len(SERVICE_DOCKERFILES) == 12, (
        f"expected 12 services/*/Dockerfile files, found {len(SERVICE_DOCKERFILES)}: "
        f"{[str(p) for p in SERVICE_DOCKERFILES]}"
    )

    problems = []
    for dockerfile in SERVICE_DOCKERFILES:
        lines = _dockerfile_lines(dockerfile)
        rel = dockerfile.relative_to(REPO_ROOT)

        arg_build_idx = _first_index(lines, lambda line: line.strip() == "ARG HERD_BUILD=dev")
        arg_date_idx = _first_index(lines, lambda line: line.strip() == "ARG HERD_BUILD_DATE=")
        env_idx = _first_index(lines, lambda line: _ENV_LINE_RE.match(line.strip()) is not None)

        if arg_build_idx == -1:
            problems.append(f"{rel}: missing 'ARG HERD_BUILD=dev'")
            continue
        if arg_date_idx == -1:
            problems.append(f"{rel}: missing 'ARG HERD_BUILD_DATE='")
            continue
        if env_idx == -1:
            problems.append(
                f"{rel}: missing an ENV line carrying both HERD_BUILD and HERD_BUILD_DATE"
            )
            continue

        # Cache-placement rule (issue #846): the ARG lines must come AFTER the last
        # `RUN uv pip install` line, so a changed build string invalidates only this
        # metadata layer, never the dependency-install layers above it.
        last_install_idx = _last_index(
            lines, lambda line: "RUN uv pip install" in line or "&& uv pip install" in line
        )
        assert last_install_idx != -1, f"{rel}: no 'uv pip install' line found to anchor against"
        if arg_build_idx <= last_install_idx or arg_date_idx <= last_install_idx:
            problems.append(
                f"{rel}: the HERD_BUILD ARG lines (lines {arg_build_idx + 1}, "
                f"{arg_date_idx + 1}) must come after the last 'uv pip install' line "
                f"(line {last_install_idx + 1}), or a build-string change invalidates "
                "the slow dependency layers"
            )

    assert not problems, "\n".join(problems)


def test_frontend_dockerfile_declares_vite_build_args_before_npm_run_build():
    lines = _dockerfile_lines(FRONTEND_DOCKERFILE)

    arg_build_idx = _first_index(lines, lambda line: line.strip() == "ARG VITE_HERD_BUILD=dev")
    arg_date_idx = _first_index(lines, lambda line: line.strip() == "ARG VITE_HERD_BUILD_DATE=")
    build_step_idx = _first_index(lines, lambda line: line.strip() == "RUN npm run build")
    npm_ci_idx = _last_index(lines, lambda line: "npm ci" in line or "npm install" in line)

    assert arg_build_idx != -1, "frontend/Dockerfile: missing 'ARG VITE_HERD_BUILD=dev'"
    assert arg_date_idx != -1, "frontend/Dockerfile: missing 'ARG VITE_HERD_BUILD_DATE='"
    assert build_step_idx != -1, "frontend/Dockerfile: missing 'RUN npm run build'"
    assert npm_ci_idx != -1, "frontend/Dockerfile: missing the npm ci/install layer"

    assert npm_ci_idx < arg_build_idx < build_step_idx, (
        "VITE_HERD_BUILD must be declared after the npm ci/install layer (so it stays "
        f"cached) and before 'RUN npm run build' (so Vite bakes it in); got npm_ci="
        f"{npm_ci_idx + 1}, ARG={arg_build_idx + 1}, npm run build={build_step_idx + 1}"
    )
    assert npm_ci_idx < arg_date_idx < build_step_idx, (
        "VITE_HERD_BUILD_DATE must be declared after the npm ci/install layer and "
        f"before 'RUN npm run build'; got npm_ci={npm_ci_idx + 1}, ARG="
        f"{arg_date_idx + 1}, npm run build={build_step_idx + 1}"
    )

    env_idx = _first_index(
        lines,
        lambda line: (
            line.strip()
            == "ENV VITE_HERD_BUILD=${VITE_HERD_BUILD} VITE_HERD_BUILD_DATE=${VITE_HERD_BUILD_DATE}"
        ),
    )
    assert env_idx != -1, (
        "frontend/Dockerfile: missing the ENV line that carries both "
        "VITE_HERD_BUILD and VITE_HERD_BUILD_DATE into the build environment"
    )
    assert arg_date_idx < env_idx < build_step_idx
