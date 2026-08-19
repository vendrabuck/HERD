"""Integration coverage for the port-aware fork-save resolver (issue #531, ports half).

End-to-end over a running HERD stack: reserve and activate two DUTs with a topology
whose canvas carries TWO L1 edges between the same device pair, each naming a distinct
physical port pair via ``data.source_port_name``/``data.target_port_name`` (the shape
the multi-port wiring dialog, PR #530, stamps onto a canvas edge). Before this fix,
``resolve_canvas_wiring`` ignored per-edge ports and resolved both edges to the same
device-pair path, so the second edge's hop always hit the ``seen`` dedupe and only ONE
fork_connections row was ever created regardless of how many lines the user staged.
This test reads the fork back through the user-facing (owner) endpoint and asserts TWO
connection rows, each carrying the port pair the canvas edge actually named, not an
arbitrary pathfinder pick.

Self-seeds via the ``fresh_devices``/``admin_client`` conftest fixtures and cleans up in
a ``finally`` block. Requires a running HERD stack; without one it errors at connect
time, which is expected. NOT executed by the authoring agent (worktree ports were held
by another running stack); it follows the pattern of
``tests/integration/test_reservation_fork_flow.py::test_fork_lifecycle_read_edit_save_archive``
and should be run against a live stack before merge.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


def _two_port_distinct_edges_canvas(device_a_id: str, device_b_id: str) -> dict:
    """A React Flow canvas with two committed L1 edges between the same device pair.

    Each edge names a distinct port pair, mirroring what the multi-port wiring dialog
    (PR #530) stamps onto ``data`` for every staged line.
    """
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": device_a_id}}},
            {"id": "nB", "data": {"device": {"id": device_b_id}}},
        ],
        "edges": [
            {
                "id": "edge-0",
                "source": "nA",
                "target": "nB",
                "data": {
                    "layer": "L1",
                    "isProposal": False,
                    "source_port_name": "eth1",
                    "target_port_name": "eth1",
                },
            },
            {
                "id": "edge-1",
                "source": "nA",
                "target": "nB",
                "data": {
                    "layer": "L1",
                    "isProposal": False,
                    "source_port_name": "eth2",
                    "target_port_name": "eth2",
                },
            },
        ],
    }


async def _create_connection(client, device_a_id: str, port_a: str, device_b_id: str, port_b: str) -> str:
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": device_a_id,
            "port_a": port_a,
            "device_b_id": device_b_id,
            "port_b": port_b,
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def _create_topology_with_canvas(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies",
        json={"name": f"int-port-fork-{uuid.uuid4().hex[:8]}"},
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _create_reservation(client, device_ids: list[str], topology_id: str) -> str:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "purpose": "port-aware fork-save resolver integration (#531)",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
            "topology_id": topology_id,
        },
    )
    assert resp.status_code == 201, f"reservation create failed: {resp.status_code}: {resp.text}"
    return resp.json()["id"]


def _endpoint_pair(conn: dict) -> frozenset:
    return frozenset(
        {(conn["device_a_id"], conn["port_a"]), (conn["device_b_id"], conn["port_b"])}
    )


async def test_activation_fork_resolves_two_port_distinct_edges_to_two_connections(
    admin_client, fresh_devices
):
    """Two same-pair, port-distinct canvas edges produce two fork_connections rows.

    Activation (fork-on-activation, ``fork_service.create_fork``, which shares
    ``resolve_canvas_wiring`` with the live-edit save path) snapshots the parent
    topology's committed edges. With both edges' ports honored, the fork must carry
    two rows: one for eth1-eth1, one for eth2-eth2, each still tagged L1 (the layer
    half of #531 is deliberately out of scope for this fix).
    """
    devices = await fresh_devices(2)
    a_id, b_id = devices[0]["id"], devices[1]["id"]

    connection_ids: list[str] = []
    topology_id: str | None = None
    reservation_id: str | None = None
    try:
        connection_ids.append(await _create_connection(admin_client, a_id, "eth1", b_id, "eth1"))
        connection_ids.append(await _create_connection(admin_client, a_id, "eth2", b_id, "eth2"))

        topology_id = await _create_topology_with_canvas(
            admin_client, _two_port_distinct_edges_canvas(a_id, b_id)
        )
        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        # Activation is synchronous in the reservation-create path for an
        # immediately-startable reservation, so the fork already exists; GET is the
        # user-facing (owner) read, exactly like the fork-lifecycle integration test.
        got = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert got.status_code == 200, got.text
        fork = got.json()
        assert fork["status"] == "ACTIVE"

        connections = fork["connections"]
        assert len(connections) == 2, (
            "expected two fork_connections rows (one per port-distinct canvas edge), "
            f"got {len(connections)}: {connections}"
        )
        pairs = {_endpoint_pair(c) for c in connections}
        assert frozenset({(a_id, "eth1"), (b_id, "eth1")}) in pairs
        assert frozenset({(a_id, "eth2"), (b_id, "eth2")}) in pairs
        assert all(c["layer"] == "L1" for c in connections)

        # Same assertion holds through an explicit save-reconcile of the identical
        # canvas: the live-edit path shares the same resolver, so re-saving must not
        # collapse the two wires back to one.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": _two_port_distinct_edges_canvas(a_id, b_id)},
        )
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result["released"] == []
        assert result["built"] == []
        assert result["unchanged_count"] == 2
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection_id in connection_ids:
            await admin_client.delete(f"/cabling/connections/{connection_id}")
