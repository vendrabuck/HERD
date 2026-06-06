import uuid

import pytest
from app.services import event_router


def _user() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_created_event_produces_single_message():
    user_id = _user()
    event = {
        "event": "reservation.created",
        "reservation_id": str(uuid.uuid4()),
        "user_id": user_id,
        "device_ids": [str(uuid.uuid4())],
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    msg = messages[0]
    assert str(msg.user_id) == user_id
    assert msg.event_type == "reservation.created"
    assert "Reservation confirmed" == msg.title
    assert "1 device" in msg.body
    assert "2026-04-21" in msg.body
    assert msg.data == event


@pytest.mark.asyncio
async def test_expiring_soon_event_produces_single_message():
    user_id = _user()
    event = {
        "event": "reservation.expiring_soon",
        "reservation_id": str(uuid.uuid4()),
        "user_id": user_id,
        "device_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    msg = messages[0]
    assert str(msg.user_id) == user_id
    assert msg.event_type == "reservation.expiring_soon"
    assert msg.title == "Reservation expiring soon"
    assert "2 devices" in msg.body
    assert "2026-04-21" in msg.body


@pytest.mark.asyncio
async def test_updated_event_with_no_material_change_skips():
    event = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [str(uuid.uuid4())],
        "added_device_ids": [],
        "removed_device_ids": [],
    }
    assert await event_router.build_messages(event) == []


@pytest.mark.asyncio
async def test_updated_event_with_added_devices_emits():
    event = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [str(uuid.uuid4())],
        "added_device_ids": [str(uuid.uuid4())],
        "removed_device_ids": [],
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert "added 1" in messages[0].body


@pytest.mark.asyncio
async def test_cancelled_and_completed_events_emit():
    for event_type, title in (
        ("reservation.cancelled", "Reservation cancelled"),
        ("reservation.completed", "Reservation completed"),
    ):
        event = {
            "event": event_type,
            "reservation_id": str(uuid.uuid4()),
            "user_id": _user(),
            "device_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        }
        messages = await event_router.build_messages(event)
        assert len(messages) == 1
        assert messages[0].title == title
        assert "2 devices" in messages[0].body


@pytest.mark.asyncio
async def test_unknown_event_is_skipped():
    event = {"event": "reservation.nonsense", "user_id": _user()}
    assert await event_router.build_messages(event) == []


@pytest.mark.asyncio
async def test_invalid_user_id_is_skipped():
    event = {
        "event": "reservation.created",
        "user_id": "not-a-uuid",
        "device_ids": [],
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    assert await event_router.build_messages(event) == []


@pytest.mark.asyncio
async def test_missing_user_id_is_skipped():
    event = {"event": "reservation.created", "device_ids": []}
    assert await event_router.build_messages(event) == []


@pytest.mark.asyncio
async def test_created_event_zero_devices():
    event = {
        "event": "reservation.created",
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [],
        "end_time": None,
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert "no devices" in messages[0].body
