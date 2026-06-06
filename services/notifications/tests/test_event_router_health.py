"""Tests for the device.health_transition path through event_router.

iter 2 fans these events out to admins + active reservation holders,
deduped. The recipient resolver is mocked at the module level so these
tests focus on the renderer + fan-out + dedup logic, not the HTTP plumbing.
Plumbing tests live in test_health_recipients.py.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.services import event_router


def _health_event(*, transition_kind: str = "bad_news") -> dict:
    return {
        "event": "device.health_transition",
        "device_id": str(uuid.uuid4()),
        "device_name": "ex-01",
        "old_status": "HEALTHY" if transition_kind == "bad_news" else "UNREACHABLE",
        "new_status": "UNREACHABLE" if transition_kind == "bad_news" else "HEALTHY",
        "transition_kind": transition_kind,
        "consecutive_failures": 3 if transition_kind == "bad_news" else 0,
        "last_run_id": str(uuid.uuid4()),
        "timestamp": "2026-05-26T12:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_bad_news_event_fans_out_to_all_recipients():
    recipients = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    event = _health_event(transition_kind="bad_news")
    with patch(
        "app.services.event_router.resolve_health_recipients",
        new=AsyncMock(return_value=recipients),
    ):
        messages = await event_router.build_messages(event)
    assert len(messages) == 3
    recipient_ids = {m.user_id for m in messages}
    assert recipient_ids == set(recipients)
    for msg in messages:
        assert msg.event_type == "device.health_transition"
        assert "unhealthy" in msg.title.lower()
        assert "3 consecutive" in msg.body


@pytest.mark.asyncio
async def test_recovery_event_uses_different_renderer():
    recipients = [uuid.uuid4()]
    event = _health_event(transition_kind="recovery")
    with patch(
        "app.services.event_router.resolve_health_recipients",
        new=AsyncMock(return_value=recipients),
    ):
        messages = await event_router.build_messages(event)
    assert len(messages) == 1
    msg = messages[0]
    assert "recovered" in msg.title.lower()
    assert "returned to HEALTHY" in msg.body


@pytest.mark.asyncio
async def test_empty_recipient_list_produces_no_messages():
    """If the resolver can't reach auth and reservations, drop silently."""
    event = _health_event()
    with patch(
        "app.services.event_router.resolve_health_recipients",
        new=AsyncMock(return_value=[]),
    ):
        messages = await event_router.build_messages(event)
    assert messages == []


@pytest.mark.asyncio
async def test_event_missing_device_id_is_skipped():
    event = _health_event()
    del event["device_id"]
    # Resolver should never be called when device_id is missing.
    mock_resolver = AsyncMock(return_value=[uuid.uuid4()])
    with patch("app.services.event_router.resolve_health_recipients", new=mock_resolver):
        messages = await event_router.build_messages(event)
    assert messages == []
    mock_resolver.assert_not_called()


@pytest.mark.asyncio
async def test_event_invalid_device_id_is_skipped():
    event = _health_event()
    event["device_id"] = "not-a-uuid"
    mock_resolver = AsyncMock(return_value=[uuid.uuid4()])
    with patch("app.services.event_router.resolve_health_recipients", new=mock_resolver):
        messages = await event_router.build_messages(event)
    assert messages == []
    mock_resolver.assert_not_called()


@pytest.mark.asyncio
async def test_event_missing_device_name_falls_back():
    """`device_name` is best-effort; falls back to 'device' if missing."""
    event = _health_event()
    del event["device_name"]
    recipients = [uuid.uuid4()]
    with patch(
        "app.services.event_router.resolve_health_recipients",
        new=AsyncMock(return_value=recipients),
    ):
        messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert "device" in messages[0].title.lower()


@pytest.mark.asyncio
async def test_event_with_one_message_per_recipient_carries_event_data():
    """The full event payload is attached as `data` so the frontend can show details."""
    recipients = [uuid.uuid4()]
    event = _health_event()
    with patch(
        "app.services.event_router.resolve_health_recipients",
        new=AsyncMock(return_value=recipients),
    ):
        messages = await event_router.build_messages(event)
    assert messages[0].data == event
