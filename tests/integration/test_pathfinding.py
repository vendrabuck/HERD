"""Integration tests for cabling pathfind + graph cache invalidation.

Connections store raw device UUIDs (no cross-schema FK into inventory), but
since issue #392 creating a connection validates both devices exist in
inventory, so tests that CREATE connections use the fresh_devices factory.
Pathfind itself performs no existence check, so pathfind-only tests still use
synthetic UUIDs for isolated/unreachable endpoints.
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


async def test_direct_connection_is_reachable(admin_client, fresh_devices):
    """Two devices directly cabled together are reachable in 2 hops (A -> B)."""
    dut_a, dut_b = [d["id"] for d in await fresh_devices(2)]
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


async def test_three_hop_chain_pathfinds(admin_client, fresh_devices):
    """A -> B -> C chain is reachable end-to-end."""
    a, b, c = [d["id"] for d in await fresh_devices(3)]
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


async def test_cache_invalidation_on_connection_delete(admin_client, fresh_devices):
    """After deleting an intermediate cable, the end-to-end pathfind flips to unreachable."""
    a, b, c = [d["id"] for d in await fresh_devices(3)]
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


async def test_batch_pathfind_resolves_pairs_in_order(admin_client, fresh_devices):
    """One batch request resolves reachable and unreachable pairs, preserving order.

    Chain a -> b -> c plus an isolated device d: (a, c) is reachable through b,
    (a, d) is not, and each batch entry echoes its requested pair with the same
    shape the single endpoint returns (issue #249).
    """
    a, b, c, d = [dev["id"] for dev in await fresh_devices(4)]
    conn_ids = []
    try:
        conn_ids.append((await _create_connection(admin_client, a, b, "eth1", "eth1"))["id"])
        conn_ids.append((await _create_connection(admin_client, b, c, "eth2", "eth1"))["id"])

        resp = await admin_client.post(
            "/cabling/pathfind/batch",
            json={
                "pairs": [
                    {"source_device_id": a, "target_device_id": c},
                    {"source_device_id": a, "target_device_id": d},
                    {"source_device_id": c, "target_device_id": a},
                ]
            },
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        assert len(results) == 3

        # Request order is preserved and each entry echoes its pair.
        assert [(r["source_device_id"], r["target_device_id"]) for r in results] == [
            (a, c),
            (a, d),
            (c, a),
        ]

        # Reachable pair matches the single endpoint's result exactly.
        single = await _pathfind(admin_client, a, c)
        first = dict(results[0])
        assert first.pop("source_device_id") == a
        assert first.pop("target_device_id") == c
        assert first == single

        # Unreachable pair uses the single endpoint's no-path shape.
        assert results[1]["reachable"] is False
        assert results[1]["hop_count"] == 0
        assert results[1]["paths"] == []

        # Reverse direction is reachable too (undirected cabling graph).
        assert results[2]["reachable"] is True
    finally:
        for cid in conn_ids:
            await admin_client.delete(f"/cabling/connections/{cid}")


async def test_batch_pathfind_empty_pairs_returns_empty_results(admin_client):
    """An empty pairs list is accepted and yields an empty results list."""
    resp = await admin_client.post("/cabling/pathfind/batch", json={"pairs": []})
    resp.raise_for_status()
    assert resp.json() == {"results": []}


async def test_batch_pathfind_over_cap_rejected(admin_client):
    """A pair list beyond the server cap (2000) is rejected with 422."""
    pair = {"source_device_id": str(uuid.uuid4()), "target_device_id": str(uuid.uuid4())}
    resp = await admin_client.post(
        "/cabling/pathfind/batch",
        json={"pairs": [pair] * 2001},
    )
    assert resp.status_code == 422


async def test_cache_invalidation_on_connection_create(admin_client, fresh_devices):
    """Creating a new cable makes a previously-unreachable pair reachable."""
    a, b = [d["id"] for d in await fresh_devices(2)]
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
