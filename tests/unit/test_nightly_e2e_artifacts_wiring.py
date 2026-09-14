"""Static pins for nightly.yml's e2e failure artifacts.

The 2026-09-14 nightly failed in an e2e test, and the run's failure artifacts held
only the compose logs: the per-test e2e artifact directory that
`tests/e2e/conftest.py` writes (screenshot, page, console, traceback, meta) lived in
the runner's temp dir and was lost with the job. These pins keep the directory
pointed into the workspace and listed in the failure upload.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly.yml"


def _job() -> dict:
    data = yaml.safe_load(NIGHTLY.read_text())
    return data["jobs"]["full-stack"]


def test_nightly_points_the_e2e_artifact_dir_into_the_workspace():
    env = _job().get("env") or {}
    value = env.get("HERD_E2E_ARTIFACT_DIR", "")
    assert "github.workspace" in value and value.endswith("/e2e-artifacts"), value


def test_nightly_failure_upload_includes_the_e2e_artifact_dir():
    uploads = [
        step
        for step in _job()["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
        and step.get("if") == "failure()"
    ]
    assert uploads, "no failure-gated upload step in nightly.yml"
    paths = uploads[0]["with"]["path"]
    assert "e2e-artifacts/**" in paths.split(), paths
