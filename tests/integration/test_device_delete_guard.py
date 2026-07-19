"""Issue #391: inventory's device delete must not orphan a UUID a reservation

still holds. DELETE /devices/{id} calls reservations' existing
/internal/by-device lookup (the same cross-service guard the config-restore
path already established, issue #337) and refuses the delete while a
non-terminal (PENDING/PENDING_PROVISION/ACTIVE) reservation includes the
device. There is deliberately no force flag.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _create_reservation(client, device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "issue 391 delete guard integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    return await client.post("/reservations/", json=body)


async def test_delete_blocked_by_active_reservation_then_succeeds_after_cancel(
    admin_client, fresh_device
):
    """A device held by a non-terminal reservation 409s on delete, naming the
    blocking reservation; cancelling the reservation then lets the delete through."""
    create_resp = await _create_reservation(admin_client, fresh_device["id"])
    assert create_resp.status_code == 201, create_resp.text
    reservation = create_resp.json()
    res_id = reservation["id"]
    assert reservation["status"] in ("PENDING", "PENDING_PROVISION", "ACTIVE")

    try:
        blocked_resp = await admin_client.delete(f"/inventory/devices/{fresh_device['id']}")
        assert blocked_resp.status_code == 409, blocked_resp.text
        detail = blocked_resp.json()["detail"]
        assert detail["error"] == "device_in_use"
        assert res_id in detail["reservation_ids"]

        # The device was never touched: it still resolves.
        get_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
        assert get_resp.status_code == 200
    finally:
        cancel_resp = await admin_client.delete(f"/reservations/{res_id}")
        assert cancel_resp.status_code == 204

    delete_resp = await admin_client.delete(f"/inventory/devices/{fresh_device['id']}")
    assert delete_resp.status_code == 204

    gone_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
    assert gone_resp.status_code == 404


async def test_delete_succeeds_for_unreserved_device(admin_client, fresh_device):
    """An AVAILABLE device with no reservations deletes normally (the
    pre-#391 happy path stays unchanged)."""
    delete_resp = await admin_client.delete(f"/inventory/devices/{fresh_device['id']}")
    assert delete_resp.status_code == 204

    gone_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
    assert gone_resp.status_code == 404
