import time
import uuid
from datetime import UTC, datetime

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


# Issue #1564: every time printed with the label UTC is the UTC instant the event's
# end_time names, whatever offset it carried. Each row is (input, rendered).
_END_TIME_RENDERINGS = [
    ("2026-11-25T18:00:00+00:00", "2026-11-25 18:00 UTC"),
    ("2026-11-25T18:00:00Z", "2026-11-25 18:00 UTC"),
    ("2026-11-25T18:00:00-00:00", "2026-11-25 18:00 UTC"),
    ("2026-11-25T10:00:00-08:00", "2026-11-25 18:00 UTC"),
    ("2026-11-25T23:45:00+05:45", "2026-11-25 18:00 UTC"),
    # Offsets whose conversion crosses a date, month, or year boundary.
    ("2026-11-26T03:00:00+09:00", "2026-11-25 18:00 UTC"),
    ("2026-11-25T20:00:00-05:30", "2026-11-26 01:30 UTC"),
    ("2026-12-31T19:00:00-05:00", "2027-01-01 00:00 UTC"),
    ("2027-01-01T08:00:00+14:00", "2026-12-31 18:00 UTC"),
    # Fractional seconds are dropped, not rounded, before and after conversion.
    ("2026-11-25T09:59:59.999999-08:00", "2026-11-25 17:59 UTC"),
    ("2026-11-25T18:00:30.5Z", "2026-11-25 18:00 UTC"),
    ("2026-11-25T18:59:59.999+01:00", "2026-11-25 17:59 UTC"),
    # Naive values are taken as UTC.
    ("2026-11-25T18:00:00", "2026-11-25 18:00 UTC"),
    ("2026-11-25T18:00:00.123456", "2026-11-25 18:00 UTC"),
    ("2026-11-25", "2026-11-25 00:00 UTC"),
]


@pytest.mark.parametrize(("end_time", "expected"), _END_TIME_RENDERINGS)
def test_format_end_time_renders_the_utc_instant(end_time, expected):
    assert event_router._format_end_time(end_time) == expected


@pytest.fixture
def host_zone_far_from_utc(monkeypatch):
    """Run with the process's local zone at a fixed -12:00 (POSIX TZ, no zone files)."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", "XST12")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(("end_time", "expected"), _END_TIME_RENDERINGS)
def test_format_end_time_ignores_the_host_zone(host_zone_far_from_utc, end_time, expected):
    """A naive value is UTC, never the process's local zone (which astimezone assumes)."""
    assert datetime(2031, 1, 1).astimezone(UTC).hour == 12  # the fixture took effect
    assert event_router._format_end_time(end_time) == expected


@pytest.mark.parametrize(
    "end_time",
    [
        "not-a-time",
        "Zulu",
        "2026-13-01T00:00:00Z",
        "2026-11-25T25:00:00+00:00",
        "2026-11-25T18:00:00+24:00",
        # Whitespace is not stripped before parsing, and a long value is not cut.
        " 2026-11-25T18:00:00Z ",
        "the end time could not be read from this event payload at all",
        # Parseable, but the instant has no UTC equivalent (before year 1, after 9999).
        "0001-01-01T00:00:00+05:00",
        "9999-12-31T23:00:00-05:00",
    ],
)
def test_format_end_time_returns_an_unusable_string_unchanged(end_time):
    assert event_router._format_end_time(end_time) == end_time


@pytest.mark.parametrize("end_time", [None, "", 1764093600, 0, ["2026-11-25T18:00:00Z"], {}])
def test_format_end_time_missing_or_non_string_is_unknown_end(end_time):
    assert event_router._format_end_time(end_time) == "unknown end"


def _end_time_event(event_type: str, end_time) -> dict:
    """An event of `event_type` that renders its end time, carrying `end_time`."""
    event = {
        "event": event_type,
        "reservation_id": str(uuid.uuid4()),
        "user_id": _user(),
        "device_ids": [str(uuid.uuid4())],
        "end_time": end_time,
    }
    if event_type == "reservation.updated":
        event.update(added_device_ids=[], removed_device_ids=[], end_time_changed=True)
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "body"),
    [
        (
            "reservation.created",
            "Reservation for 1 device is active until 2026-11-25 18:00 UTC.",
        ),
        ("reservation.updated", "Reservation updated: ends 2026-11-25 18:00 UTC"),
        (
            "reservation.expiring_soon",
            "Reservation for 1 device ends at 2026-11-25 18:00 UTC.",
        ),
    ],
)
@pytest.mark.parametrize("end_time", ["2026-11-25T10:00:00-08:00", "2026-11-26T03:00:00+09:00"])
async def test_every_end_time_message_states_the_utc_instant(event_type, body, end_time):
    """Issue #1564: each renderer that prints the end time prints the UTC instant."""
    messages = await event_router.build_messages(_end_time_event(event_type, end_time))
    assert len(messages) == 1
    assert messages[0].body == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", ["reservation.created", "reservation.updated", "reservation.expiring_soon"]
)
@pytest.mark.parametrize("end_time", ["0001-01-01T00:00:00+05:00", 1764093600])
async def test_unusable_end_time_still_notifies(event_type, end_time):
    """A value the formatter cannot convert never raises out of build_messages."""
    messages = await event_router.build_messages(_end_time_event(event_type, end_time))
    assert len(messages) == 1
    expected = end_time if isinstance(end_time, str) else "unknown end"
    assert expected in messages[0].body
