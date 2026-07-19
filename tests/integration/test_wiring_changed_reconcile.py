"""Integration coverage for the connection-driven L1 reconcile (ADR 0007, #345 P3b phase 3).

End-to-end over a running HERD stack with the checked-in mock L1 driver:

- The save flow: reserve and activate two DUTs cabled through an L1 switch (a
  connect_ports fires on activation), then save an emptied fork canvas. The save
  stages reservation.wiring_changed; the execution consumer reconciles the fork's
  now-empty intended set against the ACTIVE cross-connect and drives a
  disconnect_ports on the switch. Asserted through GET /execution/runs, the
  observable proxy for the l1_connection_assignments flip (the per-connection
  wiring-status endpoint is phase 4).
- The frozen guard (Decision 7): after the reservation completes (teardown freezes
  the wiring state), a directly-injected stale wiring_changed must not reconnect.
- Replay idempotency (Decision 4): a stale wiring_changed (fork_version at or below
  last-applied) injected after a save is a no-op, driving no additional switch op.
- The phase-4 per-connection surface (Decision 6): a fork-save build forced to fail via
  HERD_mock_fail_actions lands a FAILED row observable through the NEW user-facing
  GET /reservations/{id}/wiring-status; clearing the knob and hitting
  POST /reservations/{id}/wiring/retry flips that connection ACTIVE. This closes the
  ADR's integration test-plan item on the manual retry path.

The phase-1/3 tests assert the driver's observable execution runs (the wiring-status
endpoint did not exist before phase 4); the phase-4 test asserts the assignment-row
flip directly through the new endpoint. They require a running stack and NATS reachable
from the host; without either they skip or error at connect time, which is expected.
Live-gated at review.
"""

import asyncio
import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ._nats_helpers import probe_nats, publish_raw

pytestmark = pytest.mark.asyncio

_MOCK_L1_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l1"
_WIRING_CHANGED_SUBJECT = "herd.reservations.wiring_changed"


def _mock_l1_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L1_DIR / name, arcname=name)
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


