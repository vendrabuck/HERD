"""Integration tests for the internal package-validation endpoint (issue #28 phase 1).

Drives POST /execution/internal/validate-package through the gateway against
a running stack: the real drivers/mock_hypervisor package must come back
fully valid with a five-method dry-run section, a hand-broken variant must
come back as a red report (200, not an error), and the endpoint must hold
its internal-token gate. No LLM involvement; the validator is exercised
directly, which is exactly how ai-orchestrator's drafting loop will call it
in phase 2.
"""

import base64
import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.asyncio

MOCK_HYPERVISOR_DIR = Path(__file__).parents[2] / "drivers" / "mock_hypervisor"


def _package_b64(replacements: dict[str, str] | None = None) -> str:
    files = {
        p.name: p.read_text() for p in MOCK_HYPERVISOR_DIR.iterdir() if p.is_file()
    }
    if replacements:
        files.update(replacements)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode()


def _internal_token() -> str:
    token = os.getenv("INTERNAL_API_TOKEN")
    if not token:
        pytest.skip("INTERNAL_API_TOKEN not set in the test environment")
    return token


async def _post_validate(base_url: str, token: str, body: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        headers={"X-Internal-Token": token},
        timeout=120.0,
    ) as client:
        return await client.post("/execution/internal/validate-package", json=body)


async def test_mock_hypervisor_package_validates_live(base_url):
    token = _internal_token()
    resp = await _post_validate(base_url, token, {"package_b64": _package_b64()})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["structural"] == {"passed": True, "errors": []}
    assert report["policy"] == {"passed": True, "errors": []}
    assert report["valid"] is True, report["dry_run"]

    methods = report["dry_run"]["methods"]
    assert [m["action"] for m in methods] == [
        "login",
        "create_instance",
        "status",
        "destroy_instance",
        "logout",
    ]
    assert all(m["passed"] for m in methods)
    # The dry-run ran in simulation: create_instance reported an instance_ref
    # without any hypervisor existing anywhere in the test environment.
    create = next(m for m in methods if m["action"] == "create_instance")
    assert create["output"]["instance_ref"]


async def test_broken_package_returns_red_report_live(base_url):
    token = _internal_token()
    source = (MOCK_HYPERVISOR_DIR / "driver.py").read_text()
    broken = source.replace("def create_instance", "def create_instance_typo")
    resp = await _post_validate(
        base_url, token, {"package_b64": _package_b64({"driver.py": broken})}
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["valid"] is False
    assert "Driver class is missing required method: create_instance" in (
        report["structural"]["errors"]
    )
    assert report["dry_run"]["error"] == "not run"


async def test_validate_package_requires_internal_token(base_url):
    _internal_token()  # skip consistently with the other tests when unset
    resp = await _post_validate(base_url, "wrong-token", {"package_b64": _package_b64()})
    assert resp.status_code == 403
