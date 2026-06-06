"""Regression test for the execution service's internal inventory fetch.

Guards against a bug (introduced in the ROADMAP #13 iter-1 refactor) where
`fetch_device` / `fetch_template` in the execution service called the
Bearer-JWT-only inventory routes (`/devices/{id}`, `/templates/{id}`) while
authenticating with only an `X-Internal-Token`. A token-only caller gets 401
from those routes, so the call raised 502 and every scheduled health poll
recorded the device as UNREACHABLE before any driver ran. The fix points both
helpers at the token-guarded `/internal` routes.

This exercises the real cross-service call (the thing the original unit tests
mocked, which is why the bug shipped): `POST /execution/device-check` is
internal-token authed and runs `fetch_device` -> `fetch_template` ->
login/status/logout. Under the buggy code it 502s on the fetch; with the fix it
reaches the driver and returns SUCCESS.
"""

import io
import os
import uuid
import zipfile

import httpx
import pytest

pytestmark = pytest.mark.asyncio

# A complete Management driver: all five methods the connection type requires
# for validation, with login/status/logout succeeding so device-check can
# reach a SUCCESS verdict.
_DRIVER_PY = b'''\
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def configure(self, **kwargs):
        return {"success": True}

    def backup(self):
        return {"success": True}

    def status(self):
        return {"reachable": True}
'''

_DRIVER_METADATA = b'{"supports_dry_run": false, "version": "1.0", "vendor": "integration"}'


def _driver_zip() -> bytes:
    """Package the driver as a zip; the execution driver loader extracts a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", _DRIVER_PY)
        zf.writestr("driver_metadata.json", _DRIVER_METADATA)
    return buf.getvalue()


async def test_device_check_uses_internal_inventory_routes(admin_client, base_url, admin_token):
    """device-check resolves device+template over the /internal routes and runs the driver.

    Asserts the call does not 502 on the internal fetch and returns SUCCESS,
    which is only reachable if fetch_device and fetch_template authenticated
    against inventory with the internal token.
    """
    internal_token = os.getenv("INTERNAL_API_TOKEN")
    if not internal_token:
        pytest.skip("INTERNAL_API_TOKEN not set in the test environment")

    suffix = uuid.uuid4().hex[:8]

    # Upload a working Management driver.
    files = {"file": ("driver.zip", _driver_zip(), "application/zip")}
    data = {
        "name": f"int-internal-fetch-driver-{suffix}",
        "connection_type": "Management",
        "description": "regression: internal fetch routes",
    }
    resp = await admin_client.post("/inventory/drivers", files=files, data=data)
    resp.raise_for_status()
    driver = resp.json()

    template_id = None
    device_id = None
    try:
        # Template referencing the driver.
        resp = await admin_client.post(
            "/inventory/templates",
            json={
                "name": f"int-internal-fetch-tmpl-{suffix}",
                "template_type": "device",
                "driver_id": driver["id"],
                "vendor": "IntegrationVendor",
                "model": "IntegrationModel",
                "sections": [
                    {
                        "name": "Network",
                        "fields": [
                            {"key": "ip", "label": "IP", "type": "string", "required": True}
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
        template_id = resp.json()["id"]

        # A device on that template.
        resp = await admin_client.post(
            "/inventory/devices",
            json={
                "name": f"int-internal-fetch-dev-{suffix}",
                "template_id": template_id,
                "topology_type": "PHYSICAL",
                "field_data": {"ip": "10.0.0.2"},
            },
        )
        resp.raise_for_status()
        device_id = resp.json()["id"]

        # The acting user id (device-check records it on the ExecutionRun).
        me = await admin_client.get("/auth/me")
        me.raise_for_status()
        user_id = me.json()["id"]

        # device-check is internal-token authed; this drives fetch_device ->
        # fetch_template -> run_driver_action(login/status/logout).
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"X-Internal-Token": internal_token},
            timeout=30.0,
        ) as internal_client:
            check = await internal_client.post(
                "/execution/device-check",
                json={"device_id": device_id, "user_id": user_id},
            )

        # Under the pre-fix bug this would be 502 (Failed to fetch device/template).
        assert check.status_code == 200, (
            f"device-check returned {check.status_code}: {check.text} "
            "(a 502 here means the execution service hit the Bearer-only inventory "
            "route with an internal token)"
        )
        body = check.json()
        assert body["status"] == "SUCCESS", (
            f"expected SUCCESS, got {body['status']} (error={body.get('error')})"
        )
    finally:
        if device_id:
            await admin_client.delete(f"/inventory/devices/{device_id}")
        if template_id:
            await admin_client.delete(f"/inventory/templates/{template_id}")
        await admin_client.delete(f"/inventory/drivers/{driver['id']}")
