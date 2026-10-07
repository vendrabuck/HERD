"""Issue #1023: a port is never deleted or renamed while a cabling cable names it.

Cabling stores a connection's ports by NAME, with no reference to inventory's
port id, so deleting or renaming a cabled port would leave the cable naming a
port that no longer exists. Inventory's `DELETE /ports/{id}` and a name-changing
`PUT /ports/{id}` ask cabling's `GET /connections/internal/by-port` first and
refuse with 409 `port_cabled` (`connection_count`, `connection_ids`) until the
cable is removed. Same answer shape as the device DELETE guard's
`device_cabled` (issue #940).
"""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _port_template(admin_client) -> str:
    resp = await admin_client.post(
        "/inventory/templates",
        json={
            "name": f"int-port-guard-tpl-{uuid.uuid4().hex[:8]}",
            "template_type": "port",
            "sections": [
                {"name": "General", "fields": [{"key": "note", "label": "Note", "type": "string"}]}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _port(admin_client, device_id: str, template_id: str, name: str) -> str:
    resp = await admin_client.post(
        f"/inventory/devices/{device_id}/ports",
        json={"name": name, "template_id": template_id, "field_data": {}},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_cabled_port_refuses_delete_and_rename_until_the_cable_is_removed(
    admin_client, fresh_devices
):
    """Port ge-0/0/1 on device A is cabled to B; port ge-0/0/2 on A is not.
    Delete and rename of the cabled port are refused and change nothing; the
    uncabled sibling renames freely; once the cable is gone both operations on
    the formerly cabled port succeed. Every cleanup sits in a finally, cable
    first, so a failed assertion never leaks a Connection row or a port."""
    dev_a, dev_b = await fresh_devices(2)
    template_id = await _port_template(admin_client)
    port_ids: list[str] = []
    connection_id = None
    try:
        cabled = await _port(admin_client, dev_a["id"], template_id, "ge-0/0/1")
        free = await _port(admin_client, dev_a["id"], template_id, "ge-0/0/2")
        port_ids += [cabled, free]

        resp = await admin_client.post(
            "/cabling/connections",
            json={
                "device_a_id": dev_b["id"],
                "port_a": "eth0",
                "device_b_id": dev_a["id"],
                "port_b": "ge-0/0/1",
                "connection_type": "L1",
            },
        )
        assert resp.status_code == 201, resp.text
        connection_id = resp.json()["id"]
        expected = {
            "error": "port_cabled",
            "connection_count": 1,
            "connection_ids": [connection_id],
        }

        blocked_delete = await admin_client.delete(f"/inventory/ports/{cabled}")
        assert blocked_delete.status_code == 409, blocked_delete.text
        assert blocked_delete.json()["detail"] == expected

        blocked_rename = await admin_client.put(
            f"/inventory/ports/{cabled}", json={"name": "ge-0/0/9"}
        )
        assert blocked_rename.status_code == 409, blocked_rename.text
        assert blocked_rename.json()["detail"] == expected

        still = await admin_client.get(f"/inventory/ports/{cabled}")
        assert still.status_code == 200
        assert still.json()["name"] == "ge-0/0/1"

        # Not a rename: a cabled port stays editable.
        same_name = await admin_client.put(
            f"/inventory/ports/{cabled}", json={"name": "ge-0/0/1", "field_data": {"note": "x"}}
        )
        assert same_name.status_code == 200, same_name.text

        sibling = await admin_client.put(f"/inventory/ports/{free}", json={"name": "ge-0/0/3"})
        assert sibling.status_code == 200, sibling.text

        gone = await admin_client.delete(f"/cabling/connections/{connection_id}")
        assert gone.status_code == 204, gone.text
        connection_id = None

        renamed = await admin_client.put(f"/inventory/ports/{cabled}", json={"name": "ge-0/0/9"})
        assert renamed.status_code == 200, renamed.text
        deleted = await admin_client.delete(f"/inventory/ports/{cabled}")
        assert deleted.status_code == 204, deleted.text
        port_ids.remove(cabled)
    finally:
        if connection_id is not None:
            await admin_client.delete(f"/cabling/connections/{connection_id}")
        for port_id in port_ids:
            await admin_client.delete(f"/inventory/ports/{port_id}")
        await admin_client.delete(f"/inventory/templates/{template_id}")
