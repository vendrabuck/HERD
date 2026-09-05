"""Integration test: cabling enforces device-group boundaries.

Two devices whose device-group sets are disjoint cannot be cabled together
(422); two that share a group can (201). Exercises the cabling -> inventory
group-membership lookup over a forwarded JWT. Assumes the shipped default
ENFORCE_DEVICE_GROUP_BOUNDARIES=true.

Also covers issue #719: GET /cabling/connections filters a non-admin caller
to connections touching at least one visible device, while an admin stays
unfiltered. Exercises the cabling -> inventory visible-devices lookup over a
forwarded JWT.
"""

import uuid

import pytest


async def _create_group(admin_client, name: str) -> str:
    resp = await admin_client.post(
        "/inventory/device-groups",
        json={"name": name, "description": "boundary-int"},
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def _add_devices(admin_client, group_id: str, device_ids: list[str]) -> None:
    resp = await admin_client.post(
        f"/inventory/device-groups/{group_id}/devices/bulk",
        json={"device_ids": device_ids},
    )
    resp.raise_for_status()


def _connect_body(device_a: dict, device_b: dict) -> dict:
    return {
        "device_a_id": device_a["id"],
        "port_a": "eth0",
        "device_b_id": device_b["id"],
        "port_b": "eth0",
        "connection_type": "L1",
    }


@pytest.mark.asyncio
async def test_cross_group_cabling_rejected(admin_client, fresh_devices):
    devices = await fresh_devices(2)
    suffix = uuid.uuid4().hex[:6]
    group_a = group_b = None
    try:
        group_a = await _create_group(admin_client, f"bnd-a-{suffix}")
        group_b = await _create_group(admin_client, f"bnd-b-{suffix}")
        await _add_devices(admin_client, group_a, [devices[0]["id"]])
        await _add_devices(admin_client, group_b, [devices[1]["id"]])

        resp = await admin_client.post("/cabling/connections", json=_connect_body(*devices))
        assert resp.status_code == 422, resp.text
        assert "device group" in resp.text.lower()
    finally:
        for group_id in (group_a, group_b):
            if group_id:
                try:
                    await admin_client.delete(f"/inventory/device-groups/{group_id}")
                except Exception:
                    pass


@pytest.mark.asyncio
async def test_same_group_cabling_allowed(admin_client, fresh_devices):
    devices = await fresh_devices(2)
    suffix = uuid.uuid4().hex[:6]
    group_id = None
    connection_id = None
    try:
        group_id = await _create_group(admin_client, f"bnd-same-{suffix}")
        await _add_devices(admin_client, group_id, [devices[0]["id"], devices[1]["id"]])

        resp = await admin_client.post("/cabling/connections", json=_connect_body(*devices))
        assert resp.status_code == 201, resp.text
        connection_id = resp.json()["id"]
    finally:
        if connection_id:
            try:
                await admin_client.delete(f"/cabling/connections/{connection_id}")
            except Exception:
                pass
        if group_id:
            try:
                await admin_client.delete(f"/inventory/device-groups/{group_id}")
            except Exception:
                pass


@pytest.mark.asyncio
async def test_non_admin_connections_filtered_to_visible_devices(
    admin_client, user_client, fresh_devices, visible_fresh_device
):
    """GET /cabling/connections filters a non-admin to visible-device connections
    (issue #719), while an admin still sees the whole fleet.

    visible_fresh_device is granted to the shared intuser via a device group;
    fresh_devices(2) produces two throwaway devices that stay in "No Pool"
    (no user-group permission), so they are invisible to that same intuser.
    Three connections are created: one touching the visible device (should
    appear for the non-admin), and one between the two invisible devices
    (should be absent for the non-admin, present for the admin).
    """
    invisible_devices = await fresh_devices(2)
    connection_visible_id = None
    connection_invisible_id = None
    try:
        resp = await admin_client.post(
            "/cabling/connections",
            json=_connect_body(visible_fresh_device, invisible_devices[0]),
        )
        assert resp.status_code == 201, resp.text
        connection_visible_id = resp.json()["id"]

        resp = await admin_client.post(
            "/cabling/connections",
            json={
                "device_a_id": invisible_devices[0]["id"],
                "port_a": "eth1",
                "device_b_id": invisible_devices[1]["id"],
                "port_b": "eth0",
                "connection_type": "L1",
            },
        )
        assert resp.status_code == 201, resp.text
        connection_invisible_id = resp.json()["id"]

        # Non-admin: only the connection touching the visible device appears.
        user_resp = await user_client.get("/cabling/connections", params={"limit": 500})
        user_resp.raise_for_status()
        user_ids = {c["id"] for c in user_resp.json()["items"]}
        assert connection_visible_id in user_ids
        assert connection_invisible_id not in user_ids

        # Admin passthrough: both connections are visible, unfiltered.
        admin_resp = await admin_client.get("/cabling/connections", params={"limit": 500})
        admin_resp.raise_for_status()
        admin_ids = {c["id"] for c in admin_resp.json()["items"]}
        assert connection_visible_id in admin_ids
        assert connection_invisible_id in admin_ids
    finally:
        for connection_id in (connection_visible_id, connection_invisible_id):
            if connection_id:
                try:
                    await admin_client.delete(f"/cabling/connections/{connection_id}")
                except Exception:
                    pass
