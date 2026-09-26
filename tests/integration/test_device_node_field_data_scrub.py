"""Integration proof: a device node's field_data never survives a save.

Hardening: the topology editor used to persist the whole inventory Device
record (including field_data, which can carry a clear-text device password)
onto a canvas device node, and cabling stored and returned whatever canvas a
caller sent. This drives the real gateway route with a canvas carrying
field_data (a synthetic marker string, never a real credential) and asserts
the marker never reaches a GET, a version GET, or an export (JSON or CSV).

Requires a running stack (`make test-integration`). This test proves the fix
against whatever cabling code the stack is running: on a plain dev stack
booted from the main checkout mount, that is still pre-fix code, so this test
is EXPECTED TO FAIL there until the fix lands on main. CI's advisory
integration job (which builds and runs the branch under review) is the first
real proof; see the fix's own commit history for the unit- and HTTP-level
tests that already pass against this branch's code directly.
"""

import uuid

import pytest

# A marker used in place of a real credential: never a real password, just a
# distinctive string this test can grep the serialized response for.
_PASSWORD_MARKER = f"herd-int-test-marker-{uuid.uuid4().hex[:12]}"


def _dirty_canvas(device_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": "n1",
                "type": "deviceNode",
                "data": {
                    "device": {
                        "id": device_id,
                        "name": "int-test-device",
                        "topology_type": "PHYSICAL",
                        "status": "AVAILABLE",
                        "field_data": {"password": _PASSWORD_MARKER, "host": "10.0.0.1"},
                        "template_id": str(uuid.uuid4()),
                        "driver_sha256": "abc123",
                        "exclusive": True,
                    },
                    "label": "int-test-device",
                    "topologyType": "PHYSICAL",
                },
            }
        ],
        "edges": [],
    }


def _assert_clean(body_text: str) -> None:
    assert "field_data" not in body_text
    assert _PASSWORD_MARKER not in body_text


@pytest.mark.asyncio
async def test_field_data_never_survives_a_topology_save(admin_client, fresh_device):
    """PUT a canvas carrying field_data, then read it back through every
    relevant surface, cleaning up the topology afterward."""
    create = await admin_client.post(
        "/cabling/topologies", json={"name": f"int-scrub-{uuid.uuid4().hex[:8]}"}
    )
    create.raise_for_status()
    topology_id = create.json()["id"]

    try:
        put_resp = await admin_client.put(
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": _dirty_canvas(fresh_device["id"])},
        )
        assert put_resp.status_code == 200, put_resp.text
        _assert_clean(put_resp.text)

        get_resp = await admin_client.get(f"/cabling/topologies/{topology_id}")
        assert get_resp.status_code == 200
        _assert_clean(get_resp.text)
        device = get_resp.json()["canvas_data"]["nodes"][0]["data"]["device"]
        assert device["id"] == fresh_device["id"]
        assert "field_data" not in device

        versions_resp = await admin_client.get(f"/cabling/topologies/{topology_id}/versions")
        assert versions_resp.status_code == 200
        version_id = versions_resp.json()["items"][0]["id"]
        version_detail = await admin_client.get(
            f"/cabling/topologies/{topology_id}/versions/{version_id}"
        )
        assert version_detail.status_code == 200
        _assert_clean(version_detail.text)

        json_export = await admin_client.get(
            "/cabling/topologies/export", params={"format": "json"}
        )
        assert json_export.status_code == 200
        _assert_clean(json_export.text)

        csv_export = await admin_client.get("/cabling/topologies/export", params={"format": "csv"})
        assert csv_export.status_code == 200
        _assert_clean(csv_export.text)
    finally:
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
