"""Dependabot never proposes a major of a runtime or datastore image.

Project policy (`docs/specs/DEVELOPMENT.md`, Dependabot): a major of node, python,
postgres, or nats is never merged on green CI alone, because CI boots a fresh volume
and tests one runtime, so those moves are made by hand. A Dependabot PR for one would
only invite a merge on green, so every ecosystem block that tracks one of those images
must ignore its semver-major updates.

What is checked, as text and YAML only:

- every `docker` and `docker-compose` block of `.github/dependabot.yml`: its
  `directory` and `directories` entries are expanded against the checkout with
  `Path.glob` (so `/services/*` matches one level), and the block tracks the images
  named in the tracked Dockerfiles directly in each such directory (`docker`) or the
  tracked compose files directly in it (`docker-compose`), read with the scanners of
  `tests/unit/test_image_pins.py`;
- for every tracked image whose name, with a leading `docker.io/` and `library/`
  dropped, is in `RUNTIME_IMAGES`, the block has an `ignore` entry whose
  `dependency-name` matches the name as written in the file (`fnmatch`, so a `*`
  wildcard counts) and whose `update-types` lists `version-update:semver-major`, or
  that names neither `update-types` nor `versions` (every update ignored);
- each image in `RUNTIME_IMAGES` is tracked by at least one block, so the check does
  not pass vacuously after a file or a directory entry moves.

Limits:

- the `uv`, `npm`, and `github-actions` ecosystems are not read: they track no image
  (`requires-python`, `engines.node`, `@types/node`, and setup-node's `node-version`
  input are outside this guard);
- Dependabot's own matching is not run: the guard assumes it compares
  `dependency-name` with the image name as written in the `FROM` or `image:` line,
  and it does not read `exclude-paths`, `allow`, or `versions` ranges;
- an image named through a build argument or an interpolated compose `image:` is not
  seen (`tests/unit/test_image_pins.py` refuses those);
- only majors are pinned here; the minor ignores of python and nats are recorded in
  `docs/specs/DEVELOPMENT.md` and not checked.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml

from tests.unit.test_image_pins import (
    REPO_ROOT,
    compose_files,
    compose_references,
    dockerfile_references,
    dockerfiles,
    split_reference,
)

DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
RUNTIME_IMAGES = frozenset({"node", "python", "postgres", "nats"})
MAJOR = "version-update:semver-major"


def runtime_name(image_name: str) -> str:
    """An image name without a Docker Hub prefix (`docker.io/library/node` is `node`)."""
    for prefix in ("docker.io/", "library/"):
        if image_name.startswith(prefix):
            image_name = image_name[len(prefix) :]
    return image_name


def block_directories(block: dict, root: Path) -> list[Path]:
    entries = list(block.get("directories") or [])
    if "directory" in block:
        entries.append(block["directory"])
    found: set[Path] = set()
    for entry in entries:
        pattern = str(entry).strip("/")
        if not pattern:
            found.add(root)
        else:
            found.update(p for p in root.glob(pattern) if p.is_dir())
    return sorted(found)


def tracked_images(block: dict, root: Path = REPO_ROOT) -> set[str]:
    """Image names (as written, without tag or digest) one ecosystem block tracks."""
    directories = set(block_directories(block, root))
    names: set[str] = set()
    if block.get("package-ecosystem") == "docker":
        for path in dockerfiles(root):
            if path.parent in directories:
                names.update(
                    split_reference(r)[0] for _, r in dockerfile_references(path.read_text())
                )
    elif block.get("package-ecosystem") == "docker-compose":
        for path in compose_files(root):
            if path.parent in directories:
                names.update(split_reference(r)[0] for _, r in compose_references(path.read_text()))
    return names


def ignores_majors(block: dict, image_name: str) -> bool:
    for entry in block.get("ignore") or []:
        if not fnmatch.fnmatchcase(image_name, str(entry.get("dependency-name", ""))):
            continue
        types = entry.get("update-types")
        if types is None and "versions" not in entry:
            return True
        if types and MAJOR in types:
            return True
    return False


def missing_major_ignores(config: dict, root: Path = REPO_ROOT) -> tuple[list[str], set[str]]:
    """(problems, runtime names seen) over the docker and docker-compose blocks."""
    problems: list[str] = []
    seen: set[str] = set()
    for block in config.get("updates") or []:
        ecosystem = block.get("package-ecosystem")
        if ecosystem not in ("docker", "docker-compose"):
            continue
        for name in sorted(tracked_images(block, root)):
            if runtime_name(name) not in RUNTIME_IMAGES:
                continue
            seen.add(runtime_name(name))
            if not ignores_majors(block, name):
                problems.append(
                    f"the {ecosystem} block tracks {name!r} but does not ignore its {MAJOR} updates"
                )
    return problems, seen


def test_every_tracked_runtime_image_ignores_semver_major():
    problems, _ = missing_major_ignores(yaml.safe_load(DEPENDABOT.read_text()))
    assert not problems, "\n".join(problems)


def test_every_runtime_image_is_tracked_by_some_block():
    _, seen = missing_major_ignores(yaml.safe_load(DEPENDABOT.read_text()))
    assert seen == RUNTIME_IMAGES, f"not tracked by any block: {sorted(RUNTIME_IMAGES - seen)}"


def test_checker_flags_a_block_without_the_ignore(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "Dockerfile").write_text(
        "FROM docker.io/library/node:22.23.4-alpine AS build\nFROM nginx:1.30.6-alpine\n"
    )
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  db:\n    image: postgres:16.15-alpine\n"
    )
    docker = {"package-ecosystem": "docker", "directories": ["/*"]}
    compose = {
        "package-ecosystem": "docker-compose",
        "directory": "/",
        "ignore": [{"dependency-name": "postgres", "update-types": [MAJOR]}],
    }
    problems, seen = missing_major_ignores({"updates": [docker, compose]}, tmp_path)
    assert seen == {"node", "postgres"}
    assert problems == [
        f"the docker block tracks 'docker.io/library/node' but does not ignore its {MAJOR} updates"
    ]
    # A name that does not match as written, or a minor-only ignore, still fails.
    docker["ignore"] = [
        {"dependency-name": "node", "update-types": [MAJOR]},
        {
            "dependency-name": "docker.io/library/node",
            "update-types": ["version-update:semver-minor"],
        },
    ]
    assert len(missing_major_ignores({"updates": [docker]}, tmp_path)[0]) == 1
    # The written name, a wildcard, or an ignore of every update passes.
    for entry in (
        {"dependency-name": "docker.io/library/node", "update-types": [MAJOR]},
        {"dependency-name": "*node", "update-types": [MAJOR]},
        {"dependency-name": "docker.io/library/node"},
    ):
        docker["ignore"] = [entry]
        assert missing_major_ignores({"updates": [docker]}, tmp_path)[0] == []
