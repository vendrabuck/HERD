"""Issue #391: inventory's device delete must not orphan a UUID a reservation

still holds. DELETE /devices/{id} calls reservations' existing
/internal/by-device lookup (the same cross-service guard the config-restore
path already established, issue #337) and refuses the delete while a
non-terminal (PENDING/PENDING_PROVISION/ACTIVE) reservation includes the
device. There is deliberately no force flag.

Issue #900 extends the guard to TRANSIT hops: a switch that is in no
reservation's booked set but carries a live fork's cross-connects (cable A to S
to B, reserve A and B with an edge A to B) must also refuse the delete, since
once the inventory row is gone there is no driver left to release the hops.
Inventory asks cabling's GET /internal/forks/by-device/{id}; the 409 carries
`transit_reservation_ids` beside `reservation_ids`.

Issue #940 adds the plain-cable axis: a device that cabling still names on a
`Connection` row, with no reservation at all, refuses the delete with 409
`device_cabled` (`connection_count`, `connection_ids`) until its cables are
removed. Same cabling response, checked after `device_in_use`.
"""

import asyncio
import io
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def _create_reservation(client, device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "issue 391 delete guard integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    return await client.post("/reservations/", json=body)


async def test_delete_blocked_by_active_reservation_then_succeeds_after_cancel(
    admin_client, fresh_device
):
    """A device held by a non-terminal reservation 409s on delete, naming the
    blocking reservation; cancelling the reservation then lets the delete through."""
    create_resp = await _create_reservation(admin_client, fresh_device["id"])
    assert create_resp.status_code == 201, create_resp.text
    reservation = create_resp.json()
    res_id = reservation["id"]
    assert reservation["status"] in ("PENDING", "PENDING_PROVISION", "ACTIVE")

    try:
        blocked_resp = await admin_client.delete(f"/inventory/devices/{fresh_device['id']}")
        assert blocked_resp.status_code == 409, blocked_resp.text
        detail = blocked_resp.json()["detail"]
        assert detail["error"] == "device_in_use"
        assert res_id in detail["reservation_ids"]
        # Booked member, not a transit-only holder (issue #900 additive key).
        assert detail["transit_reservation_ids"] == []

        # The device was never touched: it still resolves.
        get_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
        assert get_resp.status_code == 200
    finally:
        cancel_resp = await admin_client.delete(f"/reservations/{res_id}")
        assert cancel_resp.status_code == 204

    delete_resp = await admin_client.delete(f"/inventory/devices/{fresh_device['id']}")
    assert delete_resp.status_code == 204

    gone_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
    assert gone_resp.status_code == 404


async def test_delete_succeeds_for_unreserved_device(admin_client, fresh_device):
    """An AVAILABLE device with no reservations deletes normally (the
    pre-#391 happy path stays unchanged)."""
    delete_resp = await admin_client.delete(f"/inventory/devices/{fresh_device['id']}")
    assert delete_resp.status_code == 204

    gone_resp = await admin_client.get(f"/inventory/devices/{fresh_device['id']}")
    assert gone_resp.status_code == 404


# --- Issue #900: transit hop guard ---------------------------------------------------

_MOCK_L1_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l1"


def _mock_l1_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L1_DIR / name, arcname=name)
    return buf.getvalue()


@pytest.fixture(scope="module")
async def dg_l1_template(base_url, admin_token):
    """A mock L1 switch driver plus template, torn down at module end."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l1-dg-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 1 Switch",
            "description": "device delete guard transit integration mock L1 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        payload = {
            "name": f"mock-l1-dg-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL1Switch",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        }
        tresp = await client.post("/inventory/templates", json=payload)
        tresp.raise_for_status()
        template = tresp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")
        await client.delete(f"/inventory/drivers/{driver['id']}")


async def _poll_active(client, reservation_id: str, *, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _fork_touches(client, reservation_id: str, device_id: str, *, timeout: float = 15.0):
    """Poll the user-facing fork until a hop names `device_id`; True when it does."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}/fork")
        if resp.status_code == 200:
            for conn in resp.json().get("connections", []):
                if device_id in (conn["device_a_id"], conn["device_b_id"]):
                    return True
        await asyncio.sleep(0.5)
    return False


async def _delete_when_released(client, device_id: str, *, timeout: float = 20.0):
    """DELETE the device, retrying while the guard still refuses (the fork archive
    after a cancel can trail the request by a moment; see the guard's docstring)."""
    deadline = asyncio.get_event_loop().time() + timeout
    resp = await client.delete(f"/inventory/devices/{device_id}")
    while resp.status_code == 409 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        resp = await client.delete(f"/inventory/devices/{device_id}")
    return resp


