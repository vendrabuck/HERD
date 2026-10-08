"""Execution's device-scoped checks against a running stack (issues #1108, #1112).

- GET /execution/device-health/{device_id} resolves a non-admin's device-group
  visibility through inventory's real visible-devices route with the caller's own
  token: a visible device answers 200 under its own id, and a device outside the
  caller's visibility answers exactly as an id that names no device (OPS-HEALTH-1).
- POST /execution/execute with a reservation_id asks reservations' internal
  by-device list before anything runs: a reservation that does not hold the device
  is refused with the pinned 422 and no run row is written, while the caller's own
  reservation holding the device passes the check (CFG-EXEC-3).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio

ADMIN_MISMATCH_DETAIL = "reservation_id must reference a reservation that includes this device"


async def test_health_read_of_a_visible_device_answers_under_its_id(
    user_client, visible_fresh_device
):
    resp = await user_client.get(f"/execution/device-health/{visible_fresh_device['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["device_id"] == visible_fresh_device["id"]


async def test_health_read_of_a_hidden_device_answers_like_an_unknown_id(user_client, fresh_device):
    hidden = await user_client.get(f"/execution/device-health/{fresh_device['id']}")
    unknown_id = str(uuid.uuid4())
    unknown = await user_client.get(f"/execution/device-health/{unknown_id}")
    assert hidden.status_code == unknown.status_code == 200, (hidden.text, unknown.text)
    hidden_body = hidden.json()
    unknown_body = unknown.json()
    assert hidden_body.pop("device_id") == fresh_device["id"]
    assert unknown_body.pop("device_id") == unknown_id
    assert hidden_body == unknown_body
    assert hidden_body["last_status"] == "UNKNOWN"


async def test_execute_refuses_a_reservation_that_does_not_hold_the_device(
    admin_client, fresh_device
):
    resp = await admin_client.post(
        "/execution/execute",
        json={
            "device_id": fresh_device["id"],
            "action": "status",
            "user_id": str(uuid.uuid4()),
            "reservation_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": ADMIN_MISMATCH_DETAIL}
    runs = await admin_client.get("/execution/runs", params={"device_id": fresh_device["id"]})
    assert runs.status_code == 200, runs.text
    assert runs.json()["total"] == 0, runs.json()


async def test_execute_accepts_the_callers_reservation_holding_the_device(
    admin_client, fresh_device
):
    now = datetime.now(timezone.utc)
    create = await admin_client.post(
        "/reservations/",
        json={
            "device_ids": [fresh_device["id"]],
            "purpose": "execute reservation scope integration",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert create.status_code == 201, create.text
    reservation = create.json()
    try:
        resp = await admin_client.post(
            "/execution/execute",
            json={
                "device_id": fresh_device["id"],
                "action": "status",
                "user_id": str(uuid.uuid4()),
                "reservation_id": reservation["id"],
            },
        )
        # The reservation check passed: whatever follows (a run, or the
        # driverless device's 409), it is not the reservation refusal.
        assert resp.status_code != 422, resp.text
        assert resp.status_code != 503, resp.text
        if resp.status_code == 201:
            assert resp.json()["reservation_id"] == reservation["id"]
    finally:
        await admin_client.delete(f"/reservations/{reservation['id']}")
