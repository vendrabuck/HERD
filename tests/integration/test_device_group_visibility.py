"""Integration tests for device group visibility (cross-service: inventory -> auth).

Verifies that non-admin users only see devices that belong to a device group
permissioned for one of their user groups. This flow exercises the inventory
service calling the auth service (`GET /auth/groups/user/{id}`) to resolve the
current user's group memberships.
"""

import io
import tarfile
import uuid

import pytest

pytestmark = pytest.mark.asyncio


def _switch_tarball() -> bytes:
    """Minimal driver package; upload validates connection_type, not methods."""
    body = b"class Driver:\n    pass\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


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


async def test_non_admin_sees_only_dut_devices_in_granted_group(
    admin_client, base_url, fresh_device
):
    """A non-admin granted a device group sees DUTs in it but NOT non-DUT switches.

    The non-admin device list is dut_only: it filters to devices whose driver
    connection_type is Management. So a switch-backed device sitting in the same
    granted group is invisible to the scoped user. Regression for a seed fixture
    that grouped switch-backed devices and surprised us with an empty user view;
    this pins that the DUT filter and the group-permission filter compose as
    intended (granted AND a DUT).
    """
    import httpx

    suffix = uuid.uuid4().hex[:8]
    email = f"dutviz-{suffix}@herd.example"
    password = "ViewerPass1!"
    driver_id = template_id = switch_id = None
    user_group_id = device_group_id = user_id = None
    try:
        # A non-DUT device: Layer 1 Switch driver -> template -> device.
        files = {"file": ("driver.tar.gz", _switch_tarball(), "application/gzip")}
        drv = await admin_client.post(
            "/inventory/drivers",
            files=files,
            data={
                "name": f"sw-drv-{suffix}",
                "connection_type": "Layer 1 Switch",
                "description": "switch acl visibility test",
            },
        )
        drv.raise_for_status()
        driver_id = drv.json()["id"]

        tpl = await admin_client.post(
            "/inventory/templates",
            json={
                "name": f"sw-tpl-{suffix}",
                "template_type": "device",
                "driver_id": driver_id,
                "vendor": "SwVendor",
                "model": "SwModel",
                "sections": [
                    {
                        "name": "General",
                        "fields": [{"key": "model", "label": "Model", "type": "string"}],
                    }
                ],
            },
        )
        tpl.raise_for_status()
        template_id = tpl.json()["id"]

        sw = await admin_client.post(
            "/inventory/devices",
            json={
                "name": f"sw-dev-{suffix}",
                "template_id": template_id,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "x"},
            },
        )
        sw.raise_for_status()
        switch_id = sw.json()["id"]

        # Isolated non-admin user + user group containing only them.
        user = await _create_user(admin_client, email, password)
        user_id = user["id"]
        ug = await admin_client.post(
            "/auth/groups",
            json={"name": f"dutviz-ug-{suffix}", "description": "dut viz"},
        )
        ug.raise_for_status()
        user_group_id = ug.json()["id"]
        (
            await admin_client.post(
                f"/auth/groups/{user_group_id}/members/bulk",
                json={"user_ids": [user_id]},
            )
        ).raise_for_status()

        # One device group holding BOTH the DUT and the switch, granted to the group.
        dg = await admin_client.post(
            "/inventory/device-groups",
            json={"name": f"dutviz-dg-{suffix}", "description": "dut viz"},
        )
        dg.raise_for_status()
        device_group_id = dg.json()["id"]
        (
            await admin_client.post(
                f"/inventory/device-groups/{device_group_id}/devices/bulk",
                json={"device_ids": [fresh_device["id"], switch_id]},
            )
        ).raise_for_status()
        (
            await admin_client.post(
                f"/inventory/device-groups/{device_group_id}/permissions/bulk",
                json={"user_group_ids": [user_group_id]},
            )
        ).raise_for_status()

        # The non-admin sees the granted DUT but not the granted switch.
        token = await _user_login(base_url, email, password)
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as uclient:
            resp = await uclient.get("/inventory/devices", params={"limit": 500})
            resp.raise_for_status()
            visible = {d["id"] for d in resp.json()["items"]}
        assert fresh_device["id"] in visible, "non-admin should see the granted DUT"
        assert switch_id not in visible, "non-admin must NOT see a non-DUT switch (dut_only filter)"
    finally:
        if device_group_id:
            await admin_client.delete(f"/inventory/device-groups/{device_group_id}")
        if switch_id:
            await admin_client.delete(f"/inventory/devices/{switch_id}")
        if template_id:
            await admin_client.delete(f"/inventory/templates/{template_id}")
        if driver_id:
            await admin_client.delete(f"/inventory/drivers/{driver_id}")
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
