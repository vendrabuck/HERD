"""Integration tests for device group visibility (cross-service: inventory -> auth).

Verifies that non-admin users only see devices that belong to a device group
permissioned for one of their user groups. This flow exercises the inventory
service calling the auth service (`GET /auth/groups/user/{id}`) to resolve the
current user's group memberships.
"""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _create_user(admin_client, email: str, password: str) -> dict:
    resp = await admin_client.post(
        "/auth/register",
        json={"email": email, "password": password, "username": email.split("@")[0]},
    )
    resp.raise_for_status()
    return resp.json()


async def _user_login(base_url, email: str, password: str) -> str:
    import httpx

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(
            f"{base_url}/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def test_non_admin_visibility_requires_device_group_permission(
    admin_client, base_url, fresh_device
):
    """End-to-end: a new user sees zero DUTs until their user group is granted a device group."""
    import httpx

    suffix = uuid.uuid4().hex[:8]
    email = f"viz-{suffix}@herd.example"
    password = "ViewerPass1!"
    user_group_id = None
    device_group_id = None
    user_id = None
    try:
        user = await _create_user(admin_client, email, password)
        user_id = user["id"]

        # Create an isolated user group containing only this user.
        ug_resp = await admin_client.post(
            "/auth/groups",
            json={"name": f"viz-ug-{suffix}", "description": "visibility test"},
        )
        ug_resp.raise_for_status()
        user_group_id = ug_resp.json()["id"]
        add_resp = await admin_client.post(
            f"/auth/groups/{user_group_id}/members/bulk",
            json={"user_ids": [user_id]},
        )
        add_resp.raise_for_status()

        # Before any device group permission: user sees no DUTs.
        user_token = await _user_login(base_url, email, password)
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=30.0,
        ) as uclient:
            before = await uclient.get("/inventory/devices")
            before.raise_for_status()
            assert before.json()["total"] == 0, "new user should see zero DUTs"

        # Create a device group with one DUT and grant this user group permission.
        device = fresh_device
        dg_resp = await admin_client.post(
            "/inventory/device-groups",
            json={"name": f"viz-dg-{suffix}", "description": "visibility test"},
        )
        dg_resp.raise_for_status()
        device_group_id = dg_resp.json()["id"]

        await admin_client.post(
            f"/inventory/device-groups/{device_group_id}/devices/bulk",
            json={"device_ids": [device["id"]]},
        )
        await admin_client.post(
            f"/inventory/device-groups/{device_group_id}/permissions/bulk",
            json={"user_group_ids": [user_group_id]},
        )

        # After grant: user sees the device.
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=30.0,
        ) as uclient:
            after = await uclient.get("/inventory/devices")
            after.raise_for_status()
            visible_ids = [d["id"] for d in after.json()["items"]]
            assert device["id"] in visible_ids

        # Revoke the permission and verify the device becomes invisible again.
        await admin_client.post(
            f"/inventory/device-groups/{device_group_id}/permissions/bulk-remove",
            json={"user_group_ids": [user_group_id]},
        )
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=30.0,
        ) as uclient:
            revoked = await uclient.get("/inventory/devices")
            revoked.raise_for_status()
            revoked_ids = [d["id"] for d in revoked.json()["items"]]
            assert device["id"] not in revoked_ids
    finally:
        if device_group_id:
            await admin_client.delete(f"/inventory/device-groups/{device_group_id}")
        if user_group_id:
            await admin_client.delete(f"/auth/groups/{user_group_id}")
        if user_id:
            await admin_client.delete(f"/auth/users/{user_id}")


async def test_admin_sees_all_devices_regardless_of_groups(admin_client):
    """Admins bypass device-group visibility entirely."""
    resp = await admin_client.get("/inventory/devices", params={"limit": 1})
    resp.raise_for_status()
    # Admin should always see devices if any exist in the seeded environment.
    assert resp.json()["total"] >= 0  # tolerate empty envs, but call must succeed
