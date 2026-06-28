"""LIVE integration: the transactional outbox (issue #21) delivers reservation
events durably, including across a full NATS outage.

The reservations service stages its lifecycle event into an `outbox` table in
the SAME transaction as the state change, and a background relay (5s tick by
default) publishes unpublished rows to JetStream (subject `herd.reservations.*`,
with a `Nats-Msg-Id` header and an `event_id` in the payload). The observable
end-to-end signal that an event was delivered is the in-app notification row the
notifications consumer creates: reservations producer to outbox to relay to NATS
to notifications consumer to notification row.

The headline property: even if NATS is down when the reservation is created, the
event is not lost. The producer no longer depends on a live publish, so the
state change still commits, and the relay drains the buffered row once NATS is
back. Delivery is at-least-once, but the notifications consumer dedupes on the
stable `event_id`, so exactly one notification row results.

Assumes a running HERD stack (make up) with NATS, notifications, reservations,
and inventory reachable through Traefik, and a host that can both reach NATS
(NATS_URL_HOST, default nats://localhost:4222) and drive `docker compose` for
the ephemeral stack. Tests that cannot satisfy a precondition skip rather than
fail.
"""

import asyncio
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import nats
import pytest

pytestmark = pytest.mark.asyncio

# The ephemeral stack is booted from the repo root with `docker compose up` (no
# -f), so the override applies and the compose project resolves from this dir.
REPO_ROOT = Path(__file__).resolve().parents[2]
NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")

_EVENT_TYPE = "reservation.created"


# --- docker compose control (resilient: never cascade-fail) ------------------


