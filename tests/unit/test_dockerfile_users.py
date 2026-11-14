"""Static pin: every service image runs as a non-root user (issue #1326).

Before #1326 the config service image had no `USER` line, so the gateway-routed config
service ran as root; the other eleven service images switch to `appuser`. This guard
reads every `services/*/Dockerfile` and the restarter's `infra/restarter/Dockerfile`
and fails when the user the final stage runs as is root.

The user of a stage is the argument of its last `USER` instruction. A stage with no
`USER` inherits the user of the stage its `FROM` names when that is an earlier stage of
the same file (by its `AS` name, matched without regard to case, or by its index),
followed through every such link; a stage built `FROM` an image starts as root. The
image runs as the user of its final stage. That user is root when it is `root`, a
number equal to zero however written (`0`, `00`, `0000`), empty, or spelled through a
variable (`${U}`, refused because it cannot be judged as text); only the part before
any `:` is judged. Instructions are read with `dockerfile_stages` from
tests/unit/test_build_args_wiring.py (continuations joined, comments dropped, a new
stage at every FROM).

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


def _stage_ref(from_args: str) -> str:
    """The image or stage name a FROM names, flags and `AS <name>` dropped."""
    words = [w for w in from_args.split() if not w.startswith("--")]
    return words[0] if words else ""


def _stage_name(from_args: str) -> str | None:
    words = [w for w in from_args.split() if not w.startswith("--")]
    if len(words) >= 3 and words[1].lower() == "as":
        return words[2].lower()
    return None


def final_user(text: str) -> str | None:
    """The user the final stage runs as, following FROM <earlier stage> links; None is root."""
    stages = dockerfile_stages(text)
    names: dict[str, int] = {}
    users: list[str | None] = []
    for index, stage in enumerate(stages):
        from_args = stage[0][2] if stage and stage[0][1] == "FROM" else ""
        declared = [args for _, keyword, args in stage if keyword == "USER"]
        if declared:
            user = declared[-1]
        else:
            ref = _stage_ref(from_args).lower()
            parent = names.get(ref, int(ref) if ref.isdigit() and int(ref) < index else None)
            user = users[parent] if parent is not None else None
        users.append(user)
        name = _stage_name(from_args)
        if name is not None:
            names[name] = index
    return users[-1] if users else None


def runs_as_root(text: str) -> bool:
    user = final_user(text)
    if user is None:
        return True
    name = user.split(":", 1)[0].strip()
    return name in ("root", "") or (name.isdigit() and int(name) == 0) or "$" in name


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
        "FROM python:3\nUSER 00\n",
        "FROM python:3\nUSER 0000:0\n",
        "FROM python:3 AS base\nUSER appuser\nFROM base AS next\nUSER root\nFROM next\n",
        "FROM python:3 AS base\nFROM base\nRUN true\n",
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
        "double-zero",
        "zero-padded-with-group",
        "inherits-root-through-a-chain",
        "inherits-root-from-an-image-stage",
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
        'FROM python:3 AS base\nUSER appuser\nFROM base\nCMD ["x"]\n',
        "FROM python:3 AS Base\nUSER 1000\nFROM base AS mid\nRUN true\nFROM mid\n",
        "FROM python:3\nUSER appuser\nFROM 0\n",
        "FROM python:3\nUSER 1001\n",
    ],
    ids=[
        "name",
        "uid-gid",
        "name-group",
        "root-in-build-stage",
        "lower-case",
        "continued",
        "inherits-from-a-named-stage",
        "inherits-through-a-chain",
        "inherits-by-index",
        "uid-1001",
    ],
)
def test_reads_non_root(text):
    assert not runs_as_root(text)
