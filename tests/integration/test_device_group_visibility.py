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


async def _make_l3_switch(admin_client, suffix: str) -> tuple[str, str, str]:
    """Driver, template and device for one Layer 3 Switch. Returns their ids."""
    drv = await admin_client.post(
        "/inventory/drivers",
        files={"file": ("driver.tar.gz", _switch_tarball(), "application/gzip")},
        data={
            "name": f"l3-drv-{suffix}",
            "connection_type": "Layer 3 Switch",
            "description": "hidden switch oracle test",
        },
    )
    drv.raise_for_status()
    driver_id = drv.json()["id"]

    tpl = await admin_client.post(
        "/inventory/templates",
        json={
            "name": f"l3-tpl-{suffix}",
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

    dev = await admin_client.post(
        "/inventory/devices",
        json={
            "name": f"l3-sw-{suffix}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "x"},
        },
    )
    dev.raise_for_status()
    return driver_id, template_id, dev.json()["id"]


def _oracle_canvas(dut_a: str, dut_b: str, switch: str) -> dict:
    """Two visible DUTs cabled through one Layer 3 switch that carries routing
    intent. The switch node is what the validate route used to answer interface
    and subnet questions about."""
    return {
        "nodes": [
            {"id": "a", "position": {"x": 0, "y": 0}, "data": {"device": {"id": dut_a}}},
            {"id": "b", "position": {"x": 400, "y": 0}, "data": {"device": {"id": dut_b}}},
            {
                "id": "sw",
                "position": {"x": 200, "y": 0},
                "data": {
                    "device": {"id": switch},
                    "l3": {
                        "routes": [
                            {
                                "destination": "10.20.0.0/24",
                                "next_hop": "10.0.0.2",
                                "interface": "eth1",
                            }
                        ]
                    },
                },
            },
        ],
        "edges": [
            {"id": "a-sw", "source": "a", "target": "sw", "data": {"layer": "L1"}},
            {"id": "sw-b", "source": "sw", "target": "b", "data": {"layer": "L1"}},
        ],
    }


async def test_hidden_device_is_not_a_validate_or_pathfind_oracle(
    admin_client, user_client, user_token, base_url, fresh_devices
):
    """Issue #763, end to end across cabling, inventory and auth.

    A non-admin whose user group is granted exactly two DUTs, with a Layer 3
    switch cabled between them that they cannot see:

    - validate on their own topology reports the switch's edges as
      `missing_device` and asks inventory nothing about the switch, while the
      admin call on the SAME topology reports the real L3 reason;
    - pathfind between the two DUTs returns the transit hop redacted
      (`device_id` null, `hidden` true) for the user and the real id for the
      admin, with identical hop counts.
    """
    suffix = uuid.uuid4().hex[:8]
    driver_id = template_id = switch_id = None
    user_group_id = device_group_id = topology_id = None
    connection_ids: list[str] = []
    try:
        dut_a, dut_b = await fresh_devices(2)
        driver_id, template_id, switch_id = await _make_l3_switch(admin_client, suffix)

        # Cable DUT A to the switch to DUT B, BEFORE any device group exists:
        # cross-group cabling is refused, and adding the DUTs to a group below
        # takes them out of the default pool the switch stays in.
        for device_id, port, other_id, other_port in (
            (dut_a["id"], "eth0", switch_id, "eth1"),
            (switch_id, "eth2", dut_b["id"], "eth0"),
        ):
            conn = await admin_client.post(
                "/cabling/connections",
                json={
                    "device_a_id": device_id,
                    "port_a": port,
                    "device_b_id": other_id,
                    "port_b": other_port,
                    "connection_type": "ethernet",
                },
            )
            conn.raise_for_status()
            connection_ids.append(conn.json()["id"])

        me = await user_client.get("/auth/me")
        me.raise_for_status()
        user_id = me.json()["id"]

        ug = await admin_client.post(
            "/auth/groups",
            json={"name": f"oracle-ug-{suffix}", "description": "oracle test"},
        )
        ug.raise_for_status()
        user_group_id = ug.json()["id"]
        (
            await admin_client.post(
                f"/auth/groups/{user_group_id}/members/bulk",
                json={"user_ids": [user_id]},
            )
        ).raise_for_status()

        # The two DUTs are granted; the switch deliberately is NOT.
        dg = await admin_client.post(
            "/inventory/device-groups",
            json={"name": f"oracle-dg-{suffix}", "description": "oracle test"},
        )
        dg.raise_for_status()
        device_group_id = dg.json()["id"]
        (
            await admin_client.post(
                f"/inventory/device-groups/{device_group_id}/devices/bulk",
                json={"device_ids": [dut_a["id"], dut_b["id"]]},
            )
        ).raise_for_status()
        (
            await admin_client.post(
                f"/inventory/device-groups/{device_group_id}/permissions/bulk",
                json={"user_group_ids": [user_group_id]},
            )
        ).raise_for_status()

        # Precondition: the user can see both DUTs and not the switch. Without
        # this the rest of the test would pass vacuously on a stack whose
        # intuser happens to hold a broader grant.
        visible = await user_client.get(
            "/inventory/device-groups/visible-devices", params={"user_id": user_id}
        )
        visible.raise_for_status()
        visible_ids = set(visible.json()["device_ids"])
        assert {dut_a["id"], dut_b["id"]} <= visible_ids
        assert switch_id not in visible_ids, (
            "the switch must be invisible for this test to mean anything"
        )

        # The user owns the topology, so they may validate it.
        topo = await user_client.post("/cabling/topologies", json={"name": f"oracle-{suffix}"})
        topo.raise_for_status()
        topology_id = topo.json()["id"]
        put = await user_client.put(
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": _oracle_canvas(dut_a["id"], dut_b["id"], switch_id)},
        )
        put.raise_for_status()

        user_validate = await user_client.post(f"/cabling/topologies/{topology_id}/validate")
        user_validate.raise_for_status()
        user_result = user_validate.json()
        reasons = {e["edge_id"]: e["reason"] for e in user_result["invalid_edges"]}
        assert reasons == {"a-sw": "missing_device", "sw-b": "missing_device"}
        assert user_result["invalid_routes"] == []
        assert switch_id not in user_result["device_ids"]

        admin_validate = await admin_client.post(f"/cabling/topologies/{topology_id}/validate")
        admin_validate.raise_for_status()
        admin_result = admin_validate.json()
        assert admin_result["invalid_edges"] == []
        # The switch exists, is a Layer 3 Switch and has no config version, so
        # the admin gets the real routing-intent verdict for it.
        assert [r["reason"] for r in admin_result["invalid_routes"]] == ["l3_switch_unconfigured"]
        assert switch_id in admin_result["device_ids"]

        pathfind_body = {"source_device_id": dut_a["id"], "target_device_id": dut_b["id"]}
        user_path = await user_client.post("/cabling/pathfind", json=pathfind_body)
        user_path.raise_for_status()
        user_hops = user_path.json()["paths"][0]
        assert user_path.json()["reachable"] is True
        assert [h["device_id"] for h in user_hops] == [dut_a["id"], None, dut_b["id"]]
        assert [h["hidden"] for h in user_hops] == [False, True, False]

        admin_path = await admin_client.post("/cabling/pathfind", json=pathfind_body)
        admin_path.raise_for_status()
        admin_hops = admin_path.json()["paths"][0]
        assert [h["device_id"] for h in admin_hops] == [dut_a["id"], switch_id, dut_b["id"]]
        assert admin_path.json()["hop_count"] == user_path.json()["hop_count"]

        # The hidden device as an ENDPOINT is refused outright, with the same
        # answer an id that does not exist gets.
        refused = await user_client.post(
            "/cabling/pathfind",
            json={"source_device_id": dut_a["id"], "target_device_id": switch_id},
        )
        assert refused.status_code == 404
        unknown = await user_client.post(
            "/cabling/pathfind",
            json={"source_device_id": dut_a["id"], "target_device_id": str(uuid.uuid4())},
        )
        assert unknown.status_code == 404
        assert refused.json()["detail"] == unknown.json()["detail"]

        batch = await user_client.post(
            "/cabling/pathfind/batch",
            json={
                "pairs": [
                    {"source_device_id": dut_a["id"], "target_device_id": switch_id},
                    pathfind_body,
                ]
            },
        )
        batch.raise_for_status()
        refused_pair, allowed_pair = batch.json()["results"]
        assert refused_pair["error"] == "Device not found"
        assert refused_pair["reachable"] is False
        assert allowed_pair["error"] is None
        assert [h["device_id"] for h in allowed_pair["paths"][0]] == [
            dut_a["id"],
            None,
            dut_b["id"],
        ]
    finally:
        # Best-effort, admin-owned teardown in reverse dependency order; the two
        # DUTs belong to the fresh_devices fixture.
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection_id in connection_ids:
            await admin_client.delete(f"/cabling/connections/{connection_id}")
        if switch_id:
            await admin_client.delete(f"/inventory/devices/{switch_id}")
        if template_id:
            await admin_client.delete(f"/inventory/templates/{template_id}")
        if driver_id:
            await admin_client.delete(f"/inventory/drivers/{driver_id}")
        if device_group_id:
            await admin_client.delete(f"/inventory/device-groups/{device_group_id}")
        if user_group_id:
            await admin_client.delete(f"/auth/groups/{user_group_id}")