@pytest.fixture(scope="session")
async def l1_driver(base_url, admin_token):
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l1-wc-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 1 Switch",
            "description": "wiring_changed integration mock L1 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l1_template(base_url, admin_token, l1_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l1-wc-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": l1_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL1Switch",
            "sections": [
                {
                    "name": "General",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string"},
                        {
                            "key": "mock_fail_actions",
                            "label": "Mock fail actions",
                            "type": "string",
                        },
                        {
                            "key": "mock_sleep_ms",
                            "label": "Mock per-call sleep ms",
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


async def _create_switch(client, template_id: str) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": f"mock-l1-sw-{uuid.uuid4().hex[:8]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _connect(client, dut_id: str, switch_id: str, switch_port: str) -> dict:
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


def _canvas_edge(a_id: str, b_id: str) -> dict:
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


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post("/cabling/topologies", json={"name": f"int-wc-{uuid.uuid4().hex[:8]}"})
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_ids: list[str], topology_id: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "wiring_changed integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if topology_id is not None:
        body["topology_id"] = topology_id
    resp = await client.post("/reservations/", json=body)
    resp.raise_for_status()
    return resp.json()


async def _count_success_runs(client, reservation_id: str, action: str) -> int:
    resp = await client.get(
        "/execution/runs",
        params={"reservation_id": reservation_id, "status": "SUCCESS", "limit": 200},
    )
    if resp.status_code != 200:
        return 0
    return len([r for r in resp.json().get("items", []) if r["action"] == action])


async def _poll_run_count_at_least(
    client, reservation_id: str, action: str, target: int, *, timeout: float = 25.0
) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    count = 0
    while asyncio.get_event_loop().time() < deadline:
        count = await _count_success_runs(client, reservation_id, action)
        if count >= target:
            return count
        await asyncio.sleep(0.5)
    return count


async def _create_switch_fd(client, template_id: str, field_data: dict) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": f"mock-l1-sw-{uuid.uuid4().hex[:8]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": field_data,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _wiring_status(client, reservation_id: str) -> dict:
    resp = await client.get(f"/reservations/{reservation_id}/wiring-status")
    resp.raise_for_status()
    return resp.json()


async def _poll_wiring_conn(client, reservation_id: str, predicate, *, timeout: float = 15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await _wiring_status(client, reservation_id)
        for conn in status.get("connections", []):
            if predicate(conn):
                return conn
        await asyncio.sleep(0.5)
    return None


async def _poll_active(client, reservation_id: str, *, timeout: float = 12.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def test_failed_build_surfaces_and_manual_retry_recovers(
    admin_client, l1_template, fresh_devices
):
    """A fork-save build forced to fail lands a FAILED row on the phase-4 wiring-status
    surface; clearing the failure knob and hitting the manual retry endpoint flips that
    connection ACTIVE (ADR 0007 Decision 6, the manual-retry integration test-plan item).

    The switch starts with HERD_mock_fail_actions=connect_ports and the reservation
    activates against an EMPTY fork canvas (so activation drives no connect and the knob
    is inert there). Saving a canvas that wires the two DUTs through the switch makes the
    reconcile BUILD the switch cross-connect, which fails on connect_ports and parks a
    FAILED row. The knob is then cleared and the retry reattempts the same pair."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(f"NATS not reachable from host: {nats_err}")

    switch = await _create_switch_fd(
        admin_client,
        l1_template["id"],
        {"model": "test", "mock_fail_actions": "connect_ports"},
    )
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        # Activate against an empty canvas: no connect fires, so the fail knob is inert
        # at activation and only bites the later save-driven build.
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"

        # Save the wired canvas: the reconcile builds the switch pair, connect_ports fails.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": _canvas_edge(dut_a["id"], dut_b["id"])},
        )
        assert saved.status_code == 200, saved.text

        # The FAILED build surfaces through the NEW user-facing wiring-status endpoint.
        failed = await _poll_wiring_conn(
            admin_client, reservation_id, lambda c: c["status"] == "FAILED"
        )
        assert failed is not None, "the failed build never surfaced as a FAILED wiring-status row"
        assert failed["retryable"] is True, "a driver-failure FAILED row must be retryable"

        # Clear the failure knob so the reattempt can succeed, then manual-retry.
        upd = await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": ""}},
        )
        assert upd.status_code == 200, upd.text

        retried = await admin_client.post(f"/reservations/{reservation_id}/wiring/retry")
        assert retried.status_code == 200, retried.text
        outcomes = retried.json()["results"]
        assert any(o["outcome"] == "reconnected" for o in outcomes), (
            f"manual retry did not reconnect the pair: {outcomes}"
        )

        # wiring-status now shows the pair ACTIVE with no lingering FAILED row.
        active = await _poll_wiring_conn(
            admin_client, reservation_id, lambda c: c["status"] == "ACTIVE"
        )
        assert active is not None, "the retried connection never reached ACTIVE"
        remaining = [
            c
            for c in (await _wiring_status(admin_client, reservation_id))["connections"]
            if c["status"] == "FAILED"
        ]
        assert remaining == [], "no FAILED row should remain after a successful retry"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_stale_wiring_writer_does_not_clobber_concurrent_winner(
    admin_client, l1_template, fresh_devices
):
    """Issue #412: concurrent wiring writers must converge on the winner's ACTIVE row.

    Scope honesty: the write-level race itself (record_l1_failed called with an
    ACTIVE row) is pinned DETERMINISTICALLY by the unit test
    test_failed_record_never_downgrades_active_row; forcing the exact clobber
    interleaving through the black-box API proved unreliable (the system has
    four concurrent wiring writers and the is_pair_active skip absorbs most
    orderings). What THIS test pins live is the convergence contract under real
    overlapping writers:

    1. A fast save with the fail knob armed parks the FAILED row and lets the
       apply complete (version stamped, nothing in flight).
    2. The knobs are re-armed as fail plus HERD_mock_sleep_ms, and retry-1 is
       fired WITHOUT awaiting it: its three in-line connect attempts each
       sleep, so it is a failing writer in flight for roughly fifteen seconds,
       holding the device context it fetched at start (knob armed).
    3. Mid-window the knobs are cleared and retry-2 runs to completion: fresh
       context, instant success, row ACTIVE (the winner).
    4. retry-1 finishes; whatever it records, the end state must be the
       winner's: row ACTIVE, no FAILED residue, attempts not inflated. The
       issue #412 guard is one of the mechanisms this contract rests on.

    Vacuity canary: retry-2 must complete while retry-1 is still in flight; if
    retry-1 ever finishes first the interleaving collapsed and the canary
    fails loudly instead of letting the test pass without exercising the race.
    """
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(f"NATS not reachable from host: {nats_err}")

    switch = await _create_switch_fd(
        admin_client,
        l1_template["id"],
        {"model": "test", "mock_fail_actions": "connect_ports"},
    )
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"

        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": _canvas_edge(dut_a["id"], dut_b["id"])},
        )
        assert saved.status_code == 200, saved.text
        saved_version = saved.json()["version_number"]

        # Step 1: the fast failing apply completes fully: FAILED row parked,
        # version stamped, no writer in flight.
        failed = await _poll_wiring_conn(
            admin_client, reservation_id, lambda c: c["status"] == "FAILED", timeout=30.0
        )
        assert failed is not None, "the failed build never surfaced a FAILED row"
        stamped = await _poll_wiring_version(admin_client, reservation_id, saved_version)
        assert stamped, "the save-driven apply never stamped its version"

        # Step 2: re-arm as a SLOW failing writer and launch retry-1 unawaited.
        upd = await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={
                "field_data": {
                    "model": "test",
                    "mock_fail_actions": "connect_ports",
                    "mock_sleep_ms": "4000",
                }
            },
        )
        assert upd.status_code == 200, upd.text
        stale_writer = asyncio.create_task(
            admin_client.post(f"/reservations/{reservation_id}/wiring/retry")
        )
        # Let retry-1 fetch its (armed) context and enter its first sleeping
        # connect attempt before the knobs change under it.
        await asyncio.sleep(2.0)

        # Step 3: clear the knobs and run the winner to completion mid-window.
        upd = await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test"}},
        )
        assert upd.status_code == 200, upd.text
        winner_resp = await admin_client.post(f"/reservations/{reservation_id}/wiring/retry")
        assert winner_resp.status_code == 200, winner_resp.text
        assert not stale_writer.done(), (
            "retry-1 finished before the winner ran; the issue #412 interleaving "
            "collapsed and this test is not exercising the race"
        )
        won = await _poll_wiring_conn(
            admin_client, reservation_id, lambda c: c["status"] == "ACTIVE", timeout=10.0
        )
        assert won is not None, "the winning retry never flipped the row ACTIVE"
        attempts_after_win = won["attempts"]

        # Step 4: the stale writer completes; its failure must be ignored.
        stale_resp = await stale_writer
        assert stale_resp.status_code == 200, stale_resp.text

        final = await _wiring_status(admin_client, reservation_id)
        rows = final["connections"]
        assert rows and all(c["status"] != "FAILED" for c in rows), (
            f"stale writer clobbered the winner's ACTIVE row (issue #412): {rows}"
        )
        survivor = next(c for c in rows if c["status"] == "ACTIVE")
        assert survivor["attempts"] == attempts_after_win, (
            "stale writer inflated attempts on the ACTIVE row"
        )
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def _poll_wiring_version(client, reservation_id: str, version: int, timeout: float = 30.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await _wiring_status(client, reservation_id)
        if status.get("last_applied_fork_version") == version:
            return True
        await asyncio.sleep(0.5)
    return False


async def test_fork_save_release_drives_disconnect(admin_client, l1_template, fresh_devices):
    """Emptying and saving the fork releases the ACTIVE cross-connect: the consumer
    reconciles the now-empty intended set and drives disconnect_ports on the switch."""
    switch = await _create_switch(admin_client, l1_template["id"])
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]

        # Activation drives the initial connect_ports.
        assert await _poll_run_count_at_least(admin_client, reservation_id, "connect_ports", 1), (
            "activation never connected the ports"
        )

        # Save an emptied fork canvas: cabling releases the wire and stages
        # reservation.wiring_changed; the consumer reconciles to an empty set.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )
        assert saved.status_code == 200, saved.text

        # The reconcile drives a disconnect_ports on the switch for the freed pair.
        disconnects = await _poll_run_count_at_least(
            admin_client, reservation_id, "disconnect_ports", 1
        )
        assert disconnects >= 1, "wiring_changed reconcile did not disconnect the released pair"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_wiring_changed_frozen_after_complete_no_reconnect(
    admin_client, l1_template, fresh_devices
):
    """After the reservation completes (teardown freezes the wiring state), an injected
    stale wiring_changed does not reconnect: the frozen guard is a no-op before any
    driver call (ADR 0007 Decision 7)."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(f"NATS not reachable from host: {nats_err}")

    switch = await _create_switch(admin_client, l1_template["id"])
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]])
        reservation_id = reservation["id"]

        assert await _poll_run_count_at_least(admin_client, reservation_id, "connect_ports", 1)

        # Complete the reservation: teardown disconnects and freezes the wiring state.
        released = await admin_client.put(f"/reservations/{reservation_id}/release")
        assert released.status_code == 200, released.text
        await _poll_run_count_at_least(admin_client, reservation_id, "disconnect_ports", 1)

        connect_before = await _count_success_runs(admin_client, reservation_id, "connect_ports")

        # Inject a stale wiring_changed asking to rebuild the pair. Frozen => no-op.
        payload = {
            "event": "reservation.wiring_changed",
            "reservation_id": reservation_id,
            "fork_version": 99,
            "released": [],
            "built": [
                {
                    "device_a_id": dut_a["id"],
                    "port_a": "eth0",
                    "device_b_id": switch["id"],
                    "port_b": "p1",
                    "layer": "L1",
                    "physical_connection_id": None,
                },
                {
                    "device_a_id": switch["id"],
                    "port_a": "p2",
                    "device_b_id": dut_b["id"],
                    "port_b": "eth0",
                    "layer": "L1",
                    "physical_connection_id": None,
                },
            ],
            "event_id": str(uuid.uuid4()),
        }
        await publish_raw(_WIRING_CHANGED_SUBJECT, json.dumps(payload).encode())

        # Give the consumer time to (not) act, then assert no new connect fired.
        await asyncio.sleep(6)
        connect_after = await _count_success_runs(admin_client, reservation_id, "connect_ports")
        assert connect_after == connect_before, "frozen reservation must not reconnect"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_wiring_changed_stale_replay_no_double_apply(
    admin_client, l1_template, fresh_devices
):
    """A stale wiring_changed (fork_version at or below last-applied) injected after a
    save reconcile is a no-op: no additional switch op fires (ADR 0007 Decision 4)."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(f"NATS not reachable from host: {nats_err}")

    switch = await _create_switch(admin_client, l1_template["id"])
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]

        assert await _poll_run_count_at_least(admin_client, reservation_id, "connect_ports", 1)

        # Save an emptied canvas to release the pair and advance last-applied.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )
        assert saved.status_code == 200, saved.text
        await _poll_run_count_at_least(admin_client, reservation_id, "disconnect_ports", 1)

        disconnect_before = await _count_success_runs(
            admin_client, reservation_id, "disconnect_ports"
        )

        # Inject a stale wiring_changed (version 1, below the applied version). The
        # last-applied guard skips it: no re-disconnect and no reconnect.
        payload = {
            "event": "reservation.wiring_changed",
            "reservation_id": reservation_id,
            "fork_version": 1,
            "released": [],
            "built": [],
            "event_id": str(uuid.uuid4()),
        }
        await publish_raw(_WIRING_CHANGED_SUBJECT, json.dumps(payload).encode())

        await asyncio.sleep(6)
        disconnect_after = await _count_success_runs(
            admin_client, reservation_id, "disconnect_ports"
        )
        connect_after = await _count_success_runs(admin_client, reservation_id, "connect_ports")
        assert disconnect_after == disconnect_before, "stale replay must not re-disconnect"
        # Exactly the one activation connect; the stale event drives no rebuild.
        assert connect_after == 1, "stale replay must not reconnect"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
