"""Integration coverage for the reservation -> cabling validate cross-service gate.

Reservations referencing a topology must call /api/cabling/topologies/{id}/validate
and refuse to create when the topology has unreachable edges in the physical
cabling graph. Cabling-side unit tests prove the validator logic; this file
proves the reservations service actually invokes it and surfaces the error
back to the caller as 422.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


def _canvas_with_edge(device_a_id: str, device_b_id: str) -> dict:
    """Minimal canvas mirroring the React Flow editor output: two device nodes
    connected by one L2 edge."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": device_a_id}}},
            {"id": "nB", "data": {"device": {"id": device_b_id}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L2", "isProposal": False},
            }
        ],
    }


async def _make_topology_with_canvas(client, device_a_id: str, device_b_id: str) -> str:
    create = await client.post(
        "/cabling/topologies",
        json={"name": f"int-validate-gate-{uuid.uuid4().hex[:8]}"},
    )
    create.raise_for_status()
    topology_id = create.json()["id"]
    canvas = _canvas_with_edge(device_a_id, device_b_id)
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


def _reservation_body(device_ids: list[str], topology_id: str | None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "validate-gate integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if topology_id is not None:
        body["topology_id"] = topology_id
    return body


async def test_reservation_rejected_when_topology_has_unreachable_edges(
    admin_client, fresh_devices
):
    """Reserving a topology whose canvas has an edge between uncabled devices fails 422."""
    devices = await fresh_devices(2)
    a_id, b_id = devices[0]["id"], devices[1]["id"]

    topology_id = await _make_topology_with_canvas(admin_client, a_id, b_id)
    try:
        resp = await admin_client.post(
            "/reservations/",
            json=_reservation_body([a_id, b_id], topology_id),
        )
        assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
        detail = resp.json().get("detail", "")
        assert "unreachable" in detail.lower(), (
            f"422 detail must explain the connectivity gate, got: {detail!r}"
        )
    finally:
        await admin_client.delete(f"/cabling/topologies/{topology_id}")


async def test_reservation_succeeds_when_topology_edges_are_reachable(admin_client, fresh_devices):
    """Same shape as the negative test, but with a physical cable between the devices."""
    devices = await fresh_devices(2)
    a_id, b_id = devices[0]["id"], devices[1]["id"]

    cable = await admin_client.post(
        "/cabling/connections",
        json={
            "device_a_id": a_id,
            "port_a": "eth1",
            "device_b_id": b_id,
            "port_b": "eth1",
            "connection_type": "L1",
        },
    )
    cable.raise_for_status()
    connection_id = cable.json()["id"]

    topology_id = await _make_topology_with_canvas(admin_client, a_id, b_id)
    reservation_id: str | None = None
    try:
        resp = await admin_client.post(
            "/reservations/",
            json=_reservation_body([a_id, b_id], topology_id),
        )
        assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
        reservation_id = resp.json()["id"]
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_id}")
