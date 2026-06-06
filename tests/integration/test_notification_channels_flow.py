"""Integration: outbound channel preferences and the expiring-soon reminder.

Assumes a running HERD stack (make up) with NATS, notifications, reservations,
inventory, user-profile, and auth reachable through Traefik. These exercise the
cross-service surface of ROADMAP #40:

- the preferences proxy round-trips the new per-channel toggles (email, chat,
  webhook) and the expiring_soon event toggle through user-profile;
- a reservation whose end_time falls inside the reminder lead window produces
  exactly one in-app reservation.expiring_soon notification, driven by the
  reservations expiration task publishing onto HERD_RESERVATIONS and the
  notifications consumer turning it into a row.

Outbound transport (SMTP/chat/webhook) is instance config and is not asserted to
actually deliver here, only that opt-in persists and the consumer honors it. The
unit suite covers the transport send paths with stubs.
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
    timeout: float = 90.0,
    interval: float = 2.0,
) -> dict | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await user_client.get(
            "/notifications/notifications", params={"limit": 100, "offset": 0}
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


async def test_channel_preferences_round_trip(user_client):
    """PUT then GET returns the new per-channel toggles unchanged."""
    update = {
        "channels": {"in_app": True, "email": True, "chat": False, "webhook": True},
        "events": {"reservation.expiring_soon": True},
    }
    put_resp = await user_client.put(
        "/notifications/notifications/preferences", json=update
    )
    assert put_resp.status_code == 200, put_resp.text
    got = put_resp.json()
    assert got["channels"]["email"] is True
    assert got["channels"]["webhook"] is True
    assert got["channels"]["chat"] is False
    assert got["events"]["reservation.expiring_soon"] is True

    get_resp = await user_client.get("/notifications/notifications/preferences")
    assert get_resp.status_code == 200
    reloaded = get_resp.json()
    assert reloaded["channels"]["email"] is True
    assert reloaded["channels"]["webhook"] is True

    # Restore outbound channels to default off for shared state hygiene.
    await user_client.put(
        "/notifications/notifications/preferences",
        json={"channels": {"in_app": True, "email": False, "chat": False, "webhook": False}},
    )


async def test_expiring_soon_reminder_produces_single_notification(
    user_client, visible_fresh_device
):
    """A reservation ending inside the lead window yields exactly one
    expiring_soon in-app notification.

    The expiration task runs on an interval and the default lead window is one
    hour, so a reservation ending a few minutes out is in-window. We poll long
    enough to cover at least one expiration tick. Exactly-once is asserted by
    counting matches after the reminder lands.
    """
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [visible_fresh_device["id"]],
        "purpose": "expiring_soon integration",
        "start_time": now.isoformat(),
        # End a few minutes out: in the future (not yet completed) but well
        # inside the default one-hour reminder lead window.
        "end_time": (now + timedelta(minutes=3)).isoformat(),
    }
    resp = await user_client.post("/reservations/", json=body)
    resp.raise_for_status()
    reservation = resp.json()
    try:
        matched = await _poll_for_notification(
            user_client, "reservation.expiring_soon", reservation["id"]
        )
        assert matched is not None, "expiring_soon notification did not arrive"

        # Exactly one: a second poll window must not add a duplicate row.
        await asyncio.sleep(5.0)
        listing = await user_client.get(
            "/notifications/notifications", params={"limit": 100, "offset": 0}
        )
        count = sum(
            1
            for item in listing.json().get("items", [])
            if item.get("event_type") == "reservation.expiring_soon"
            and (item.get("data") or {}).get("reservation_id") == reservation["id"]
        )
        assert count == 1, f"expected exactly one expiring_soon notification, got {count}"
    finally:
        await user_client.delete(f"/reservations/{reservation['id']}")
