"""Every image HERD builds from or runs names an exact version (issue #1300).

`traefik:latest` and `ghcr.io/astral-sh/uv:latest` let a new upstream release change
every fresh install and every CI run on the same day with no change in this
repository, and floating tags such as `python:3.12-slim` or `nginx:1.25-alpine` let
an old commit rebuild into something its gate never tested. This guard reads every
Dockerfile and compose file in the checkout and fails on an image reference that is
not pinned.

No stack, no Docker, no registry: the files are read as text and YAML only.

What is read:

- Dockerfiles: every file named `Dockerfile` (or ending in `.Dockerfile`) under the
  repository root, skipping directories whose name starts with `.` and every
  `node_modules`; in each, the image of every `FROM` and every `COPY --from=` /
  `ADD --from=` (continuation lines joined, comments dropped). A `FROM` or `--from`
  that names an earlier stage of the same file (by its `AS` name or its index), and
  `FROM scratch`, are not images and pass.
- Compose files: every `docker-compose*.yml` or `compose*.yml` found the same way;
  in each, the `image:` of every service under `services:`. A service that also has
  a `build:` key names an image this repository builds itself, and passes.

The rule for one reference `[registry/]name[:tag][@sha256:<64 hex>]`:

- a digest, when present, is exactly `sha256:` and 64 lowercase hex characters;
- with a digest and no tag, the reference passes (a digest is immutable);
- otherwise the tag must be present, must not be `latest`, and must start with a
  version of at least three numeric parts (`v3.6.10`, `3.12.16-slim`,
  `4.43.0-20260404`), so a major-only or major.minor tag (`22-alpine`, `3.12-slim`,
  `v3`) fails. The one exception is `_TWO_PART_VERSION_IMAGES`: an image whose
  upstream numbers each release with two parts (PostgreSQL since 10: `16.15` is one
  release) may use a two-part version.

Limits. The files are read as text, not built or rendered:

- not seen: an image named only through a build argument or a variable (`FROM
  ${BASE}` and an interpolated compose `image:` are refused, not resolved), `RUN
  --mount=from=<image>`, an image a script or the Makefile pulls or runs by name, a
  compose file under a hidden directory or `node_modules`, and a Dockerfile under any
  other name;
- not judged: whether the tag and the digest name the same image, whether either
  exists in its registry, and whether the version is the newest. Dependabot's
  `docker` and `docker-compose` ecosystems (`.github/dependabot.yml`) propose the
  bumps.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Images whose upstream release numbers have two parts, so `<major>.<minor>` is one
# exact release rather than a floating line. Keyed by the image name without a
# registry; every entry must be used by a reference in the checkout.
_TWO_PART_VERSION_IMAGES = {
    "postgres": "PostgreSQL numbers releases MAJOR.MINOR since version 10",
}

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_THREE_PART = re.compile(r"v?\d+\.\d+\.\d+(?![0-9])")
_TWO_PART = re.compile(r"v?\d+\.\d+(?![0-9.])")


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "node_modules")
        for name in sorted(filenames):
            yield Path(dirpath) / name


def dockerfiles(root: Path = REPO_ROOT) -> list[Path]:
    return [p for p in _walk(root) if p.name == "Dockerfile" or p.name.endswith(".Dockerfile")]


def compose_files(root: Path = REPO_ROOT) -> list[Path]:
    return [p for p in _walk(root) if re.fullmatch(r"(docker-)?compose[\w.-]*\.ya?ml", p.name)]


def split_reference(ref: str) -> tuple[str, str | None, str | None]:
    """(name, tag, digest) of an image reference; tag and digest None when absent."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    last_slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > last_slash:
        ref, tag = ref[:colon], ref[colon + 1 :]
    return ref, tag, digest


def reference_problem(ref: str) -> str | None:
    """Why one image reference is not pinned, or None when it is."""
    if "$" in ref:
        return f"{ref!r} is interpolated, so the guard cannot read the version it names"
    name, tag, digest = split_reference(ref)
    if digest is not None and not _DIGEST.fullmatch(digest):
        return f"{ref!r} has a malformed digest (expected sha256: and 64 lowercase hex)"
    if tag is None:
        if digest is not None:
            return None
        return f"{ref!r} has no tag, which means latest"
    if tag == "latest":
        return f"{ref!r} uses the floating latest tag"
    if _THREE_PART.match(tag):
        return None
    short_name = name.rsplit("/", 1)[-1]
    if short_name in _TWO_PART_VERSION_IMAGES and _TWO_PART.match(tag):
        return None
    return (
        f"{ref!r}: tag {tag!r} is not an exact version (it must start with "
        "MAJOR.MINOR.PATCH), so it floats to whatever upstream publishes next"
    )


