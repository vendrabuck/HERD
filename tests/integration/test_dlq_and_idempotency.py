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


async def _publish_raw(payload: bytes) -> None:
    """Publish raw bytes to the reservations stream (bypassing the producer)."""
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Idempotent: the stream is created by the reservations/execution lifespan.
        await js.add_stream(name="HERD_RESERVATIONS", subjects=["herd.reservations.*"])
        await js.publish(_RESERVATIONS_SUBJECT, payload)
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
# own at-least-once path: re-publish the exact reservation.created event the
# producer emitted (same payload event_id, a NEW stream sequence, as a relay
# republish after a dedup-window-expired outage would). The consumer keys its
# idempotency on the payload event_id (issue #21), so the re-seen event skips the
# already-succeeded create_vlan while the non-guarded login re-runs.


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


async def _reserve(client, device_id):
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": [device_id],
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
    admin_client, slow_l2_template, fresh_device
):
    """A re-published event (same event_id, new stream sequence) must not re-run
    an already-succeeded create_vlan.

    Provision normally so create_vlan commits SUCCESS, capture the exact
    reservation.created event the producer emitted, then re-publish it verbatim.
    JetStream assigns a new sequence, so the consumer sees a fresh message, but it
    keys idempotency on the payload event_id, so it skips the already-succeeded
    create_vlan while the non-guarded login re-runs. Asserts login ran >=2 times
    (the re-published event was delivered and processed) while create_vlan
    succeeded exactly once (the event_id guard held across the resequencing).
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
    reservation = None
    connection = None
    try:
        connection = await _connect(admin_client, fresh_device["id"], switch["id"], "eth1")
        reservation = await _reserve(admin_client, fresh_device["id"])

        # 1. First delivery provisions normally: wait for create_vlan SUCCESS.
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            if await _runs(admin_client, reservation["id"], "create_vlan", status="SUCCESS"):
                break
            await asyncio.sleep(1.0)
        first = await _runs(admin_client, reservation["id"], "create_vlan", status="SUCCESS")
        assert len(first) == 1, f"create_vlan did not succeed once on first delivery: {len(first)}"
        login_before = len(await _runs(admin_client, reservation["id"], "login"))

        # 2. Capture the producer's reservation.created event and re-publish it
        #    verbatim. Same payload event_id, new JetStream sequence.
        raw = await _fetch_reservation_event(reservation["id"])
        assert raw is not None, "could not capture the reservation.created event from the stream"
        await _publish_raw(raw)

        # 3. The re-published event is delivered (login re-runs) but create_vlan is
        #    skipped by the event_id guard.
        deadline = asyncio.get_event_loop().time() + 60
        login_after = login_before
        while asyncio.get_event_loop().time() < deadline:
            login_after = len(await _runs(admin_client, reservation["id"], "login"))
            if login_after > login_before:
                break
            await asyncio.sleep(1.0)
        assert login_after > login_before, (
            f"the re-published event was never processed (login stayed at {login_before}); "
            "delivery did not happen"
        )
        # Settle, then assert the guard held: still exactly one create_vlan SUCCESS.
        await asyncio.sleep(3.0)
        create_success = await _runs(
            admin_client, reservation["id"], "create_vlan", status="SUCCESS"
        )
        assert len(create_success) == 1, (
            f"create_vlan succeeded {len(create_success)} times across a re-publish; "
            "the event_id guard failed to skip the already-applied op"
        )
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
