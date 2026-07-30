"""Integration tests for execution-consumer DLQ retention.

The execution service consumes herd.reservations.* from JetStream. A poison
message (undecodable JSON) must not wedge the consumer or be silently dropped:
the consumer routes it to herd.reservations.dlq.execution (a 4-token subject
bound only by the HERD_DLQ stream, deliberately outside the 3-token consumer
filter so it cannot be redelivered into a poison loop) and acks the original.
This test proves the poison bytes are actually retained on that DLQ subject,
the headline gap from the QA-sweep backlog (#214).

NATS is reached directly from the test host (NATS_URL_HOST), mirroring
test_health_alerting_flow.py; if the host cannot reach NATS the test skips.
"""

import asyncio
import io
import json
import os
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import nats
import pytest

pytestmark = pytest.mark.asyncio

NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
_RESERVATIONS_SUBJECT = "herd.reservations.created"
_EXECUTION_DLQ_SUBJECT = "herd.reservations.dlq.execution"
_MOCK_L2_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l2"


async def _publish_raw(payload: bytes, subject: str = _RESERVATIONS_SUBJECT) -> None:
    """Publish raw bytes to the reservations stream (bypassing the producer)."""
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Idempotent: the stream is created by the reservations/execution lifespan.
        await js.add_stream(name="HERD_RESERVATIONS", subjects=["herd.reservations.*"])
        await js.publish(subject, payload)
    finally:
        await nc.close()


async def _find_in_execution_dlq(marker: bytes, *, timeout: float = 15.0) -> bytes | None:
    """Poll the HERD_DLQ stream for a message on the execution DLQ subject whose
    body contains `marker`. Returns the message bytes, or None on timeout.

    Reading is non-destructive: an ephemeral pull consumer over a limits-retention
    stream leaves the messages in place.
    """
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        await js.add_stream(name="HERD_DLQ", subjects=["herd.*.dlq.>"])  # idempotent
        sub = await js.pull_subscribe(_EXECUTION_DLQ_SUBJECT, stream="HERD_DLQ")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await sub.fetch(100, timeout=2)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                msgs = []
            for m in msgs:
                await m.ack()
                if marker in m.data:
                    return m.data
            await asyncio.sleep(0.3)
        return None
    finally:
        await nc.close()


async def _fetch_reservation_event(
    reservation_id: str, event: str = "reservation.created", *, timeout: float = 30.0
) -> bytes | None:
    """Return the raw bytes of the `event` message for `reservation_id` from
    HERD_RESERVATIONS, or None on timeout.

    Reading is non-destructive (ephemeral pull consumer over a limits-retention
    stream). Used to capture a real producer-published event so the test can
    re-publish it and exercise the consumer's event_id idempotency guard.
    """
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        await js.add_stream(name="HERD_RESERVATIONS", subjects=["herd.reservations.*"])
        sub = await js.pull_subscribe("herd.reservations.*", stream="HERD_RESERVATIONS")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await sub.fetch(100, timeout=2)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                msgs = []
            for m in msgs:
                await m.ack()
                try:
                    body = json.loads(m.data)
                except Exception:  # noqa: BLE001 - skip non-JSON
                    continue
                if body.get("event") == event and body.get("reservation_id") == reservation_id:
                    return m.data
            await asyncio.sleep(0.3)
        return None
    finally:
        await nc.close()


async def test_poison_reservation_event_is_retained_in_dlq():
    """An undecodable reservation event lands on herd.reservations.dlq.execution
    in the HERD_DLQ stream with its original bytes preserved (not void-dropped)."""
    marker = uuid.uuid4().hex.encode()
    poison = b"POISON-" + marker + b" {not valid json"

    try:
        await _publish_raw(poison)
    except Exception as exc:  # noqa: BLE001 - host may not reach NATS in some envs
        pytest.skip(f"NATS unreachable from test host: {exc}")

    retained = await _find_in_execution_dlq(marker)
    assert retained is not None, (
        "poison message was not retained on herd.reservations.dlq.execution; "
        "the execution consumer either dropped it or did not route it to the DLQ"
    )
    # The DLQ preserves the original payload verbatim for inspection/replay.
    assert retained == poison


# --- Redelivery idempotency -------------------------------------------------
#
# A raised/failed driver action is ACKed (the consumer records FAILED and
# continues), not NAK'd, and the pull consumers do not redeliver on a late ack,
# so neither a driver failure nor a slow handler forces a redelivery of an
# already-succeeded op. The faithful way to exercise the guard is the outbox's
# own at-least-once path: re-publish the exact event the producer emitted (same
# payload, a NEW stream sequence, as a relay republish after a
# dedup-window-expired outage would). As of ADR 0009 phase 7 all wiring rides
# reservation.wiring_changed (activation stages the fork's initial version), and
# the consumer's replay guard for it is the per-reservation
# last_applied_fork_version marker: a re-seen version is a stale no-op before
# any driver call, so nothing (not even login) re-runs.


def _mock_l2_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L2_DIR / name, arcname=name)
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


