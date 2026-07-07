"""Integration tests for POST /inventory/devices/batch (issue #250).

The batch endpoint replaces the topology editor's per-device GET fan-out with
a single request. These tests exercise the live route through Traefik: admin
fetch with an unknown id omitted, non-admin group-visibility omission (the
inventory-to-auth resolution path), and the request-size cap.
"""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_admin_batch_fetch_returns_requested_and_omits_unknown(admin_client, fresh_devices):
    devices = await fresh_devices(2)
    ids = [d["id"] for d in devices]
    unknown = str(uuid.uuid4())

    resp = await admin_client.post("/inventory/devices/batch", json={"device_ids": ids + [unknown]})
    resp.raise_for_status()
    items = resp.json()["items"]
    returned = {d["id"] for d in items}
    # Both real devices come back; the unknown id is omitted, never a 404.
    assert returned == set(ids)
    # Payloads carry the full DeviceResponse shape the editor hydrates from.
    by_id = {d["id"]: d for d in items}
    for device in devices:
        assert by_id[device["id"]]["name"] == device["name"]
        assert by_id[device["id"]]["template_id"] == device["template_id"]


async def test_non_admin_batch_omits_devices_outside_visibility(
    user_client, visible_fresh_device, fresh_devices
):
    """The intuser sees the granted device; an ungranted sibling is omitted
    from the same batch (no 403/404 for the whole request)."""
    # A second device left in the default "No Pool" group: invisible to intuser.
    invisible = (await fresh_devices(1))[0]

    resp = await user_client.post(
        "/inventory/devices/batch",
        json={"device_ids": [visible_fresh_device["id"], invisible["id"]]},
    )
    resp.raise_for_status()
    returned = {d["id"] for d in resp.json()["items"]}
    assert visible_fresh_device["id"] in returned
    assert invisible["id"] not in returned


async def test_batch_over_cap_returns_422(admin_client):
    ids = [str(uuid.uuid4()) for _ in range(501)]
    resp = await admin_client.post("/inventory/devices/batch", json={"device_ids": ids})
    assert resp.status_code == 422
