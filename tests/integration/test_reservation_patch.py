"""Integration tests for PATCH /reservations/{id}: extend end_time, add/remove devices.

Each test uses the fresh_devices factory fixture to provision its own exclusive
DUT devices, creates a reservation, and cleans up after itself.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _create_reservation(client, device_ids: list[str], hours: int = 1) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": device_ids,
        "purpose": "patch integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=hours)).isoformat(),
    }
    resp = await client.post("/reservations/", json=body)
    resp.raise_for_status()
    return resp.json()


async def _cancel(client, res_id: str) -> None:
    await client.delete(f"/reservations/{res_id}")


async def test_patch_extends_end_time(admin_client, fresh_devices):
    """PATCH with a later end_time updates the reservation window."""
    duts = await fresh_devices(1)

    res = await _create_reservation(admin_client, [duts[0]["id"]])
    try:
        new_end = datetime.now(timezone.utc) + timedelta(hours=3)
        resp = await admin_client.patch(
            f"/reservations/{res['id']}",
            json={"end_time": new_end.isoformat()},
        )
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["end_time"].startswith(new_end.isoformat()[:16])
    finally:
        await _cancel(admin_client, res["id"])


async def test_patch_updates_purpose(admin_client, fresh_devices):
    """PATCH with a new purpose updates the reservation purpose text."""
    duts = await fresh_devices(1)

    res = await _create_reservation(admin_client, [duts[0]["id"]])
    try:
        resp = await admin_client.patch(
            f"/reservations/{res['id']}",
            json={"purpose": "updated-via-patch"},
        )
        assert resp.status_code == 200
        assert resp.json()["purpose"] == "updated-via-patch"
    finally:
        await _cancel(admin_client, res["id"])


async def test_patch_adds_device_and_reserves_it(admin_client, fresh_devices):
    """Adding a device to a reservation marks that device RESERVED."""
    duts = await fresh_devices(2)

    res = await _create_reservation(admin_client, [duts[0]["id"]])
    try:
        new_ids = [duts[0]["id"], duts[1]["id"]]
        resp = await admin_client.patch(
            f"/reservations/{res['id']}",
            json={"device_ids": new_ids},
        )
        assert resp.status_code == 200
        assert sorted(resp.json()["device_ids"]) == sorted(new_ids)

        # Added device should now be RESERVED.
        dev_resp = await admin_client.get(f"/inventory/devices/{duts[1]['id']}")
        dev_resp.raise_for_status()
        assert dev_resp.json()["status"] == "RESERVED"
    finally:
        await _cancel(admin_client, res["id"])


async def test_patch_removes_device_and_releases_it(admin_client, fresh_devices):
    """Removing a device from a reservation releases it back to AVAILABLE."""
    duts = await fresh_devices(2)

    res = await _create_reservation(admin_client, [duts[0]["id"]], hours=1)
    # Extend first to add the second device.
    await admin_client.patch(
        f"/reservations/{res['id']}",
        json={"device_ids": [duts[0]["id"], duts[1]["id"]]},
    )
    try:
        resp = await admin_client.patch(
            f"/reservations/{res['id']}",
            json={"device_ids": [duts[0]["id"]]},
        )
        assert resp.status_code == 200
        assert resp.json()["device_ids"] == [duts[0]["id"]]

        # Removed device should be AVAILABLE again.
        dev_resp = await admin_client.get(f"/inventory/devices/{duts[1]['id']}")
        dev_resp.raise_for_status()
        assert dev_resp.json()["status"] == "AVAILABLE"
    finally:
        await _cancel(admin_client, res["id"])


async def test_patch_nonexistent_reservation_returns_404(admin_client):
    import uuid

    resp = await admin_client.patch(
        f"/reservations/{uuid.uuid4()}",
        json={"purpose": "ghost"},
    )
    assert resp.status_code == 404
