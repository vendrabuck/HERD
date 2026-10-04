"""Integration coverage for the topology DELETE guard (issue #977).

cabling refuses `DELETE /cabling/topologies/{id}` with 409
`{"error": "topology_in_use", "reservation_ids": [...]}` while a PENDING,
PENDING_PROVISION, or ACTIVE reservation references the topology, asking
reservations' internal by-topology route over X-Internal-Token. A terminal
reservation does not block, and the 403 for a non-owner comes before the
lookup. The fail-closed 503 needs reservations down, so it is pinned by the
cabling unit suite (tests/test_topology_delete_guard.py) rather than here.

Requires a running HERD stack. Self-seeds via the conftest fixtures; every
reservation is cancelled and every topology deleted in try/finally.
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ._topology_teardown import delete_topology_checked

pytestmark = pytest.mark.asyncio

ACTIVE_POLL_SECONDS = 20


async def _create_topology(client, device_id: str) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-topo-guard-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    canvas = {"nodes": [{"id": "n1", "data": {"device": {"id": device_id}}}], "edges": []}
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_id: str, topology_id: str, *, starts_in: timedelta) -> dict:
    start = datetime.now(timezone.utc) + starts_in
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": [device_id],
            "topology_id": topology_id,
            "purpose": "topology delete guard integration",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 201, f"reservation create failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _wait_active(client, reservation_id: str) -> str:
    deadline = time.monotonic() + ACTIVE_POLL_SECONDS
    status = None
    while time.monotonic() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        resp.raise_for_status()
        status = resp.json()["status"]
        if status != "PENDING_PROVISION":
            return status
        await asyncio.sleep(0.5)
    return status


async def _assert_refused(client, topology_id: str, reservation_id: str) -> None:
    resp = await client.delete(f"/cabling/topologies/{topology_id}")
    assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
    assert resp.json()["detail"] == {
        "error": "topology_in_use",
        "reservation_ids": [reservation_id],
    }
    still = await client.get(f"/cabling/topologies/{topology_id}")
    assert still.status_code == 200, "a refused delete must leave the topology in place"


async def _cancel(client, reservation_id: str) -> None:
    resp = await client.delete(f"/reservations/{reservation_id}")
    assert resp.status_code in (200, 204), f"cancel failed: {resp.status_code} {resp.text}"
    read = await client.get(f"/reservations/{reservation_id}")
    assert read.json()["status"] == "CANCELLED"


async def _assert_deleted(client, topology_id: str) -> None:
    resp = await client.delete(f"/cabling/topologies/{topology_id}")
    assert resp.status_code == 204, f"expected 204, got {resp.status_code}: {resp.text}"
    gone = await client.get(f"/cabling/topologies/{topology_id}")
    assert gone.status_code == 404


async def test_future_pending_reservation_blocks_delete_until_cancelled(admin_client, fresh_device):
    """The defect case: a PENDING booking would otherwise activate with an empty fork."""
    topology_id = await _create_topology(admin_client, fresh_device["id"])
    reservation_id = None
    try:
        reservation = await _reserve(
            admin_client, fresh_device["id"], topology_id, starts_in=timedelta(days=2)
        )
        reservation_id = reservation["id"]
        assert reservation["status"] == "PENDING"

        await _assert_refused(admin_client, topology_id, reservation_id)

        await _cancel(admin_client, reservation_id)
        reservation_id = None
        await _assert_deleted(admin_client, topology_id)
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await delete_topology_checked(admin_client, topology_id)


async def test_active_reservation_blocks_delete_until_cancelled(admin_client, fresh_device):
    topology_id = await _create_topology(admin_client, fresh_device["id"])
    reservation_id = None
    try:
        reservation = await _reserve(
            admin_client, fresh_device["id"], topology_id, starts_in=timedelta(0)
        )
        reservation_id = reservation["id"]
        assert await _wait_active(admin_client, reservation_id) == "ACTIVE"

        await _assert_refused(admin_client, topology_id, reservation_id)

        await _cancel(admin_client, reservation_id)
        reservation_id = None
        await _assert_deleted(admin_client, topology_id)
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await delete_topology_checked(admin_client, topology_id)


async def test_non_owner_gets_403_not_the_reservation_ids(admin_client, user_client, fresh_device):
    """The 403 runs before the lookup: a non-owner learns nothing about bookings."""
    topology_id = await _create_topology(admin_client, fresh_device["id"])
    reservation_id = None
    try:
        reservation = await _reserve(
            admin_client, fresh_device["id"], topology_id, starts_in=timedelta(days=2)
        )
        reservation_id = reservation["id"]
        resp = await user_client.delete(f"/cabling/topologies/{topology_id}")
        assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
        assert reservation_id not in resp.text
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await delete_topology_checked(admin_client, topology_id)