async def test_transit_switch_on_live_fork_blocks_delete_until_cancel(
    admin_client, dg_l1_template, fresh_devices
):
    """Cable A to S to B, reserve A and B with a topology edge A to B so the fork
    records hops through S, reach ACTIVE. S is in no reservation_devices row, yet
    DELETE S must 409 with the reservation id in BOTH lists (it holds S only as a
    transit hop). After the reservation is cancelled the delete goes through."""
    switch_resp = await admin_client.post(
        "/inventory/devices",
        json={
            "name": f"mock-l1-dg-sw-{uuid.uuid4().hex[:8]}",
            "template_id": dg_l1_template["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    switch_resp.raise_for_status()
    switch = switch_resp.json()
    dut_a, dut_b = await fresh_devices(2)
    connections: list[dict] = []
    reservation_id = None
    topology_id = None
    switch_deleted = False
    try:
        for dut, port in ((dut_a, "p1"), (dut_b, "p2")):
            cresp = await admin_client.post(
                "/cabling/connections",
                json={
                    "device_a_id": dut["id"],
                    "port_a": "eth0",
                    "device_b_id": switch["id"],
                    "port_b": port,
                    "connection_type": "L1",
                },
            )
            assert cresp.status_code in (200, 201), cresp.text
            connections.append(cresp.json())

        canvas = {
            "nodes": [
                {"id": "nA", "data": {"device": {"id": dut_a["id"]}}},
                {"id": "nB", "data": {"device": {"id": dut_b["id"]}}},
            ],
            "edges": [
                {
                    "id": "e1",
                    "source": "nA",
                    "target": "nB",
                    "data": {"layer": "L1", "isProposal": False},
                }
            ],
        }
        topo = await admin_client.post(
            "/cabling/topologies", json={"name": f"int-dg-{uuid.uuid4().hex[:8]}"}
        )
        topo.raise_for_status()
        topology_id = topo.json()["id"]
        put = await admin_client.put(
            f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas}
        )
        put.raise_for_status()

        now = datetime.now(timezone.utc)
        res = await admin_client.post(
            "/reservations/",
            json={
                "device_ids": [dut_a["id"], dut_b["id"]],
                "topology_id": topology_id,
                "purpose": "issue 900 transit delete guard integration test",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
        )
        assert res.status_code == 201, res.text
        reservation_id = res.json()["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"
        # Precondition: the fork really recorded a hop through the switch, and the
        # switch is not a booked member (the old guard could not see it).
        assert await _fork_touches(admin_client, reservation_id, switch["id"]), (
            "the fork never recorded a hop through the transit switch"
        )
        booked = await admin_client.get(f"/reservations/{reservation_id}")
        assert switch["id"] not in [str(d) for d in booked.json().get("device_ids", [])]

        blocked = await admin_client.delete(f"/inventory/devices/{switch['id']}")
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json()["detail"]
        assert detail["error"] == "device_in_use"
        assert reservation_id in detail["reservation_ids"]
        assert reservation_id in detail["transit_reservation_ids"]

        # The delete never ran: the switch still resolves.
        still = await admin_client.get(f"/inventory/devices/{switch['id']}")
        assert still.status_code == 200

        # Cancel; the fork archives and the guard lets the delete through.
        cancel = await admin_client.delete(f"/reservations/{reservation_id}")
        assert cancel.status_code == 204
        reservation_id = None
        # Remove the cables before the switch, so no Connection row is left naming a
        # deleted device (the guard under test is about fork hops, not plain cables).
        while connections:
            conn = connections.pop()
            gone_conn = await admin_client.delete(f"/cabling/connections/{conn['id']}")
            assert gone_conn.status_code in (200, 204), gone_conn.text
        deleted = await _delete_when_released(admin_client, switch["id"])
        assert deleted.status_code == 204, deleted.text
        switch_deleted = True
        gone = await admin_client.get(f"/inventory/devices/{switch['id']}")
        assert gone.status_code == 404
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        if not switch_deleted:
            await _delete_when_released(admin_client, switch["id"])


# --- Issue #940: cabled, unreserved device ---------------------------------------


def _cable_body(a_id: str, b_id: str, port: str) -> dict:
    return {
        "device_a_id": a_id,
        "port_a": port,
        "device_b_id": b_id,
        "port_b": port,
        "connection_type": "L1",
    }


async def test_cabled_unreserved_device_blocks_delete_until_cables_removed(
    admin_client, fresh_devices
):
    """Two fresh devices cabled to each other, no reservation. Deleting either end
    is refused with device_cabled naming the true count and the (sorted) ids; the
    device still resolves; once the cables are gone the delete succeeds.

    The cable cleanup sits in a finally so a failed assertion (including an old
    stack answering 204 here) never leaves a Connection row behind."""
    dev_a, dev_b = await fresh_devices(2)
    connection_ids: list[str] = []
    try:
        for port in ("eth0", "eth1"):
            resp = await admin_client.post(
                "/cabling/connections", json=_cable_body(dev_a["id"], dev_b["id"], port)
            )
            assert resp.status_code == 201, resp.text
            connection_ids.append(resp.json()["id"])

        for device in (dev_a, dev_b):
            blocked = await admin_client.delete(f"/inventory/devices/{device['id']}")
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["detail"] == {
                "error": "device_cabled",
                "connection_count": 2,
                "connection_ids": sorted(connection_ids),
            }
            still = await admin_client.get(f"/inventory/devices/{device['id']}")
            assert still.status_code == 200

        while connection_ids:
            gone_conn = await admin_client.delete(f"/cabling/connections/{connection_ids[-1]}")
            assert gone_conn.status_code in (200, 204), gone_conn.text
            connection_ids.pop()

        deleted = await admin_client.delete(f"/inventory/devices/{dev_a['id']}")
        assert deleted.status_code == 204, deleted.text
        gone = await admin_client.get(f"/inventory/devices/{dev_a['id']}")
        assert gone.status_code == 404
    finally:
        for connection_id in connection_ids:
            await admin_client.delete(f"/cabling/connections/{connection_id}")
