"""One uv and one Python base across the service images and CI (issue #1300).

The twelve service Dockerfiles copy the uv binary from a named stage and install
from `uv export --frozen`, and CI runs `uv lock --check` and the image-versus-lock
check with the uv that `astral-sh/setup-uv` installs. If the images and CI ran
different uv releases, CI would vouch for a lock and an export the images never
produced, and a Dependabot bump of one image alone would split the images.

What is checked, as text and YAML only:

- every `services/*/Dockerfile` has exactly one `FROM` naming
  `ghcr.io/astral-sh/uv`, and all twelve name the same reference (tag and digest);
- the final stage of every service Dockerfile starts from the same `python`
  reference (tag and digest);
- every workflow step under `.github/workflows/` that uses `astral-sh/setup-uv`
  passes `with: version:` equal to the tag of that uv reference.

Limits: a uv installed some other way (pip, a curl script, a Makefile target) is not
seen, nor a workflow outside `.github/workflows/`; whether the version exists is not
judged (`tests/unit/test_image_pins.py` and the registry are). A Dependabot PR that
bumps the uv image fails here until the workflows' `version:` moves with it, which is
the intent.
"""

from __future__ import annotations

import yaml

from tests.unit.test_image_pins import (
    REPO_ROOT,
    _instructions,
    dockerfile_references,
    split_reference,
)

UV_IMAGE = "ghcr.io/astral-sh/uv"
SERVICE_DOCKERFILES = sorted((REPO_ROOT / "services").glob("*/Dockerfile"))
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))


def uv_references(text: str) -> list[str]:
    return [ref for _, ref in dockerfile_references(text) if split_reference(ref)[0] == UV_IMAGE]


def final_base(text: str) -> str:
    froms = [args.split() for _, keyword, args in _instructions(text) if keyword == "FROM"]
    words = [w for w in froms[-1] if not w.startswith("--")]
    return words[0]


def setup_uv_versions(workflow: dict) -> list[tuple[str, object]]:
    """(job/step label, the step's with.version or None) for every setup-uv step."""
    found = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                version = (step.get("with") or {}).get("version")
                found.append((f"{job_name} step {index} ({step.get('name', '')})", version))
    return found


def pin_problems(dockerfiles: dict[str, str], workflows: dict[str, dict]) -> list[str]:
    problems = []
    uv_refs = {}
    bases = {}
    for name, text in dockerfiles.items():
        refs = uv_references(text)
        if len(refs) != 1:
            problems.append(f"{name}: {len(refs)} uv image references, expected 1")
        else:
            uv_refs[name] = refs[0]
        bases[name] = final_base(text)
    if len(set(uv_refs.values())) > 1:
        problems.append(f"the service Dockerfiles name different uv images: {uv_refs}")
    if len(set(bases.values())) > 1:
        problems.append(f"the service Dockerfiles start from different bases: {bases}")
    tags = {split_reference(ref)[1] for ref in uv_refs.values()}
    expected = tags.pop() if len(tags) == 1 else None
    steps = 0
    for name, workflow in workflows.items():
        for label, version in setup_uv_versions(workflow):
            steps += 1
            if str(version) != str(expected) or version is None:
                problems.append(
                    f"{name} {label}: setup-uv version {version!r}, the images use {expected!r}"
                )
    if steps == 0:
        problems.append("no workflow step uses astral-sh/setup-uv")
    return problems


def test_service_images_and_ci_use_one_uv_and_one_python_base():
    assert len(SERVICE_DOCKERFILES) == 12
    dockerfiles = {str(p.relative_to(REPO_ROOT)): p.read_text() for p in SERVICE_DOCKERFILES}
    workflows = {str(p.relative_to(REPO_ROOT)): yaml.safe_load(p.read_text()) for p in WORKFLOWS}
    problems = pin_problems(dockerfiles, workflows)
    assert not problems, "\n".join(problems)


_UV = "ghcr.io/astral-sh/uv:0.13.7@sha256:" + "1" * 64
_PY = "python:3.12.16-slim@sha256:" + "2" * 64


def _dockerfile(uv=_UV, py=_PY):
    return f"FROM {uv} AS uv\nFROM {py}\nCOPY --from=uv /uv /usr/local/bin/uv\n"


def _workflow(version="0.13.7"):
    step = {"uses": "astral-sh/setup-uv@" + "a" * 40}
    if version is not None:
        step["with"] = {"version": version}
    return {"jobs": {"backend": {"steps": [{"uses": "actions/checkout@x"}, step]}}}


def test_detector_passes_matching_pins():
    assert pin_problems({"a": _dockerfile(), "b": _dockerfile()}, {"ci": _workflow()}) == []


def test_detector_refuses_a_split_uv_a_split_base_and_a_drifted_ci():
    other_uv = "ghcr.io/astral-sh/uv:0.13.8@sha256:" + "3" * 64
    split = pin_problems({"a": _dockerfile(), "b": _dockerfile(uv=other_uv)}, {"ci": _workflow()})
    assert any("different uv images" in p for p in split)
    base = pin_problems(
        {"a": _dockerfile(), "b": _dockerfile(py="python:3.13.1-slim")}, {"ci": _workflow()}
    )
    expected_bases = {"a": _PY, "b": "python:3.13.1-slim"}
    assert base == [f"the service Dockerfiles start from different bases: {expected_bases}"]
    drift = pin_problems({"a": _dockerfile()}, {"ci": _workflow("0.12.0")})
    assert drift == ["ci backend step 1 (): setup-uv version '0.12.0', the images use '0.13.7'"]
    missing = pin_problems({"a": _dockerfile()}, {"ci": _workflow(None)})
    assert missing == ["ci backend step 1 (): setup-uv version None, the images use '0.13.7'"]
    assert pin_problems({"a": _dockerfile()}, {"ci": {"jobs": {}}}) == [
        "no workflow step uses astral-sh/setup-uv"
    ]
