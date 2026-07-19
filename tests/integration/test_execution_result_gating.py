"""Live coverage for issue #370: run_driver_action gates on the driver RESULT.

The sandbox transport flag only says the driver subprocess did not raise. A
driver method that RETURNS {"success": False, ...} (the mock drivers'
HERD_mock_fail_actions convention) must land the run at FAILED with the
driver's error and full output preserved; before the fix these runs were
recorded SUCCESS with the failure buried in output.

Also pins the conservative posture: the mock driver's status() deliberately
returns bare data with no "success" key even under the fail knob ("status must
always answer"), so a status run stays SUCCESS; only an explicit
success: False fails a run.

Self-seeds its own driver/template/device (session fixtures), per the
integration-test seeding convention.
"""

import io
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest

_MOCK_L2_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l2"


def _mock_l2_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L2_DIR / name, arcname=name)
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


@pytest.fixture(scope="session")
async def gating_driver(base_url, admin_token):
    """Upload the mock L2 driver once per session for the result-gating tests."""
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l2.tar.gz", _mock_l2_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l2-gating-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 2 Switch",
            "description": "integration mock L2 driver (issue #370 result gating)",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def gating_template(base_url, admin_token, gating_driver):
    """A template declaring the mock_fail_actions injection field."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l2-gating-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": gating_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL2SwitchGating",
            "sections": [
                {
                    "name": "General",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string"},
                        {
                            "key": "mock_fail_actions",
                            "label": "Mock fail actions",
                            "type": "string",
                        },
                    ],
                }
            ],
        }
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def gating_device(base_url, admin_token, gating_template):
    """A switch whose driver returns success: False for login, and bare data
    (no success key) for status, exercising both sides of the gate."""
    async with _admin_session_client(base_url, admin_token) as client:
        resp = await client.post(
            "/inventory/devices",
            json={
                "name": f"mock-l2-gating-sw-{uuid.uuid4().hex[:8]}",
                "template_id": gating_template["id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "sw", "mock_fail_actions": "login,status"},
            },
        )
        resp.raise_for_status()
        device = resp.json()
        yield device
        await client.delete(f"/inventory/devices/{device['id']}")


async def _execute(client, device_id, action):
    resp = await client.post(
        "/execution/execute",
        json={
            "device_id": device_id,
            "action": action,
            "user_id": str(uuid.uuid4()),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def test_driver_result_failure_records_failed_run(base_url, admin_token, gating_device):
    """login returns {"success": False, ...}: the run is FAILED, not SUCCESS,
    the driver's error is surfaced, and the output payload is preserved."""
    async with _admin_session_client(base_url, admin_token) as client:
        run = await _execute(client, gating_device["id"], "login")
    assert run["status"] == "FAILED"
    assert run["error"] == "mock injected failure on login"
    assert run["output"] is not None
    assert "mock injected failure on login" in run["output"]


async def test_bare_data_output_stays_success(base_url, admin_token, gating_device):
    """status under the fail knob returns bare data with no success key
    (reachable: False), which the conservative gate must NOT fail."""
    async with _admin_session_client(base_url, admin_token) as client:
        run = await _execute(client, gating_device["id"], "status")
    assert run["status"] == "SUCCESS"
    assert '"reachable": false' in (run["output"] or "")


async def test_unknobbed_action_stays_success(base_url, admin_token, gating_device):
    """logout has no injection configured and returns success: True."""
    async with _admin_session_client(base_url, admin_token) as client:
        run = await _execute(client, gating_device["id"], "logout")
    assert run["status"] == "SUCCESS"
    assert run["error"] is None