def _run_compose(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess | None:
    """Run `docker compose <args>` from the repo root.

    Returns the CompletedProcess, or None if docker compose cannot be invoked at
    all on this host (binary missing, or the call timed out). Callers treat None
    or a non-zero return code as a reason to skip, not to fail.
    """
    try:
        return subprocess.run(
            ["docker", "compose", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


async def _compose(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess | None:
    """Async wrapper so a blocking docker call does not stall the event loop."""
    return await asyncio.to_thread(_run_compose, *args, timeout=timeout)


# --- NATS reachability probes ------------------------------------------------


async def _nats_reachable(timeout: float = 3.0) -> bool:
    """True if a TCP+protocol connection to NATS succeeds from the test host."""
    try:
        nc = await nats.connect(NATS_URL_HOST, connect_timeout=timeout)
    except Exception:  # noqa: BLE001 - any failure means "not reachable right now"
        return False
    await nc.close()
    return True


async def _wait_nats(up: bool, *, timeout: float = 60.0, interval: float = 1.0) -> bool:
    """Poll until NATS reachability equals `up`, bounded by `timeout`."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await _nats_reachable() == up:
            return True
        await asyncio.sleep(interval)
    return await _nats_reachable() == up


# --- reservation + notification helpers --------------------------------------


async def _create_reservation(client: httpx.AsyncClient, device_id: str) -> httpx.Response:
    """Create an immediate ("start now") exclusive reservation for one device.

    An exclusive DUT reserved at start_time=now is provisioned synchronously in
    the create request: device flipped RESERVED in inventory, then the row lands
    ACTIVE with the reservation.created event staged in the same transaction. The
    relay publishes that staged row asynchronously, so the create call itself
    never touches NATS.
    """
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "outbox durability integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    return await client.post("/reservations/", json=body)


async def _matching_notifications(client: httpx.AsyncClient, reservation_id: str) -> list[dict]:
    """Return the caller's reservation.created notifications for this reservation."""
    resp = await client.get("/notifications/notifications", params={"limit": 100, "offset": 0})
    if resp.status_code != 200:
        return []
    out = []
    for item in resp.json().get("items", []):
        if item.get("event_type") != _EVENT_TYPE:
            continue
        data = item.get("data") or {}
        if data.get("reservation_id") == reservation_id:
            out.append(item)
    return out


async def _poll_for_notification(
    client: httpx.AsyncClient,
    reservation_id: str,
    *,
    timeout: float,
    interval: float = 1.0,
) -> list[dict]:
    """Poll until at least one matching notification lands, or the timeout hits.

    Returns the matching list (possibly empty on timeout) so the caller asserts.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        items = await _matching_notifications(client, reservation_id)
        if items:
            return items
        await asyncio.sleep(interval)
    return await _matching_notifications(client, reservation_id)


# --- safety net: NATS must be running after any test that stops it ------------


@pytest.fixture
async def nats_restart_guard():
    """Guarantee NATS is running and reachable after the test, no matter what.

    NATS is declared `restart: unless-stopped`, so a manual `docker compose stop
    nats` stays stopped. A failed assertion mid-test (the suite runs with -x)
    would otherwise leave NATS down and wedge every later test. This finalizer
    runs unconditionally in the finally branch: it issues `docker compose start
    nats` (idempotent if already running) and waits for reachability. If docker
    compose is not usable the start is a no-op None and the wait simply reports
    the current state; the test body will have skipped in that case anyway.
    """
    try:
        yield
    finally:
        await _compose("start", "nats")
        await _wait_nats(True, timeout=60.0)


# --- tests -------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_outbox_delivers_reservation_event_end_to_end(user_client, visible_fresh_device):
    """Happy path (no outage): the outbox to relay to consumer chain delivers
    exactly one reservation.created notification.

    The device is exposed via `visible_fresh_device` so the non-admin caller
    passes the reservations create-route visibility check.
    """
    create = await _create_reservation(user_client, visible_fresh_device["id"])
    assert create.status_code == 201, create.text
    reservation = create.json()
    try:
        items = await _poll_for_notification(user_client, reservation["id"], timeout=30.0)
        assert items, "reservation.created notification never arrived via the outbox relay"

        # At-least-once delivery plus event_id idempotency must collapse to a
        # single row. Settle briefly to let any duplicate redelivery land (it
        # would be deduped), then assert exactly one.
        await asyncio.sleep(3.0)
        final = await _matching_notifications(user_client, reservation["id"])
        assert len(final) == 1, (
            f"expected exactly one reservation.created notification, got {len(final)}; "
            "consumer idempotency on event_id did not hold"
        )
    finally:
        await user_client.delete(f"/reservations/{reservation['id']}")


@pytest.mark.timeout(240)
async def test_outbox_survives_nats_outage(user_client, visible_fresh_device, nats_restart_guard):
    """Acceptance criterion: a reservation created while NATS is DOWN still
    commits, is buffered in the outbox, and is delivered exactly once once NATS
    is back.

    `nats_restart_guard` guarantees NATS is restarted even if any assertion
    below fails.
    """
    # Preconditions: skip (do not fail) if we cannot control docker compose or
    # cannot currently reach NATS from this host.
    ver = await _compose("version", timeout=20.0)
    if ver is None or ver.returncode != 0:
        pytest.skip("docker compose is not usable from the test host")
    if not await _nats_reachable():
        pytest.skip("NATS is not reachable from the test host (NATS_URL_HOST)")

    reservation_id = None
    try:
        # 1. Stop NATS and confirm it is actually down.
        stop = await _compose("stop", "nats", timeout=60.0)
        if stop is None or stop.returncode != 0:
            detail = stop.stderr.strip() if stop else "docker compose unavailable"
            pytest.skip(f"could not stop the nats container: {detail}")
        assert await _wait_nats(False, timeout=30.0), "nats did not go down after stop"

        # 2. Create a reservation while NATS is down. The API must SUCCEED and
        #    the reservation must be ACTIVE: the state change committed even
        #    though no live publish was possible. This is the whole point of the
        #    outbox, the producer no longer depends on a reachable broker.
        create = await _create_reservation(user_client, visible_fresh_device["id"])
        assert create.status_code == 201, (
            f"create failed while NATS was down (status {create.status_code}): {create.text}"
        )
        reservation = create.json()
        reservation_id = reservation["id"]
        assert reservation["status"] == "ACTIVE", (
            f"reservation should be ACTIVE despite NATS being down, got {reservation['status']!r}"
        )

        # 3. Lenient buffering check: with NATS still down the staged event
        #    cannot have been delivered yet, so no notification exists. Kept
        #    short to avoid flakiness; the row is buffered, not lost.
        await asyncio.sleep(2.0)
        buffered = await _matching_notifications(user_client, reservation_id)
        assert buffered == [], (
            "a reservation.created notification was delivered while NATS was down; "
            "the event should have stayed buffered in the outbox"
        )

        # 4. Bring NATS back and wait until it is reachable again.
        start = await _compose("start", "nats", timeout=60.0)
        assert start is not None and start.returncode == 0, (
            f"could not restart the nats container: "
            f"{start.stderr.strip() if start else 'docker compose unavailable'}"
        )
        assert await _wait_nats(True, timeout=60.0), "nats did not come back up after start"

        # 5. The relay drains the buffered row (5s default tick) and the consumer
        #    creates the notification. Generous timeout for relay tick plus
        #    consumer catch-up. Delivered exactly once (not lost, not duplicated).
        items = await _poll_for_notification(user_client, reservation_id, timeout=60.0)
        assert items, (
            "the buffered reservation.created event was never delivered after NATS "
            "recovered; the outbox relay did not drain it"
        )
        await asyncio.sleep(3.0)
        final = await _matching_notifications(user_client, reservation_id)
        assert len(final) == 1, (
            f"expected exactly one delivered notification after recovery, got {len(final)}; "
            "the event was lost or duplicated across the outage"
        )
    finally:
        if reservation_id is not None:
            try:
                await user_client.delete(f"/reservations/{reservation_id}")
            except Exception:  # noqa: BLE001 - cleanup must never mask the result
                pass
