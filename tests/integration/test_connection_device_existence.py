"""Integration test: POST /connections rejects nonexistent device UUIDs.

Issue #392: inventory's GET /device-groups/device/{id} used to return 200 []
for a device that does not exist, indistinguishable from an existing-but-
ungrouped device, so cabling's device-group boundary guard fell through its
empty-set short-circuit and let a connection to a bogus or deleted device UUID
through. Inventory now 404s on that route for a nonexistent device, and
cabling's guard turns that 404 into a hard 4xx reject instead of the existing
fail-open (that fail-open is intentionally preserved for a genuinely
unreachable/erroring inventory; see test_cabling_group_boundary.py and
services/cabling/tests/test_service_unit.py). Assumes the shipped default
ENFORCE_DEVICE_GROUP_BOUNDARIES=true.
"""

import uuid

import pytest


def _connect_body(device_a_id: str, device_b_id: str) -> dict:
    return {
        "device_a_id": device_a_id,
        "port_a": "eth0",
        "device_b_id": device_b_id,
        "port_b": "eth0",
        "connection_type": "L1",
    }


@pytest.mark.asyncio
async def test_connection_rejected_when_both_devices_nonexistent(admin_client):
    body = _connect_body(str(uuid.uuid4()), str(uuid.uuid4()))
    resp = await admin_client.post("/cabling/connections", json=body)
    assert resp.status_code >= 400 and resp.status_code < 500, resp.text


@pytest.mark.asyncio
async def test_connection_rejected_when_one_device_nonexistent(admin_client, fresh_device):
    body = _connect_body(fresh_device["id"], str(uuid.uuid4()))
    resp = await admin_client.post("/cabling/connections", json=body)
    assert resp.status_code >= 400 and resp.status_code < 500, resp.text


@pytest.mark.asyncio
async def test_connection_succeeds_with_two_real_devices(admin_client, fresh_devices):
    devices = await fresh_devices(2)
    connection_id = None
    try:
        body = _connect_body(devices[0]["id"], devices[1]["id"])
        resp = await admin_client.post("/cabling/connections", json=body)
        assert resp.status_code == 201, resp.text
        connection_id = resp.json()["id"]
    finally:
        if connection_id:
            try:
                await admin_client.delete(f"/cabling/connections/{connection_id}")
            except Exception:
                pass
