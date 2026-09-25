"""Static pins for the frontend npm-version gate (issue #885).

No npm, no Docker, no stack: this only reads frontend/package.json,
frontend/.npmrc, .github/workflows/ci.yml, and frontend/Dockerfile as text/
JSON/YAML, the same style as test_compose_settings_wiring.py and
test_nos_lab_ci_wiring.py.

Background: npm only started writing a `libc` array into optional-platform
lockfile entries (npm/cli PR #9025, 2026-02-25) in npm 11.11.0. Dependabot's
npm_and_yarn updater runs npm 11.17.0, so every Dependabot PR re-adds those
arrays; an older local npm (11.6.0 on the machine that filed this issue)
silently strips them again on a plain `npm install`, producing an 18-line
package-lock.json diff with no package.json change. The fix pins a floor:
`frontend/package.json` declares `engines.npm >= 11.11.0` and
`frontend/.npmrc` sets `engine-strict=true` so an incompatible npm refuses
instead of silently drifting the lockfile. ci.yml installs a compliant npm
before `npm ci` and, at the end of the frontend job, reruns `npm install`
and fails on any resulting `package-lock.json` diff (the frontend twin of
the backend job's `uv lock --check`).

The frontend Docker image is deliberately exempt: `frontend/Dockerfile`
copies only `package.json`/`package-lock.json` into the build stage before
running `npm ci`/`npm install`, so `.npmrc` (and therefore engine-strict) is
not present in the image at install time regardless of what npm ships with
the `node:22-alpine` base image. This test pins that COPY ordering instead
of requiring an in-image npm pin, since the two are equally correct fixes
and this repo chose the cheaper one (see the Dockerfile's own comment).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"
PACKAGE_JSON_PATH = FRONTEND_DIR / "package.json"
NPMRC_PATH = FRONTEND_DIR / ".npmrc"
DOCKERFILE_PATH = FRONTEND_DIR / "Dockerfile"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The floor below which npm silently strips the lockfile's `libc` array
# (npm/cli PR #9025). A future bump of this constant, together with a lower
# ci.yml pin, is exactly the regression this test suite must catch.
MIN_ENGINE_NPM_VERSION = (11, 11, 0)


def _parse_version(raw: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        raise AssertionError(f"could not parse a semver-like version out of {raw!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _load_package_json() -> dict:
    return json.loads(PACKAGE_JSON_PATH.read_text())


def _load_ci_workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW_PATH.read_text())


def _frontend_job_steps() -> list[dict]:
    data = _load_ci_workflow()
    return data["jobs"]["frontend"]["steps"]


def test_package_json_declares_engines_npm_floor():
    engines = _load_package_json().get("engines", {})
    assert "npm" in engines, "frontend/package.json must declare engines.npm (issue #885)"
    floor = _parse_version(engines["npm"])
    assert floor >= MIN_ENGINE_NPM_VERSION, (
        f"engines.npm ({engines['npm']}) must require at least "
        f"{'.'.join(map(str, MIN_ENGINE_NPM_VERSION))}, the first npm release that writes "
        "the lockfile's optional-platform `libc` array (npm/cli PR #9025); anything lower "
        "lets a compliant-looking npm still strip it"
    )
    assert "node" in engines, "frontend/package.json must also declare engines.node"


def test_npmrc_sets_engine_strict():
    assert NPMRC_PATH.exists(), "frontend/.npmrc must exist and set engine-strict=true"
    lines = [
        line.strip()
        for line in NPMRC_PATH.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "engine-strict=true" in lines, (
        "frontend/.npmrc must set engine-strict=true, or an incompatible npm only warns "
        "instead of refusing, silently reproducing the original drift"
    )


def test_ci_installs_a_compliant_npm_before_npm_ci():
    steps = _frontend_job_steps()
    install_versions = []
    for step in steps:
        run = str(step.get("run", ""))
        match = re.search(r"npm install -g npm@([\d.]+)", run)
        if match:
            install_versions.append(match.group(1))

    assert install_versions, (
        "ci.yml's frontend job must run `npm install -g npm@<version>` before `npm ci`, "
        "since setup-node's bundled npm is not guaranteed to satisfy package.json's "
        "engines.npm floor and frontend/.npmrc's engine-strict=true would then refuse "
        "every step in the job (issue #885)"
    )
    for version in install_versions:
        assert _parse_version(version) >= MIN_ENGINE_NPM_VERSION, (
            f"ci.yml pins npm@{version}, which is below the engines.npm floor "
            f"{'.'.join(map(str, MIN_ENGINE_NPM_VERSION))} declared in frontend/package.json; "
            "every step in the frontend job (npm ci, lint, test, build, the lockfile gate) "
            "would then refuse under engine-strict"
        )

    ci_step_index = None
    install_step_index = None
    for index, step in enumerate(steps):
        run = str(step.get("run", ""))
        if re.search(r"npm install -g npm@", run) and install_step_index is None:
            install_step_index = index
        if re.search(r"\bnpm ci\b", run) and ci_step_index is None:
            ci_step_index = index
    assert install_step_index is not None and ci_step_index is not None
    assert install_step_index < ci_step_index, (
        "the npm@<version> install step must come before `npm ci`, or `npm ci` runs "
        "under whatever npm setup-node's Node install bundled"
    )


def test_ci_gates_on_a_stable_package_lock():
    steps = _frontend_job_steps()
    gate_steps = [
        step
        for step in steps
        if "npm install" in str(step.get("run", ""))
        and "git diff --exit-code" in str(step.get("run", ""))
        and "package-lock.json" in str(step.get("run", ""))
    ]
    assert gate_steps, (
        "ci.yml's frontend job must have a step that reruns `npm install` and fails on "
        "`git diff --exit-code -- package-lock.json`, the frontend twin of the backend "
        "job's `uv lock --check` (issue #885); without it a future lockfile-writing "
        "change in some npm release could drift the committed lockfile again with "
        "nothing in CI to catch it"
    )

    # The gate must run after the real install (`npm ci`) and build steps, so its own
    # `npm install` (which, unlike `npm ci`, is free to rewrite the lockfile) cannot
    # affect the dependency tree the build and test steps actually exercised.
    steps_order = [str(step.get("run", "")) for step in steps]
    gate_index = steps.index(gate_steps[0])
    build_indices = [i for i, run in enumerate(steps_order) if re.search(r"\bnpm run build\b", run)]
    assert build_indices, "expected a `npm run build` step in the frontend job"
    assert gate_index > build_indices[-1], (
        "the package-lock.json gate step must run after the build step, not before, so "
        "its `npm install` cannot change what npm ci/build/test actually installed"
    )


def test_dockerfile_keeps_npmrc_out_of_the_install_layer():
    """The image build is exempt from engine-strict only because .npmrc is not
    copied into the build stage before npm ci/install runs. If that ever
    changes, the image needs its own npm pin (see the Dockerfile's comment)."""
    text = DOCKERFILE_PATH.read_text()
    lines = text.splitlines()

    install_line_index = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().startswith("RUN") and re.search(r"npm ci|npm install", line)
        ),
        None,
    )
    assert install_line_index is not None, "expected an npm ci/install RUN line in the Dockerfile"

    copy_lines_before_install = [
        line for line in lines[:install_line_index] if line.strip().startswith("COPY")
    ]
    assert copy_lines_before_install, "expected at least one COPY before the install RUN line"

    for line in copy_lines_before_install:
        assert ".npmrc" not in line and line.strip() != "COPY . .", (
            f"a COPY before the install step now brings .npmrc into the build stage "
            f"({line!r}); engine-strict would then apply to node:22-alpine's bundled "
            "npm, which is not guaranteed to satisfy package.json's engines.npm floor. "
            "Either keep .npmrc out of the pre-install COPY set, or add an explicit "
            "`RUN npm install -g npm@<compliant version>` before the install step"
        )
