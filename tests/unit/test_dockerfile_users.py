"""Static pin: every service image runs as a non-root user (issue #1326).

Before #1326 the config service image had no `USER` line, so the gateway-routed config
service ran as root; the other eleven service images switch to `appuser`. This guard
reads every `services/*/Dockerfile` and the restarter's `infra/restarter/Dockerfile`
and fails when the user the final stage runs as is root.

The user is the argument of the last `USER` instruction of the final stage (the stage
the image ships; a `USER` in an earlier build stage does not carry over). Root is: no
`USER` in the final stage, a user part (before any `:`) of `root` or `0`, or a user
spelled through a variable (`${U}`, refused because it cannot be judged as text).
Instructions are read with `dockerfile_stages` from tests/unit/test_build_args_wiring.py
(continuations joined, comments dropped, a new stage at every FROM).

Exemptions are by path, each with its reason, in ROOT_EXEMPT. An exempt image must
still run as root, so an exemption that no longer applies fails until it is removed.

Limits: the user is judged from the Dockerfile text, not a built image, so a base image
whose default user is not root, a compose `user:` key, and an ENTRYPOINT that drops
privileges are not seen. The frontend image (nginx, `frontend/Dockerfile`) and the lab
images under `infra/` other than the restarter are outside the scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_build_args_wiring import dockerfile_stages

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_DOCKERFILES = sorted((REPO_ROOT / "services").glob("*/Dockerfile"))
RESTARTER_DOCKERFILE = REPO_ROOT / "infra" / "restarter" / "Dockerfile"
SCANNED = SERVICE_DOCKERFILES + [RESTARTER_DOCKERFILE]

ROOT_EXEMPT = {
    "infra/restarter/Dockerfile": (
        "the restarter opens the host Docker socket, owned by root with mode 0660; uid 0 "
        "opens it with no capability, and a non-root user in the docker group would hold "
        "the same root-equivalent API while adding a host group id knob (ADR 0017). "
        "Compose runs it read-only, with every capability dropped and no-new-privileges."
    ),
}


def runs_as_root(text: str) -> bool:
    stages = dockerfile_stages(text)
    if not stages:
        return True
    users = [args for _, keyword, args in stages[-1] if keyword == "USER"]
    if not users:
        return True
    user = users[-1].split(":", 1)[0].strip()
    return user in ("root", "0", "") or "$" in user


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_every_service_dockerfile_is_scanned():
    assert len(SERVICE_DOCKERFILES) == 12, [_relative(p) for p in SERVICE_DOCKERFILES]
    assert RESTARTER_DOCKERFILE.is_file()


@pytest.mark.parametrize("path", SCANNED, ids=_relative)
def test_the_image_runs_as_a_non_root_user_unless_exempt(path):
    root = runs_as_root(path.read_text())
    if _relative(path) in ROOT_EXEMPT:
        assert root, f"{_relative(path)} no longer runs as root; remove its ROOT_EXEMPT entry"
    else:
        assert not root, f"{_relative(path)} runs as root: add a non-root USER to its final stage"


def test_every_exemption_names_a_scanned_file():
    assert set(ROOT_EXEMPT) <= {_relative(path) for path in SCANNED}


# -- Detector: the reading of one Dockerfile, both directions --


@pytest.mark.parametrize(
    "text",
    [
        "FROM python:3\nRUN true\n",
        "FROM python:3\nUSER root\n",
        "FROM python:3\nUSER 0\n",
        "FROM python:3\nUSER 0:1000\n",
        "FROM python:3\nUSER root:appuser\n",
        "FROM python:3\nUSER appuser\nRUN true\nUSER root\n",
        "FROM python:3 AS build\nUSER appuser\nFROM python:3\nCOPY --from=build /a /a\n",
        "FROM python:3\nUSER ${APP_USER}\n",
        "FROM python:3\n# USER appuser\n",
        "",
    ],
    ids=[
        "no-user",
        "root",
        "uid-0",
        "uid-0-with-group",
        "root-with-group",
        "back-to-root",
        "user-in-build-stage-only",
        "variable",
        "commented-out",
        "empty",
    ],
)
def test_reads_root(text):
    assert runs_as_root(text)


@pytest.mark.parametrize(
    "text",
    [
        "FROM python:3\nUSER appuser\n",
        "FROM python:3\nUSER 1000:1000\n",
        'FROM python:3\nUSER appuser:appuser\nCMD ["x"]\n',
        "FROM python:3 AS build\nUSER root\nFROM python:3\nUSER appuser\n",
        "FROM python:3\nuser appuser\n",
        "FROM python:3\nUSER root\nRUN chown x\nUSER \\\n  appuser\n",
    ],
    ids=["name", "uid-gid", "name-group", "root-in-build-stage", "lower-case", "continued"],
)
def test_reads_non_root(text):
    assert not runs_as_root(text)
