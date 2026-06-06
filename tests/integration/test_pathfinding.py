"""Integration tests for cabling pathfind + graph cache invalidation.

Connections store raw device UUIDs (no FK into inventory), so we can build a
minimal synthetic topology for these tests without creating real devices.
"""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _create_connection(
    client, device_a_id: str, device_b_id: str, port_a: str = "eth1", port_b: str = "eth1"
) -> dict:
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
    return resp.json()


async def _pathfind(client, src: str, tgt: str) -> dict:
    resp = await client.post(
        "/cabling/pathfind",
        json={"source_device_id": src, "target_device_id": tgt},
    )
    resp.raise_for_status()
    return resp.json()


async def test_direct_connection_is_reachable(admin_client):
    """Two devices directly cabled together are reachable in 2 hops (A -> B)."""
    dut_a = str(uuid.uuid4())
    dut_b = str(uuid.uuid4())
    conn_ids = []
    try:
        conn = await _create_connection(admin_client, dut_a, dut_b)
        conn_ids.append(conn["id"])

        result = await _pathfind(admin_client, dut_a, dut_b)
        assert result["reachable"] is True
        assert result["hop_count"] >= 1
    finally:
        for cid in conn_ids:
            await admin_client.delete(f"/cabling/connections/{cid}")


async def test_three_hop_chain_pathfinds(admin_client):
    """A -> B -> C chain is reachable end-to-end."""
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    conn_ids = []
    try:
        conn_ids.append((await _create_connection(admin_client, a, b, "eth1", "eth1"))["id"])
        conn_ids.append((await _create_connection(admin_client, b, c, "eth2", "eth1"))["id"])

        result = await _pathfind(admin_client, a, c)
        assert result["reachable"] is True
        assert result["paths"], "at least one path must be returned"
        hop_ids = [hop["device_id"] for hop in result["paths"][0]]
        assert b in hop_ids, "path must traverse the intermediate node"
    finally:
        for cid in conn_ids:
            await admin_client.delete(f"/cabling/connections/{cid}")


async def test_unreachable_devices_return_not_reachable(admin_client):
    """Two isolated UUIDs with no cabling return reachable=false."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    result = await _pathfind(admin_client, a, b)
    assert result["reachable"] is False
    assert result["hop_count"] == 0
    assert result["paths"] == []


async def test_cache_invalidation_on_connection_delete(admin_client):
    """After deleting an intermediate cable, the end-to-end pathfind flips to unreachable."""
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    conn_ab = None
    conn_bc = None
    try:
        conn_ab = (await _create_connection(admin_client, a, b, "eth1", "eth1"))["id"]
        conn_bc = (await _create_connection(admin_client, b, c, "eth2", "eth1"))["id"]

        before = await _pathfind(admin_client, a, c)
        assert before["reachable"] is True

        # Drop the middle hop; cache must invalidate, path must flip.
        del_resp = await admin_client.delete(f"/cabling/connections/{conn_bc}")
        assert del_resp.status_code == 204
        conn_bc = None

        after = await _pathfind(admin_client, a, c)
        assert after["reachable"] is False
    finally:
        if conn_bc:
            await admin_client.delete(f"/cabling/connections/{conn_bc}")
        if conn_ab:
            await admin_client.delete(f"/cabling/connections/{conn_ab}")


async def test_cache_invalidation_on_connection_create(admin_client):
    """Creating a new cable makes a previously-unreachable pair reachable."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    conn_id = None
    try:
        before = await _pathfind(admin_client, a, b)
        assert before["reachable"] is False

        conn_id = (await _create_connection(admin_client, a, b))["id"]

        after = await _pathfind(admin_client, a, b)
        assert after["reachable"] is True
    finally:
        if conn_id:
            await admin_client.delete(f"/cabling/connections/{conn_id}")