@pytest.fixture(scope="session")
async def slow_l2_driver(base_url, admin_token):
    """Upload the mock L2 driver once per session for the redelivery test."""
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l2.tar.gz", _mock_l2_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l2-slow-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 2 Switch",
            "description": "integration mock L2 switch driver (redelivery test)",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def slow_l2_template(base_url, admin_token, slow_l2_driver):
    """An L2 template that declares the mock_sleep_ms injection field, so a switch
    device can carry a per-action driver sleep in its field_data."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l2-slow-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": slow_l2_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL2SwitchSlow",
            "sections": [
                {
                    "name": "General",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string"},
                        {"key": "mock_sleep_ms", "label": "Mock sleep ms", "type": "string"},
                        {
                            "key": "mock_raise_actions",
                            "label": "Mock raise actions",
                            "type": "string",
                        },
                    ],
                }
            ],
        }
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


async def _create_switch(client, template_id, name, sleep_ms, raise_actions=""):
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {
                "model": "sw",
                "mock_sleep_ms": str(sleep_ms),
                "mock_raise_actions": raise_actions,
            },
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _connect(client, dut_id, switch_id, switch_port):
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": dut_id,
            "port_a": "eth0",
            "device_b_id": switch_id,
            "port_b": switch_port,
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _canvas_edge(a_id, b_id):
    """A committed one-edge canvas wiring device a to device b (React Flow shape)."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": a_id}}},
            {"id": "nB", "data": {"device": {"id": b_id}}},
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


async def _create_topology(client, canvas):
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-dlq-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_id, topology_id):
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": [device_id],
            "topology_id": topology_id,
            "purpose": "redelivery idempotency test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _runs(client, reservation_id, action, status=None):
    params = {"reservation_id": reservation_id, "limit": 200}
    if status:
        params["status"] = status
    resp = await client.get("/execution/runs", params=params)
    resp.raise_for_status()
    return [r for r in resp.json().get("items", []) if r["action"] == action]


@pytest.mark.timeout(120)
async def test_redelivery_does_not_rerun_succeeded_create_vlan(
    admin_client, slow_l2_template, fresh_devices
):
    """A re-published wiring_changed (same fork_version, new stream sequence) must
    not re-run an already-succeeded create_vlan.

    Provision normally through the activation-staged wiring_changed (ADR 0009
    phase 7: the reservation books a wired parent topology) so create_vlan commits
    SUCCESS, capture the exact reservation.wiring_changed event the producer
    emitted, then re-publish it verbatim. JetStream assigns a new sequence, so the
    consumer sees a fresh message, but its replay guard is the per-reservation
    last_applied_fork_version marker: the re-seen version is a stale no-op before
    any driver call. Delivery of the re-published event is proven by an ordering
    anchor (the consumer processes the stream sequentially): a SECOND wired
    reservation booked after the re-publish provisions, so once its create_vlan
    succeeds the re-published event was consumed; the first reservation's
    create_vlan and login counts must be unchanged.
    """
    try:
        _probe = await nats.connect(NATS_URL_HOST, connect_timeout=5)
        await _probe.close()
    except Exception as exc:  # noqa: BLE001 - host may not reach NATS in some envs
        pytest.skip(f"NATS unreachable from test host: {exc}")

    suffix = uuid.uuid4().hex[:8]
    switch = await _create_switch(
        admin_client, slow_l2_template["id"], f"mock-l2-slow-{suffix}", sleep_ms=0
    )
    dut_a, dut_b = await fresh_devices(2)
    reservations = []
    connections = []
    topology_ids = []
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "eth1"))
        topo_a = await _create_topology(admin_client, _canvas_edge(dut_a["id"], switch["id"]))
        topology_ids.append(topo_a)
        res_a = await _reserve(admin_client, dut_a["id"], topo_a)
        reservations.append(res_a)

        # 1. First delivery provisions normally: wait for create_vlan SUCCESS.
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            if await _runs(admin_client, res_a["id"], "create_vlan", status="SUCCESS"):
                break
            await asyncio.sleep(1.0)
        first = await _runs(admin_client, res_a["id"], "create_vlan", status="SUCCESS")
        assert len(first) == 1, f"create_vlan did not succeed once on first delivery: {len(first)}"
        login_before = len(await _runs(admin_client, res_a["id"], "login"))

        # 2. Capture the producer's reservation.wiring_changed event and re-publish
        #    it verbatim. Same payload (same fork_version), new JetStream sequence.
        raw = await _fetch_reservation_event(res_a["id"], event="reservation.wiring_changed")
        assert raw is not None, (
            "could not capture the reservation.wiring_changed event from the stream"
        )
        await _publish_raw(raw, subject="herd.reservations.wiring_changed")

        # 3. Ordering anchor: a second wired reservation booked AFTER the re-publish.
        #    Its activation-staged wiring_changed sits behind the re-published event
        #    on the stream, so its create_vlan SUCCESS proves the re-publish was
        #    consumed.
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "eth2"))
        topo_b = await _create_topology(admin_client, _canvas_edge(dut_b["id"], switch["id"]))
        topology_ids.append(topo_b)
        res_b = await _reserve(admin_client, dut_b["id"], topo_b)
        reservations.append(res_b)
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            if await _runs(admin_client, res_b["id"], "create_vlan", status="SUCCESS"):
                break
            await asyncio.sleep(1.0)
        assert await _runs(admin_client, res_b["id"], "create_vlan", status="SUCCESS"), (
            "anchor reservation never provisioned; cannot prove the re-publish was consumed"
        )

        # 4. The stale-version guard held: nothing re-ran for the first reservation,
        #    not even login (the no-op happens before any driver call).
        create_success = await _runs(admin_client, res_a["id"], "create_vlan", status="SUCCESS")
        assert len(create_success) == 1, (
            f"create_vlan succeeded {len(create_success)} times across a re-publish; "
            "the stale-version guard failed to skip the already-applied version"
        )
        assert len(await _runs(admin_client, res_a["id"], "login")) == login_before, (
            "a re-published wiring_changed must no-op before any driver call"
        )
    finally:
        for reservation in reservations:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        for topology_id in topology_ids:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection in connections:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