def _instructions(text: str) -> list[tuple[int, str, str]]:
    """(first line number, KEYWORD, arguments) per instruction; comments dropped."""
    out: list[tuple[int, str, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if pending and stripped.startswith("#"):
            continue
        if not pending:
            start = number
        if stripped.endswith("\\"):
            pending.append(stripped[:-1])
            continue
        pending.append(stripped)
        joined = " ".join(" ".join(pending).split())
        pending = []
        keyword, _, arguments = joined.partition(" ")
        out.append((start, keyword.upper(), arguments))
    return out


def dockerfile_references(text: str) -> list[tuple[int, str]]:
    """(line, image) for every FROM and COPY/ADD --from= that names an image."""
    stages: set[str] = set()
    count = 0
    found: list[tuple[int, str]] = []
    for line, keyword, arguments in _instructions(text):
        words = arguments.split()
        if keyword == "FROM":
            words = [w for w in words if not w.startswith("--")]
            image = words[0] if words else ""
            if image.lower() != "scratch" and image not in stages:
                found.append((line, image))
            stages.add(str(count))
            if len(words) >= 3 and words[1].lower() == "as":
                stages.add(words[2])
            count += 1
        elif keyword in ("COPY", "ADD"):
            for word in words:
                if word.startswith("--from="):
                    source = word.split("=", 1)[1]
                    if source not in stages:
                        found.append((line, source))
    return found


def compose_references(text: str) -> list[tuple[str, str]]:
    """(service, image) for every compose service that runs an image it does not build."""
    data = yaml.safe_load(text) or {}
    found: list[tuple[str, str]] = []
    for service, spec in (data.get("services") or {}).items():
        if not isinstance(spec, dict) or "image" not in spec or "build" in spec:
            continue
        found.append((service, str(spec["image"])))
    return found


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_discovery_finds_the_known_build_and_compose_files():
    found = {_rel(p) for p in dockerfiles()}
    services = {
        f"services/{d.name}/Dockerfile"
        for d in (REPO_ROOT / "services").iterdir()
        if (d / "Dockerfile").is_file()
    }
    expected = services | {"frontend/Dockerfile", "infra/nos-test/frr/Dockerfile"}
    assert len(services) == 12
    assert expected <= found, f"not discovered: {sorted(expected - found)}"
    composes = {_rel(p) for p in compose_files()}
    assert {
        "docker-compose.yml",
        "docker-compose.override.yml",
        "infra/ldap-test/docker-compose.yml",
        "infra/nos-test/docker-compose.yml",
    } <= composes


def test_every_dockerfile_image_is_pinned():
    problems = [
        f"{_rel(path)}:{line}: {problem}"
        for path in dockerfiles()
        for line, ref in dockerfile_references(path.read_text())
        if (problem := reference_problem(ref))
    ]
    assert not problems, "\n".join(problems)


def test_every_compose_image_is_pinned():
    problems = [
        f"{_rel(path)} service {service}: {problem}"
        for path in compose_files()
        for service, ref in compose_references(path.read_text())
        if (problem := reference_problem(ref))
    ]
    assert not problems, "\n".join(problems)


def test_two_part_version_exceptions_are_all_used():
    names = {
        split_reference(ref)[0].rsplit("/", 1)[-1]
        for path in compose_files()
        for _, ref in compose_references(path.read_text())
    } | {
        split_reference(ref)[0].rsplit("/", 1)[-1]
        for path in dockerfiles()
        for _, ref in dockerfile_references(path.read_text())
    }
    stale = set(_TWO_PART_VERSION_IMAGES) - names
    assert not stale, f"_TWO_PART_VERSION_IMAGES lists images no file uses: {sorted(stale)}"


_HEX = "0" * 64


@pytest.mark.parametrize(
    "ref",
    [
        "traefik:v3.6.10",
        f"traefik:v3.6.10@sha256:{_HEX}",
        f"python:3.12.16-slim@sha256:{_HEX}",
        "selenium/standalone-chrome:4.43.0-20260404",
        "ghcr.io/nokia/srlinux:26.7.2-519",
        f"frrouting/frr@sha256:{_HEX}",
        "osixia/openldap:1.5.0",
        "localhost:5000/team/tool:1.2.3",
        "postgres:16.15-alpine",
    ],
)
def test_pinned_references_pass(ref):
    assert reference_problem(ref) is None


@pytest.mark.parametrize(
    "ref",
    [
        "traefik:latest",
        "traefik",
        "traefik:v3",
        "traefik:v3.6",
        "ghcr.io/astral-sh/uv:latest",
        "python:3.12-slim",
        f"python:3.12-slim@sha256:{_HEX}",
        "node:22-alpine",
        "nginx:1.25-alpine",
        "nats:2.10-alpine",
        "postgres:16-alpine",
        "localhost:5000/team/tool",
        "frrouting/frr@sha256:abc",
        "${BASE_IMAGE}",
        "python:3.12.16-slim@sha256:" + "A" * 64,
    ],
)
def test_floating_references_fail(ref):
    assert reference_problem(ref) is not None


def test_dockerfile_scan_skips_local_stages_and_reads_copy_from_images():
    text = (
        "# a comment naming FROM python:latest\n"
        f"FROM ghcr.io/astral-sh/uv:0.9.3@sha256:{_HEX} AS uv\n"
        "FROM --platform=$BUILDPLATFORM node:22.23.4-alpine AS build\n"
        "RUN echo hi\n"
        "FROM build AS test\n"
        "FROM scratch\n"
        "COPY --from=uv /uv /usr/local/bin/uv\n"
        "COPY --from=0 /x /x\n"
        "COPY --chown=1:1 \\\n"
        "  --from=ghcr.io/astral-sh/uv:latest /uv /uv\n"
        "ADD --from=busybox /bin/sh /sh\n"
    )
    refs = dockerfile_references(text)
    assert refs == [
        (2, f"ghcr.io/astral-sh/uv:0.9.3@sha256:{_HEX}"),
        (3, "node:22.23.4-alpine"),
        (9, "ghcr.io/astral-sh/uv:latest"),
        (11, "busybox"),
    ]
    assert [reference_problem(r) is None for _, r in refs] == [True, True, False, False]


def test_compose_scan_exempts_built_images_and_reads_the_rest():
    text = (
        "services:\n"
        "  built:\n"
        "    build: .\n"
        "    image: herd-built-here\n"
        "  gateway:\n"
        "    image: traefik:latest\n"
        "  db:\n"
        "    image: postgres:16.15-alpine\n"
        "  nothing:\n"
        "    command: [x]\n"
    )
    assert compose_references(text) == [
        ("gateway", "traefik:latest"),
        ("db", "postgres:16.15-alpine"),
    ]
