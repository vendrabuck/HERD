"""Integration coverage for editing a running reservation's topology.

This exercises the cross-service flow that lets the owner of a live reservation
edit the topology behind it while keeping it consistent, and that blocks an
unrelated user from mutating the wiring out from under that reservation:

- Phase 1 (reservations -> cabling): PATCH /reservations/{id} with a changed
  device set re-runs the topology connectivity gate via the cabling service's
  /topologies/{id}/validate/internal. An edit that strands a topology edge is
  rejected with 400 (mirrors the create-path gate in
  test_topology_validate_gate.py, which surfaces as 422 on create).
- Phase 2 (cabling reservation lock): PUT /cabling/topologies/{id} with a
  canvas_data change is blocked with 409 for a non-owner when the topology has a
  live reservation owned by someone else, and allowed for the reservation owner
  (the owner carve-out). A name-only edit never touches live wiring, so it is
  not gated even for a non-owner.

The service-side unit tests prove the gate logic; this file proves the services
actually wire together over HTTP. Tests self-seed via the conftest fixtures and
clean up their reservations, topologies, and connections in try/finally. They
require a running HERD stack; without one they error at connect time, which is
expected.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


def _canvas_with_edge(device_a_id: str, device_b_id: str) -> dict:
    """Minimal React Flow canvas: two device nodes joined by one L2 edge.

    node.data.device.id is the inventory device id the cabling validator
    resolves against the physical Connection graph.
    """
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


async def _create_topology(client, name_prefix: str = "int-live-edit") -> str:
    resp = await client.post(
        "/cabling/topologies",
        json={"name": f"{name_prefix}-{uuid.uuid4().hex[:8]}"},
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def _set_canvas(client, topology_id: str, canvas: dict):
    """PUT a canvas onto a topology; returns the raw response (callers assert)."""
    return await client.put(
        f"/cabling/topologies/{topology_id}",
        json={"canvas_data": canvas},
    )


async def _create_connection(client, device_a_id: str, device_b_id: str) -> str:
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": device_a_id,
            "port_a": "eth1",
            "device_b_id": device_b_id,
            "port_b": "eth1",
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _reservation_body(device_ids: list[str], topology_id: str | None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "live-edit topology integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if topology_id is not None:
        body["topology_id"] = topology_id
    return body


async def _create_reservation(client, device_ids: list[str], topology_id: str | None) -> str:
    resp = await client.post("/reservations/", json=_reservation_body(device_ids, topology_id))
    assert resp.status_code == 201, f"reservation create failed: {resp.status_code}: {resp.text}"
    return resp.json()["id"]


async def test_owner_can_edit_canvas_of_reserved_topology(admin_client, fresh_devices):
    """Owner carve-out: the reservation owner may PUT a canvas change on a topology
    that has their own live reservation.

    Build a connected, reachable topology (real cable between the two devices),
    reserve it, then have the owner re-PUT the same canvas. The reservation lock
    only blocks *other* users, so the owner's edit returns 200.
    """
    devices = await fresh_devices(2)
    a_id, b_id = devices[0]["id"], devices[1]["id"]

    connection_id = await _create_connection(admin_client, a_id, b_id)
    topology_id = await _create_topology(admin_client)
    reservation_id: str | None = None
    try:
        canvas = _canvas_with_edge(a_id, b_id)
        first = await _set_canvas(admin_client, topology_id, canvas)
        first.raise_for_status()

        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        # Owner re-edits the canvas while the reservation is live: allowed.
        edited = _canvas_with_edge(a_id, b_id)
        edited["nodes"][0]["data"]["device"]["label"] = "owner-edited"
        resp = await _set_canvas(admin_client, topology_id, edited)
        assert resp.status_code == 200, (
            f"owner carve-out must allow the edit, got {resp.status_code}: {resp.text}"
        )
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_id}")


async def test_non_owner_canvas_edit_of_reserved_topology_is_blocked_409(
    admin_client, user_client, visible_fresh_device, fresh_devices
):
    """A different user PUTting a canvas change on a topology with someone else's
    live reservation is blocked with 409.

    The admin owns a topology and a live reservation referencing it. The
    non-admin intuser (not the reservation owner) attempts a canvas change and is
    refused by the cabling reservation lock. visible_fresh_device provides one
    device the intuser can see; a second device rounds out the canvas edge.
    """
    second = (await fresh_devices(1))[0]
    a_id, b_id = visible_fresh_device["id"], second["id"]

    connection_id = await _create_connection(admin_client, a_id, b_id)
    topology_id = await _create_topology(admin_client)
    reservation_id: str | None = None
    try:
        first = await _set_canvas(admin_client, topology_id, _canvas_with_edge(a_id, b_id))
        first.raise_for_status()

        # Admin owns the live reservation on this topology.
        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        # Non-owner intuser tries to change the wiring: blocked.
        edited = _canvas_with_edge(a_id, b_id)
        edited["nodes"][1]["data"]["device"]["label"] = "stranger-edited"
        resp = await _set_canvas(user_client, topology_id, edited)
        assert resp.status_code == 409, (
            f"non-owner canvas edit on a reserved topology must 409, "
            f"got {resp.status_code}: {resp.text}"
        )
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_id}")


async def test_non_owner_name_only_edit_of_reserved_topology_is_allowed(
    admin_client, user_client, visible_fresh_device, fresh_devices
):
    """A name-only topology edit by a non-owner during a live reservation is not
    gated: the reservation lock only guards canvas (wiring) changes.

    Same setup as the 409 test, but the intuser PUTs a name change with no
    canvas_data, so the lock does not apply and the edit succeeds.
    """
    second = (await fresh_devices(1))[0]
    a_id, b_id = visible_fresh_device["id"], second["id"]

    connection_id = await _create_connection(admin_client, a_id, b_id)
    topology_id = await _create_topology(admin_client)
    reservation_id: str | None = None
    try:
        first = await _set_canvas(admin_client, topology_id, _canvas_with_edge(a_id, b_id))
        first.raise_for_status()

        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        new_name = f"int-renamed-{uuid.uuid4().hex[:8]}"
        resp = await user_client.put(
            f"/cabling/topologies/{topology_id}",
            json={"name": new_name},
        )
        assert resp.status_code == 200, (
            f"name-only edit must not be gated by the reservation lock, "
            f"got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["name"] == new_name
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_id}")


async def test_patch_device_set_revalidates_topology_and_succeeds_when_connected(
    admin_client, fresh_devices
):
    """Phase 1, happy path: PATCH the reservation's device set on a fully cabled
    topology re-runs the connectivity gate and returns 200.

    The two topology devices are physically cabled, so the validate/internal gate
    passes; adding a third (uncabled) device that is not part of any topology edge
    leaves every edge reachable, so the edit is accepted.
    """
    devices = await fresh_devices(3)
    a_id, b_id, c_id = devices[0]["id"], devices[1]["id"], devices[2]["id"]

    connection_id = await _create_connection(admin_client, a_id, b_id)
    topology_id = await _create_topology(admin_client)
    reservation_id: str | None = None
    try:
        first = await _set_canvas(admin_client, topology_id, _canvas_with_edge(a_id, b_id))
        first.raise_for_status()

        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        new_ids = [a_id, b_id, c_id]
        resp = await admin_client.patch(
            f"/reservations/{reservation_id}",
            json={"device_ids": new_ids},
        )
        assert resp.status_code == 200, (
            f"device-set PATCH on a connected topology must pass the gate, "
            f"got {resp.status_code}: {resp.text}"
        )
        assert sorted(resp.json()["device_ids"]) == sorted(new_ids)
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_id}")


async def test_patch_device_set_rejected_when_topology_edge_unreachable(
    admin_client, fresh_devices
):
    """Phase 1, gate path: PATCH the reservation's device set when the topology has
    an edge between two uncabled devices is rejected with 400.

    The topology edge a-b has no physical cable, so the validate/internal gate
    reports it unreachable. The reservation is created first while the cable
    exists, then the cable is dropped so the live topology strands an edge; the
    subsequent device-set PATCH must fail the re-validation. This mirrors the
    create-path gate in test_topology_validate_gate.py (which returns 422 on
    create; the PATCH path surfaces the same ValueError as 400).
    """
    devices = await fresh_devices(3)
    a_id, b_id, c_id = devices[0]["id"], devices[1]["id"], devices[2]["id"]

    connection_id: str | None = await _create_connection(admin_client, a_id, b_id)
    topology_id = await _create_topology(admin_client)
    reservation_id: str | None = None
    try:
        first = await _set_canvas(admin_client, topology_id, _canvas_with_edge(a_id, b_id))
        first.raise_for_status()

        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        # Strand the topology edge by removing the only physical cable. The live
        # topology now has an unreachable edge.
        del_resp = await admin_client.delete(f"/cabling/connections/{connection_id}")
        assert del_resp.status_code == 204
        connection_id = None

        # Any change to the device set re-runs the connectivity gate, which now fails.
        resp = await admin_client.patch(
            f"/reservations/{reservation_id}",
            json={"device_ids": [a_id, b_id, c_id]},
        )
        assert resp.status_code == 400, (
            f"device-set PATCH must be rejected when a topology edge is unreachable, "
            f"got {resp.status_code}: {resp.text}"
        )
        detail = resp.json().get("detail", "")
        assert "unreachable" in detail.lower(), (
            f"400 detail must explain the connectivity gate, got: {detail!r}"
        )
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection_id:
            await admin_client.delete(f"/cabling/connections/{connection_id}")
