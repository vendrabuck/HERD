"""End-to-end integration: a device.health_transition NATS event
produces in-app notifications for both admins and active reservation
holders via the notifications consumer.

The full scheduler pipeline (poll a device until it fails N times,
then watch for the publish) requires minutes of wall-clock waiting
against a live driver, so this test instead publishes the NATS event
directly. The producer (health scheduler) is exhaustively covered by
unit tests; this integration test validates the consumer + recipient
resolver end-to-end against real auth, reservations, and notifications
services.

Assumes a running HERD stack (make up) with NATS, notifications, auth,
reservations, and inventory services reachable through Traefik.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import nats
import pytest

pytestmark = pytest.mark.asyncio


HEALTH_NATS_SUBJECT = "herd.health.status_changed"


async def _publish_health_event(payload: dict) -> None:
    """Connect to NATS, publish to herd.health.status_changed, close.

    NATS_URL defaults to the docker-compose-internal hostname; override
    via env to talk to a host-mapped port (`make up` does not expose 4222
    by default). Tests that can't reach NATS skip rather than fail.
    """
    nats_url = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
    nc = await nats.connect(nats_url, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Idempotent: stream is created by execution's lifespan, but if
        # this test runs before execution boots we want a clean fail.
        await js.add_stream(name="HERD_HEALTH", subjects=["herd.health.*"])
        await js.publish(HEALTH_NATS_SUBJECT, json.dumps(payload).encode())
    finally:
        await nc.close()


async def _poll_for_notification(
    client: httpx.AsyncClient,
    event_type: str,
    device_id: str,
    *,
    timeout: float = 15.0,
    interval: float = 0.5,
) -> dict | None:
    """Poll the caller's notification list until a matching entry lands."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/notifications/notifications", params={"limit": 50, "offset": 0})
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                if item.get("event_type") != event_type:
                    continue
                data = item.get("data") or {}
                if data.get("device_id") == device_id:
                    return item
        await asyncio.sleep(interval)
    return None


def _bad_news_event(device_id: str, device_name: str = "int-device") -> dict:
    return {
        "event": "device.health_transition",
        "device_id": device_id,
        "device_name": device_name,
        "old_status": "HEALTHY",
        "new_status": "UNREACHABLE",
        "transition_kind": "bad_news",
        "consecutive_failures": 3,
        "last_run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _recovery_event(device_id: str, device_name: str = "int-device") -> dict:
    return {
        "event": "device.health_transition",
        "device_id": device_id,
        "device_name": device_name,
        "old_status": "UNREACHABLE",
        "new_status": "HEALTHY",
        "transition_kind": "recovery",
        "consecutive_failures": 0,
        "last_run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def test_bad_news_event_reaches_admin_in_app(admin_client):
    """Publishing a bad-news event lands a notification for the admin caller."""
    device_id = str(uuid.uuid4())
    try:
        await _publish_health_event(_bad_news_event(device_id, device_name="int-bad-news"))
    except Exception as exc:
        pytest.skip(f"NATS unreachable from test host: {exc}")

    matched = await _poll_for_notification(admin_client, "device.health_transition", device_id)
    assert matched is not None, "bad_news notification did not arrive for admin"
    assert matched["data"]["transition_kind"] == "bad_news"
    assert matched["data"]["device_name"] == "int-bad-news"
    assert "unhealthy" in matched["title"].lower()


async def test_recovery_event_reaches_admin_in_app(admin_client):
    """Recovery events use a different renderer."""
    device_id = str(uuid.uuid4())
    try:
        await _publish_health_event(_recovery_event(device_id, device_name="int-recovery"))
    except Exception as exc:
        pytest.skip(f"NATS unreachable from test host: {exc}")

    matched = await _poll_for_notification(admin_client, "device.health_transition", device_id)
    assert matched is not None, "recovery notification did not arrive for admin"
    assert matched["data"]["transition_kind"] == "recovery"
    assert "recovered" in matched["title"].lower()


async def test_active_reservation_holder_also_receives(user_client, visible_fresh_device):
    """A non-admin user with an active reservation on the device gets notified.

    The device is exposed via `visible_fresh_device` so the non-admin caller
    passes the reservations create-route visibility check.
    """
    fresh_device = visible_fresh_device
    device_id = fresh_device["id"]

    # Book an active reservation in the caller's name so the recipient
    # resolver picks them up via reservations /internal/active-users.
    now = datetime.now(timezone.utc)
    res_body = {
        "device_ids": [device_id],
        "purpose": "health-alerting integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    res_resp = await user_client.post("/reservations/", json=res_body)
    res_resp.raise_for_status()
    reservation = res_resp.json()
    try:
        try:
            await _publish_health_event(
                _bad_news_event(device_id, device_name=fresh_device.get("name", "int-device"))
            )
        except Exception as exc:
            pytest.skip(f"NATS unreachable from test host: {exc}")

        matched = await _poll_for_notification(user_client, "device.health_transition", device_id)
        assert matched is not None, "reservation holder did not receive health transition"
        assert matched["data"]["transition_kind"] == "bad_news"
    finally:
        await user_client.delete(f"/reservations/{reservation['id']}")


async def test_event_with_no_recipients_drops_silently(user_client):
    """A device with no holders and a non-admin caller results in no notification.

    The caller is a regular user (not admin), and the random device_id has no
    active reservation. Recipient resolver returns admins (which the caller is
    not in) and an empty holder list, so the user_client sees nothing.
    """
    device_id = str(uuid.uuid4())
    try:
        await _publish_health_event(_bad_news_event(device_id, device_name="int-no-recipients"))
    except Exception as exc:
        pytest.skip(f"NATS unreachable from test host: {exc}")

    # Short timeout: if a notification was going to arrive for this user
    # it would have done so by now.
    matched = await _poll_for_notification(
        user_client, "device.health_transition", device_id, timeout=4.0
    )
    assert matched is None
