import json
import uuid
from unittest.mock import AsyncMock

import pytest
from app.database import Base
from app.models.notification import Notification
from app.schemas.preferences import NotificationPreferences
from app.services import nats_consumer
from app.services.dispatchers.base import DispatchMessage
from app.services.preferences_client import PreferencesClient, set_preferences_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


def _session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._s = _SessionLocal()
            return self._s

        async def __aexit__(self, *args):
            await self._s.close()

    return _Ctx()


@pytest.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class _StubPrefsClient(PreferencesClient):
    def __init__(self, prefs: NotificationPreferences):
        self._prefs = prefs

    async def get(self, user_id):
        return self._prefs

    def invalidate(self, user_id):
        pass


@pytest.fixture(autouse=True)
def _prefs_all_enabled():
    set_preferences_client(_StubPrefsClient(NotificationPreferences.with_defaults(None)))
    yield
    set_preferences_client(None)


class _StubDispatcher:
    def __init__(self, channel="in_app"):
        self.channel = channel
        self.sent: list[DispatchMessage] = []

    async def send(self, session_factory, message):
        self.sent.append(message)


class _FakeMsg:
    def __init__(
        self,
        data: bytes,
        num_delivered: int = 1,
        stream_seq: int | None = None,
        stream: str = "HERD_RESERVATIONS",
    ):
        self.data = data
        seq = type("Seq", (), {"stream": stream_seq}) if stream_seq is not None else None
        self.metadata = type(
            "M", (), {"num_delivered": num_delivered, "sequence": seq, "stream": stream}
        )
        self.ack = AsyncMock()
        self.nak = AsyncMock()


@pytest.mark.asyncio
async def test_handle_event_creates_in_app_notification():
    dispatcher = _StubDispatcher()
    event = {
        "event": "reservation.created",
        "user_id": str(uuid.uuid4()),
        "device_ids": [str(uuid.uuid4())],
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    await nats_consumer.handle_event(event, _session_factory, dispatchers=[dispatcher])
    assert len(dispatcher.sent) == 1
    assert dispatcher.sent[0].event_type == "reservation.created"


@pytest.mark.asyncio
async def test_handle_event_respects_event_opt_out():
    set_preferences_client(
        _StubPrefsClient(
            NotificationPreferences(
                channels={"in_app": True}, events={"reservation.created": False}
            )
        )
    )
    dispatcher = _StubDispatcher()
    event = {
        "event": "reservation.created",
        "user_id": str(uuid.uuid4()),
        "device_ids": [],
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    await nats_consumer.handle_event(event, _session_factory, dispatchers=[dispatcher])
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_handle_event_respects_channel_opt_out():
    set_preferences_client(
        _StubPrefsClient(NotificationPreferences(channels={"in_app": False}, events={}))
    )
    dispatcher = _StubDispatcher()
    event = {
        "event": "reservation.cancelled",
        "user_id": str(uuid.uuid4()),
        "device_ids": [],
    }
    await nats_consumer.handle_event(event, _session_factory, dispatchers=[dispatcher])
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_process_message_ack_on_success():
    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
            "end_time": "2026-04-21T00:00:00+00:00",
        }
    ).encode()
    msg = _FakeMsg(payload)
    js = AsyncMock()

    dispatcher = _StubDispatcher()

    async def _handler(event, sf, dedupe_key=None):
        await nats_consumer.handle_event(event, sf, dispatchers=[dispatcher])

    result = await nats_consumer.process_message(msg, js, _handler, _session_factory)
    assert result == "ack"
    msg.ack.assert_awaited_once()
    assert len(dispatcher.sent) == 1


@pytest.mark.asyncio
async def test_process_message_poison_goes_to_dlq():
    msg = _FakeMsg(b"not-json")
    js = AsyncMock()

    async def _handler(event, sf, dedupe_key=None):
        raise AssertionError("handler should not be called on poison msg")

    result = await nats_consumer.process_message(msg, js, _handler, _session_factory)
    assert result == "dlq"
    js.publish.assert_awaited_once()
    msg.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_message_nak_on_transient_error():
    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
            "end_time": None,
        }
    ).encode()
    msg = _FakeMsg(payload, num_delivered=1)
    js = AsyncMock()

    async def _handler(event, sf, dedupe_key=None):
        raise RuntimeError("transient")

    result = await nats_consumer.process_message(msg, js, _handler, _session_factory)
    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_dlq_on_max_deliver_exhausted():
    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
            "end_time": None,
        }
    ).encode()
    msg = _FakeMsg(payload, num_delivered=nats_consumer.NATS_MAX_DELIVER)
    js = AsyncMock()

    async def _handler(event, sf, dedupe_key=None):
        raise RuntimeError("still failing")

    result = await nats_consumer.process_message(msg, js, _handler, _session_factory)
    assert result == "dlq"
    js.publish.assert_awaited_once()
    msg.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_message_poison_publishes_to_notifications_dlq_subject():
    """Poison messages route to the notifications-scoped DLQ, not the reservations DLQ."""
    msg = _FakeMsg(b"not-json-at-all")
    js = AsyncMock()

    async def _handler(event, sf, dedupe_key=None):
        pytest.fail("handler should not be called on poison msg")

    await nats_consumer.process_message(msg, js, _handler, _session_factory)
    js.publish.assert_awaited_once_with("herd.reservations.dlq.notifications", b"not-json-at-all")


