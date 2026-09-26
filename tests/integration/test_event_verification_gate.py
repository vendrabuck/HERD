"""Integration test for the reservation-event corroboration gate (hardening).

execution's NATS consumer used to treat any event on herd.reservations.* as
authoritative: a forged reservation.cancelled for an ACTIVE reservation froze
its wiring and tore down its ledgers, no different from a genuine cancel. The
gate in nats_consumer._verify_reservation_event corroborates an event's claim
against reservations' own record (GET /internal/{id}) before any handler runs.

This is the inverted attack this file proves closed: book a real, wired
reservation so it is genuinely ACTIVE, publish a FORGED reservation.cancelled
for it directly onto the stream (bypassing reservations entirely, stamping a
fresh event_id and Nats-Msg-Id per the #611 redelivery-dedupe rule), and assert
that nothing happened: the reservation is still ACTIVE via the API, its wiring
is not frozen, and its L1 ledger rows are unchanged. Then cancel the SAME
reservation for real and assert the real path still tears the wiring down,
proving the gate does not break the legitimate path it sits in front of.
"""

import asyncio
import io
import json
import os
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nats
import pytest

pytestmark = pytest.mark.asyncio

NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
_DRIVERS_DIR = Path(__file__).resolve().parents[2] / "drivers"


def _mock_l1_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_DRIVERS_DIR / "mock_l1" / name, arcname=name)
    return buf.getvalue()


@pytest.fixture
async def gate_switch_template(admin_client):
    """One mock_l1 driver + template, function-scoped so this file stays
    independent of the other teardown suites' session-scoped fixtures."""
    files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
    data = {
        "name": f"mock_l1-gate-{uuid.uuid4().hex[:8]}",
        "connection_type": "Layer 1 Switch",
        "description": "event-verification-gate mock_l1 driver",
    }
    driver_resp = await admin_client.post("/inventory/drivers", files=files, data=data)
    driver_resp.raise_for_status()
    driver = driver_resp.json()

    template_resp = await admin_client.post(
        "/inventory/templates",
        json={
            "name": f"mock_l1-gate-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": driver["id"],
            "exclusive": True,
            "vendor": "IntegrationVendor",
            "model": "EventVerificationGate",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        },
    )
    template_resp.raise_for_status()
    template = template_resp.json()

    yield template

    await admin_client.delete(f"/inventory/templates/{template['id']}")
    await admin_client.delete(f"/inventory/drivers/{driver['id']}")


