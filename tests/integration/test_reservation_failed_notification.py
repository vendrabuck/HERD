"""A reservation that lands FAILED notifies its owner in the app (issue #1077).

Drives a real FAILED transition through the same fault-injection seam
`test_provisioning_failed.py` uses: with HERD_FAULT_INJECTION set
(docker-compose.override.yml, dev and test only) inventory refuses the status
update of a device whose name carries `__herd_fault_status__`, the create's
inventory flip exhausts its retries, and reservations writes FAILED and stages
`reservation.failed` in the same transaction. The notifications consumer must
turn that event into one in-app notification for the owner whose text names the
reservation and carries no upstream error text.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ._device_teardown import delete_device_checked

pytestmark = pytest.mark.asyncio

# Mirrors services/inventory/app/routers/devices.py:_FAULT_STATUS_SENTINEL.
_FAULT_STATUS_SENTINEL = "__herd_fault_status__"
PREFS_PATH = "/notifications/notifications/preferences"


async def _poll_for_failed_notification(client, reservation_id: str, timeout: float = 15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/notifications/notifications", params={"limit": 50})
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                if item.get("event_type") != "reservation.failed":
                    continue
                if (item.get("data") or {}).get("reservation_id") == reservation_id:
                    return item
        await asyncio.sleep(0.5)
    return None


async def test_failed_reservation_notifies_owner_in_app(admin_client, dut_template):
    prefs = await admin_client.get(PREFS_PATH)
    assert prefs.status_code == 200, prefs.text
    body = prefs.json()
    if not body["channels"]["in_app"] or not body["events"].get("reservation.failed", True):
        pytest.fail(
            "precondition: the admin account has the in-app channel or the "
            "reservation.failed event turned off; restore its notification preferences"
        )

    suffix = uuid.uuid4().hex[:8]
    purpose = f"int-failed-notify-{suffix}"
    create_device = await admin_client.post(
        "/inventory/devices",
        json={
            "name": f"int-{_FAULT_STATUS_SENTINEL}-notify-{suffix}",
            "template_id": dut_template["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    create_device.raise_for_status()
    device = create_device.json()

    try:
        now = datetime.now(timezone.utc)
        create_resp = await admin_client.post(
            "/reservations/",
            json={
                "device_ids": [device["id"]],
                "purpose": purpose,
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
            timeout=30.0,
        )
        assert create_resp.status_code == 503, create_resp.text

        listed = await admin_client.get("/reservations/", params={"search": purpose})
        assert listed.status_code == 200, listed.text
        mine = [r for r in listed.json()["items"] if r.get("purpose") == purpose]
        assert len(mine) == 1, mine
        reservation = mine[0]
        assert reservation["status"] == "FAILED", reservation

        matched = await _poll_for_failed_notification(admin_client, reservation["id"])
        assert matched is not None, "reservation.failed notification did not arrive"
        assert matched["title"] == "Reservation failed"
        assert matched["body"] == f"Reservation {reservation['id'][:8]} for 1 device failed."
        assert matched["read_at"] is None
        assert matched["data"]["event"] == "reservation.failed"
        assert matched["data"]["device_ids"] == [device["id"]]
    finally:
        await delete_device_checked(admin_client, device["id"])
