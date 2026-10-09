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
async def test_updated_metadata_only_with_unchanged_end_time_skips():
    # Mirrors the real producer payload for a metadata-only edit (e.g. purpose):
    # the producer flags end_time_changed=False and nulls end_time, so the
    # consumer must suppress a misleading "ends <time>" notification.
    event = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [str(uuid.uuid4())],
        "added_device_ids": [],
        "removed_device_ids": [],
        "end_time_changed": False,
        "end_time": None,
    }
    assert await event_router.build_messages(event) == []


@pytest.mark.asyncio
async def test_updated_event_with_changed_end_time_emits():
    event = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [str(uuid.uuid4())],
        "added_device_ids": [],
        "removed_device_ids": [],
        "end_time_changed": True,
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert "ends" in messages[0].body
    assert "2026-04-21" in messages[0].body


@pytest.mark.asyncio
async def test_updated_event_with_added_devices_emits():
    event = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [str(uuid.uuid4())],
        "added_device_ids": [str(uuid.uuid4())],
        "removed_device_ids": [],
        "end_time_changed": False,
        "end_time": None,
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert "added 1" in messages[0].body
    # Device-only change must not advertise an unchanged end time.
    assert "ends" not in messages[0].body


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


@pytest.mark.asyncio
async def test_failed_event_notifies_the_owner():
    """Issue #1077: reservation.failed produces one message for the owner."""
    user_id = _user()
    event = {
        "event": "reservation.failed",
        "reservation_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "user_id": user_id,
        "device_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        "topology_id": None,
        "topology_type": "PHYSICAL",
        "purpose_category": None,
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    msg = messages[0]
    assert str(msg.user_id) == user_id
    assert msg.event_type == "reservation.failed"
    assert msg.title == "Reservation failed"
    assert msg.body == "Reservation 0f1e2d3c for 2 devices failed."
    assert msg.data == event


@pytest.mark.asyncio
async def test_failed_event_text_carries_no_upstream_error_text():
    """The rendered text is built from the id and device count only.

    The producer's payload has no reason field; even if a future payload carried
    one, its text must not reach the title or body (class-name-only rule).
    """
    leak = "ConnectError: [Errno 111] connect to 10.0.0.5:22 refused password=hunter2"
    event = {
        "event": "reservation.failed",
        "reservation_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "user_id": _user(),
        "device_ids": [],
        "error": leak,
        "reason": leak,
        "last_error": leak,
    }
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert messages[0].title == "Reservation failed"
    assert messages[0].body == "Reservation 0f1e2d3c for no devices failed."
    assert leak not in messages[0].title + messages[0].body


@pytest.mark.asyncio
async def test_failed_event_without_reservation_id_still_notifies():
    event = {"event": "reservation.failed", "user_id": _user(), "device_ids": ["d1"]}
    messages = await event_router.build_messages(event)
    assert len(messages) == 1
    assert messages[0].body == "Reservation for 1 device failed."


@pytest.mark.asyncio
async def test_failed_event_without_user_id_is_skipped():
    event = {"event": "reservation.failed", "reservation_id": "r1", "device_ids": []}
    assert await event_router.build_messages(event) == []


@pytest.mark.asyncio
async def test_other_unrendered_reservation_events_are_still_skipped():
    """INTEG-ROUTE-2: provision_requested and wiring_changed still produce nothing."""
    for name in ("reservation.provision_requested", "reservation.wiring_changed"):
        event = {"event": name, "reservation_id": "r1", "user_id": _user(), "device_ids": []}
        assert await event_router.build_messages(event) == []