async def _create_switch(client, template_id: str, name: str) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "sw"},
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _connect(client, dut_id: str, dut_port: str, switch_id: str, switch_port: str) -> dict:
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": dut_id,
            "port_a": dut_port,
            "device_b_id": switch_id,
            "port_b": switch_port,
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _canvas(dut_a_id: str, dut_b_id: str) -> dict:
    """A single L1 edge between the two DUTs. cabling's fork resolver hop-pairs
    each switch's connections by edge_key and chain-walks the group into an L1
    cross-connect (dut_a's port to dut_b's port via the switch); a lone
    dut-to-switch cable with nothing on the switch's other side is a dead end
    with nothing to cross-connect, so this needs both DUTs, not one."""
    return {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": dut_a_id}}},
            {"id": "n2", "data": {"device": {"id": dut_b_id}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-gate-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_ids: list[str], topology_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": "event verification gate inverted-attack test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _poll(coro_factory, predicate, *, timeout: float = 30.0, interval: float = 0.5):
    deadline = asyncio.get_event_loop().time() + timeout
    result = await coro_factory()
    while asyncio.get_event_loop().time() < deadline:
        if predicate(result):
            return result
        await asyncio.sleep(interval)
        result = await coro_factory()
    return result


async def _publish_forged_cancelled(reservation_id: str, device_ids: list[str]) -> None:
    """Publish a forged reservation.cancelled straight onto the stream, bypassing
    reservations entirely. Stamps a fresh event_id and sets Nats-Msg-Id to it
    (issue #611): a raw host-side publish that reuses the same id across test
    runs would otherwise dedupe against a stale sequence on a NATS container
    that has since been recreated, since NATS carries no volume under `make up`
    (see docs/OPERATIONS.md, JetStream durability)."""
    event_id = str(uuid.uuid4())
    payload = {
        "event": "reservation.cancelled",
        "event_id": event_id,
        "reservation_id": reservation_id,
        "user_id": str(uuid.uuid4()),
        "device_ids": device_ids,
    }
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        await js.stream_info("HERD_RESERVATIONS")
        await js.publish(
            "herd.reservations.cancelled",
            json.dumps(payload).encode(),
            headers={"Nats-Msg-Id": event_id},
        )
    finally:
        await nc.close()


async def test_forged_cancelled_event_for_an_active_reservation_is_ignored(
    admin_client, gate_switch_template, fresh_devices
):
    """Book and wire a reservation for real, forge reservation.cancelled for it,
    and prove nothing happened; then cancel it for real and prove the real path
    still tears the wiring down."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_switch(admin_client, gate_switch_template["id"], f"gate-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    topology_id = None
    reservation = None
    try:
        connections.append(
            await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "ge-0/0/1")
        )
        connections.append(
            await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "ge-0/0/2")
        )
        topology_id = await _create_topology(admin_client, _canvas(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(
            admin_client, [dut_a["id"], dut_b["id"], switch["id"]], topology_id
        )
        res_id = reservation["id"]

        async def _get_res():
            resp = await admin_client.get(f"/reservations/{res_id}")
            resp.raise_for_status()
            return resp.json()

        async def _get_wiring():
            resp = await admin_client.get(f"/reservations/{res_id}/wiring-status")
            resp.raise_for_status()
            return resp.json()

        # Wait for real provisioning: an ACTIVE l1 row for this switch.
        wiring_before = await _poll(
            _get_wiring,
            lambda w: any(c["status"] == "ACTIVE" for c in w["connections"]),
        )
        active_before = [c for c in wiring_before["connections"] if c["status"] == "ACTIVE"]
        assert active_before, "provisioning never completed, cannot test the inverted attack"
        assert wiring_before["frozen"] is False

        # The attack: forge reservation.cancelled for this still-ACTIVE reservation,
        # published directly onto the stream, no reservations service involved.
        await _publish_forged_cancelled(res_id, [dut_a["id"], dut_b["id"], switch["id"]])

        # No ordering anchor is possible here: a correctly-behaving consumer
        # produces no observable effect from this event at all, so there is
        # nothing to poll on. Give it a fixed window to have processed the
        # forged event (well past one corroboration HTTP round trip) before
        # asserting on the absence of any effect.
        await asyncio.sleep(3.0)

        res_after_forgery = await _get_res()
        assert res_after_forgery["status"] == "ACTIVE", (
            "a forged reservation.cancelled for a still-ACTIVE reservation changed "
            "its status; the corroboration gate did not hold"
        )
        wiring_after_forgery = await _get_wiring()
        assert wiring_after_forgery["frozen"] is False, (
            "a forged reservation.cancelled froze wiring for a reservation "
            "reservations never actually cancelled"
        )
        active_after_forgery = [
            c for c in wiring_after_forgery["connections"] if c["status"] == "ACTIVE"
        ]
        assert {c["id"] for c in active_after_forgery} == {c["id"] for c in active_before}, (
            "a forged reservation.cancelled changed the L1 ledger rows"
        )

        # Now cancel it for real: the real path must still tear the wiring down.
        cancel_resp = await admin_client.delete(f"/reservations/{res_id}")
        cancel_resp.raise_for_status()

        # Terminal teardown freezes the wiring FIRST and only then drives the
        # release, so a poll that stops at frozen=True can observe rows still
        # ACTIVE for a moment (that is what CI saw on the first run). Poll for
        # the end state, freeze plus release, and assert both together.
        wiring_final = await _poll(
            _get_wiring,
            lambda w: (
                w["frozen"] is True and all(c["status"] != "ACTIVE" for c in w["connections"])
            ),
        )
        assert wiring_final["frozen"] is True, "a real cancel never froze the wiring"
        assert all(c["status"] != "ACTIVE" for c in wiring_final["connections"]), (
            "a real cancel did not release the applied L1 connection"
        )
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection in connections:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
