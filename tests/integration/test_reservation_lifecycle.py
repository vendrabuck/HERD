"""Integration tests for reservation lifecycle against a running HERD stack."""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _create_reservation(client, device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    resp = await client.post("/reservations/", json=body)
    return resp


@pytest.fixture
async def reservation_with_cleanup(admin_client, fresh_device):
    """Create a reservation and clean it up after the test."""
    resp = await _create_reservation(admin_client, fresh_device["id"])
    assert resp.status_code == 201
    reservation = resp.json()
    yield reservation, fresh_device
    if reservation["status"] in ("ACTIVE", "PENDING", "PENDING_PROVISION"):
        await admin_client.delete(f"/reservations/{reservation['id']}")


async def test_create_reservation_changes_device_status(admin_client, reservation_with_cleanup):
    """Creating a reservation marks the device as RESERVED."""
    reservation, device = reservation_with_cleanup
    resp = await admin_client.get(f"/inventory/devices/{device['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "RESERVED"


async def test_cancel_reservation_releases_device(admin_client, fresh_device):
    """Cancelling a reservation returns the device to AVAILABLE."""
    create_resp = await _create_reservation(admin_client, fresh_device["id"])
    assert create_resp.status_code == 201
    res_id = create_resp.json()["id"]

    cancel_resp = await admin_client.delete(f"/reservations/{res_id}")
    assert cancel_resp.status_code == 204

    device_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
    assert device_resp.status_code == 200
    assert device_resp.json()["status"] == "AVAILABLE"


async def test_overlapping_reservation_is_rejected(admin_client, fresh_device):
    """Reserving the same exclusive device twice fails.

    An active reservation flips the device to RESERVED immediately, so the
    second request is rejected by the availability precheck (422) rather than
    reaching the time-window conflict check (409).
    """
    resp1 = await _create_reservation(admin_client, fresh_device["id"])
    assert resp1.status_code == 201
    res_id = resp1.json()["id"]

    try:
        resp2 = await _create_reservation(admin_client, fresh_device["id"])
        assert resp2.status_code in (409, 422)
    finally:
        await admin_client.delete(f"/reservations/{res_id}")


async def test_calendar_returns_reservation(admin_client, fresh_device):
    """Calendar endpoint returns newly created reservation."""
    resp = await _create_reservation(admin_client, fresh_device["id"])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    try:
        now = datetime.now(timezone.utc)
        cal_resp = await admin_client.get(
            "/reservations/calendar",
            params={
                "range_start": (now - timedelta(hours=1)).isoformat(),
                "range_end": (now + timedelta(hours=2)).isoformat(),
            },
        )
        assert cal_resp.status_code == 200
        ids = [r["id"] for r in cal_resp.json()]
        assert res_id in ids
    finally:
        await admin_client.delete(f"/reservations/{res_id}")


async def test_release_reservation_returns_device_to_available(admin_client, fresh_device):
    """Releasing an active reservation returns the device to AVAILABLE."""
    resp = await _create_reservation(admin_client, fresh_device["id"])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    release_resp = await admin_client.put(f"/reservations/{res_id}/release")
    assert release_resp.status_code == 200
    assert release_resp.json()["status"] == "COMPLETED"

    device_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
    assert device_resp.status_code == 200
    assert device_resp.json()["status"] == "AVAILABLE"