@pytest.mark.asyncio
async def test_process_message_max_deliver_publishes_to_notifications_dlq_subject():
    """Exhausted messages route to the notifications-scoped DLQ with the original payload."""
    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
            "end_time": None,
        }
    ).encode()
    msg = _FakeMsg(payload, num_delivered=nats_consumer.NATS_MAX_DELIVER)
    js = AsyncMock()

    async def _handler(event, sf, dedupe_key=None):
        raise RuntimeError("persistent")

    await nats_consumer.process_message(msg, js, _handler, _session_factory)
    js.publish.assert_awaited_once_with("herd.reservations.dlq.notifications", payload)


@pytest.mark.asyncio
async def test_process_message_dlq_publish_failure_does_not_propagate():
    """If the DLQ publish itself fails we still ack so the loop keeps draining."""
    msg = _FakeMsg(b"bad")
    js = AsyncMock()
    js.publish.side_effect = RuntimeError("nats down")

    async def _handler(event, sf, dedupe_key=None):
        return None

    result = await nats_consumer.process_message(msg, js, _handler, _session_factory)
    assert result == "dlq"
    msg.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_event_propagates_prefs_client_failure():
    """If the preferences client raises, the handler propagates so the caller can NAK."""

    class _BrokenPrefsClient(PreferencesClient):
        async def get(self, user_id):
            raise RuntimeError("user-profile unreachable")

        def invalidate(self, user_id):
            pass

    set_preferences_client(_BrokenPrefsClient())
    dispatcher = _StubDispatcher()
    event = {
        "event": "reservation.created",
        "user_id": str(uuid.uuid4()),
        "device_ids": [],
        "end_time": "2026-04-21T00:00:00+00:00",
    }
    with pytest.raises(RuntimeError):
        await nats_consumer.handle_event(event, _session_factory, dispatchers=[dispatcher])
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_process_message_naks_when_prefs_client_fails():
    """End-to-end: prefs client failure should NAK the message (transient)."""

    class _BrokenPrefsClient(PreferencesClient):
        async def get(self, user_id):
            raise RuntimeError("user-profile unreachable")

        def invalidate(self, user_id):
            pass

    set_preferences_client(_BrokenPrefsClient())

    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
            "end_time": "2026-04-21T00:00:00+00:00",
        }
    ).encode()
    msg = _FakeMsg(payload, num_delivered=1)
    js = AsyncMock()

    async def _handler(event, sf, dedupe_key=None):
        await nats_consumer.handle_event(event, sf, dispatchers=[_StubDispatcher()])

    result = await nats_consumer.process_message(msg, js, _handler, _session_factory)
    assert result == "nak"
    msg.nak.assert_awaited_once()
    js.publish.assert_not_awaited()


# --- idempotency: dedupe key from NATS metadata ---


def test_dedupe_key_from_msg_uses_stream_and_sequence():
    msg = _FakeMsg(b"{}", stream_seq=42, stream="HERD_RESERVATIONS")
    assert nats_consumer._dedupe_key_from_msg(msg) == "HERD_RESERVATIONS:42"


def test_dedupe_key_from_msg_none_when_no_sequence():
    # The default _FakeMsg has no sequence metadata (non-JetStream stub).
    assert nats_consumer._dedupe_key_from_msg(_FakeMsg(b"{}")) is None


@pytest.mark.asyncio
async def test_redelivered_message_creates_one_notification():
    """Processing the same JetStream message twice (same stream sequence)
    persists a single notification row; the redelivery is deduped."""
    user_id = str(uuid.uuid4())
    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": user_id,
            "device_ids": [str(uuid.uuid4())],
            "end_time": "2026-04-21T00:00:00+00:00",
        }
    ).encode()
    js = AsyncMock()

    # Same stream sequence on both deliveries (a redelivery, not a new publish).
    first = _FakeMsg(payload, num_delivered=1, stream_seq=7)
    second = _FakeMsg(payload, num_delivered=2, stream_seq=7)

    assert (
        await nats_consumer.process_message(first, js, nats_consumer.handle_event, _session_factory)
        == "ack"
    )
    assert (
        await nats_consumer.process_message(
            second, js, nats_consumer.handle_event, _session_factory
        )
        == "ack"
    )

    async with _SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Notification).where(Notification.user_id == uuid.UUID(user_id))
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].dedupe_key == "HERD_RESERVATIONS:7"
