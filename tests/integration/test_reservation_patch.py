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


async def _create_window(client, device_ids, start, end) -> dict:
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "purpose": "patch integration",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _device_status(client, device_id: str) -> str:
    resp = await client.get(f"/inventory/devices/{device_id}")
    resp.raise_for_status()
    return resp.json()["status"]


async def test_patch_add_to_pending_accepts_a_device_busy_now_and_writes_no_status(
    admin_client, fresh_devices
):
    """Issue #999: a device RESERVED now but free in a PENDING row's window can be added,
    as create would accept it; a PENDING row holds nothing, so inventory is untouched
    (issue #994, RES-HOLD-1)."""
    duts = await fresh_devices(2)
    now = datetime.now(timezone.utc)
    busy = await _create_reservation(admin_client, [duts[1]["id"]], hours=1)
    later = await _create_window(
        admin_client,
        [duts[0]["id"]],
        now + timedelta(hours=2),
        now + timedelta(hours=3),
    )
    try:
        assert later["status"] == "PENDING"
        assert await _device_status(admin_client, duts[1]["id"]) == "RESERVED"
        resp = await admin_client.patch(
            f"/reservations/{later['id']}",
            json={"device_ids": [duts[0]["id"], duts[1]["id"]]},
        )
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["device_ids"]) == sorted([duts[0]["id"], duts[1]["id"]])
        assert await _device_status(admin_client, duts[1]["id"]) == "RESERVED"
        assert await _device_status(admin_client, duts[0]["id"]) == "AVAILABLE"
    finally:
        await _cancel(admin_client, later["id"])
        await _cancel(admin_client, busy["id"])


async def test_patch_add_to_pending_refuses_a_device_booked_over_the_window(
    admin_client, fresh_devices
):
    """Issue #999: the window conflict check decides, with create's wording."""
    duts = await fresh_devices(2)
    now = datetime.now(timezone.utc)
    start, end = now + timedelta(hours=2), now + timedelta(hours=3)
    mine = await _create_window(admin_client, [duts[0]["id"]], start, end)
    theirs = await _create_window(admin_client, [duts[1]["id"]], start, end)
    try:
        resp = await admin_client.patch(
            f"/reservations/{mine['id']}",
            json={"device_ids": [duts[0]["id"], duts[1]["id"]]},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            f"Time conflict: devices ['{duts[1]['id']}'] already reserved in the requested window"
        )
    finally:
        await _cancel(admin_client, mine["id"])
        await _cancel(admin_client, theirs["id"])


async def test_patch_end_time_past_the_maximum_duration_is_refused(admin_client, fresh_devices):
    """Issue #995: PATCH applies RESERVATION_MAX_DURATION_SECONDS (default 30 days)."""
    duts = await fresh_devices(1)
    res = await _create_reservation(admin_client, [duts[0]["id"]])
    try:
        start = datetime.fromisoformat(res["start_time"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        resp = await admin_client.patch(
            f"/reservations/{res['id']}",
            json={"end_time": (start + timedelta(days=400)).isoformat()},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("reservation duration exceeds the maximum of ")
    finally:
        await _cancel(admin_client, res["id"])
