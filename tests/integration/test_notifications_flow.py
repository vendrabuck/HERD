"""End-to-end integration: reservation lifecycle events produce in-app
notifications via the NATS consumer, and the preferences proxy round-trips
through user-profile.

Assumes a running HERD stack (make up) with NATS, notifications, reservations,
inventory, and user-profile services reachable through Traefik.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def _poll_for_notification(
    user_client: httpx.AsyncClient,
    event_type: str,
    reservation_id: str,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> dict | None:
    """Poll the user's notification list until a matching entry lands or we time out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await user_client.get(
            "/notifications/notifications", params={"limit": 50, "offset": 0}
        )
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                if item.get("event_type") != event_type:
                    continue
                data = item.get("data") or {}
                if data.get("reservation_id") == reservation_id:
                    return item
        await asyncio.sleep(interval)
    return None


async def _create_reservation(client, device_id: str, purpose_category: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "notifications integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if purpose_category is not None:
        body["purpose_category"] = purpose_category
    resp = await client.post("/reservations/", json=body)
    resp.raise_for_status()
    return resp.json()


async def test_reservation_created_produces_in_app_notification(user_client, visible_fresh_device):
    """Happy path: a reservation event fans out to an in-app notification row.

    The device is exposed via `visible_fresh_device` so the non-admin caller
    passes the reservations create-route visibility check.
    """
    reservation = await _create_reservation(
        user_client, visible_fresh_device["id"], purpose_category="qa_regression"
    )
    try:
        matched = await _poll_for_notification(
            user_client, "reservation.created", reservation["id"]
        )
        assert matched is not None, "reservation.created notification did not arrive"
        assert matched["read_at"] is None
        # The notification's data is the outbox event payload verbatim
        # (issue #646 phase 1: purpose_category rides every reservation.*
        # lifecycle event, additive).
        assert matched["data"]["purpose_category"] == "qa_regression"
    finally:
        await user_client.delete(f"/reservations/{reservation['id']}")


async def test_preferences_round_trip_via_proxy(user_client):
    """GET after PUT returns the same event_enabled/channel_enabled values."""
    update = {
        "channels": {"in_app": True},
        "events": {"reservation.created": False},
    }
    put_resp = await user_client.put("/notifications/notifications/preferences", json=update)
    assert put_resp.status_code == 200, put_resp.text
    got = put_resp.json()
    assert got["events"]["reservation.created"] is False

    get_resp = await user_client.get("/notifications/notifications/preferences")
    assert get_resp.status_code == 200
    reloaded = get_resp.json()
    assert reloaded["events"]["reservation.created"] is False

    # Restore to default so other tests see a clean state.
    await user_client.put(
        "/notifications/notifications/preferences",
        json={"events": {"reservation.created": True}},
    )


async def test_disabled_event_suppresses_notification(user_client, visible_fresh_device):
    """With reservation.created disabled, no new notification row is created.

    The device is exposed via `visible_fresh_device` so the non-admin caller
    passes the reservations create-route visibility check.
    """
    fresh_device = visible_fresh_device
    # Snapshot current count so we measure a delta.
    baseline = await user_client.get("/notifications/notifications", params={"limit": 1})
    baseline_total = baseline.json().get("total", 0) if baseline.status_code == 200 else 0

    await user_client.put(
        "/notifications/notifications/preferences",
        json={"events": {"reservation.created": False}},
    )
    try:
        reservation = await _create_reservation(user_client, fresh_device["id"])
        try:
            # Give the consumer a window to process the event.
            await asyncio.sleep(3.0)
            after = await user_client.get(
                "/notifications/notifications",
                params={"limit": 50, "offset": 0},
            )
            items = after.json().get("items", [])
            for item in items:
                data = item.get("data") or {}
                assert data.get("reservation_id") != reservation["id"], (
                    "notification was created despite event opt-out"
                )
            assert after.json().get("total", 0) <= baseline_total + 1, (
                "unexpected notification count increase while event was disabled"
            )
        finally:
            await user_client.delete(f"/reservations/{reservation['id']}")
    finally:
        await user_client.put(
            "/notifications/notifications/preferences",
            json={"events": {"reservation.created": True}},
        )
