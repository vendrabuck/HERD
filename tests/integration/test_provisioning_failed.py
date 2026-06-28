"""Integration test for the provisioning FAILED branch + device revert.

Issue #214 headline cross-cutting gap: when marking exclusive devices RESERVED
in inventory fails after retries on `POST /reservations`, the reservation must
land FAILED, the request must return 503, and any device that DID reach RESERVED
must be compensated back to AVAILABLE.

This exercises that path live by driving inventory's double-gated fault-injection
seam (services/inventory/app/routers/devices.py): with HERD_FAULT_INJECTION set
(docker-compose.override.yml, dev/test only) a device whose name carries the
__herd_fault_status__ sentinel has its `POST /devices/{id}/status` refused with a
503. The reservation create POSTs the two exclusive devices' statuses, the
sentinel device's update keeps failing, retries exhaust, and the compensating
revert runs.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio

# Mirrors services/inventory/app/routers/devices.py:_FAULT_STATUS_SENTINEL.
_FAULT_STATUS_SENTINEL = "__herd_fault_status__"


async def _create_named_device(client, template_id: str, name: str) -> dict:
    """Create a fresh AVAILABLE device with an explicit name (the seam keys off it)."""
    body = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {"model": "test"},
    }
    resp = await client.post("/inventory/devices", json=body)
    resp.raise_for_status()
    return resp.json()


async def test_provisioning_failure_lands_failed_and_reverts_devices(admin_client, dut_template):
    """Retry-exhaustion to FAILED, 503 to caller, and device revert to AVAILABLE.

    Device X carries the fault sentinel so its inventory status update always 503s;
    device Y is normal so its RESERVED update succeeds. The create's status POSTs
    run together, X never goes RESERVED, retries exhaust, the reservation flips to
    FAILED, and the compensating revert returns Y from RESERVED to AVAILABLE.
    """
    suffix = uuid.uuid4().hex[:8]
    purpose = f"int-provision-fail-{suffix}"

    # Device X: name carries the sentinel, so its status update will be refused.
    device_x = await _create_named_device(
        admin_client,
        dut_template["id"],
        f"int-{_FAULT_STATUS_SENTINEL}-{suffix}",
    )
    # Device Y: normal name, its RESERVED update succeeds and is later reverted.
    device_y = await _create_named_device(
        admin_client,
        dut_template["id"],
        f"int-normal-{suffix}",
    )

    try:
        now = datetime.now(timezone.utc)
        body = {
            "device_ids": [device_x["id"], device_y["id"]],
            "purpose": purpose,
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        }
        # Both devices are exclusive (template default) and AVAILABLE, so the create
        # reaches the inventory-flip step, where X's update keeps failing. Allow for
        # the ~1.5s of retry backoff (3 attempts, 0.5s + 1.0s) plus request overhead.
        create_resp = await admin_client.post("/reservations/", json=body, timeout=30.0)

        # 1. Provisioning failed after retries: the router maps the RuntimeError to 503.
        assert create_resp.status_code == 503, create_resp.text

        # 2. The reservation row persists as FAILED. Find it among the caller's
        #    reservations by the unique purpose string (most recent if duplicated).
        list_resp = await admin_client.get("/reservations/", params={"limit": 500})
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        mine = [r for r in items if r.get("purpose") == purpose]
        assert mine, f"no reservation found with purpose {purpose!r}"
        mine.sort(key=lambda r: r["created_at"], reverse=True)
        reservation = mine[0]
        assert reservation["status"] == "FAILED", reservation
        assert set(reservation["device_ids"]) == {device_x["id"], device_y["id"]}

        # 3. Device states via inventory.
        x_resp = await admin_client.get(f"/inventory/devices/{device_x['id']}")
        assert x_resp.status_code == 200
        # X never reached RESERVED (its status update was refused every attempt).
        assert x_resp.json()["status"] == "AVAILABLE"

        y_resp = await admin_client.get(f"/inventory/devices/{device_y['id']}")
        assert y_resp.status_code == 200
        # Y ending AVAILABLE is the proof the compensating revert ran: Y's RESERVED
        # update succeeded during provisioning, so only the post-failure revert
        # returns it to AVAILABLE.
        assert y_resp.json()["status"] == "AVAILABLE"
    finally:
        await admin_client.delete(f"/inventory/devices/{device_x['id']}")
        await admin_client.delete(f"/inventory/devices/{device_y['id']}")
