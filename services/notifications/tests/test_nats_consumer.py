import asyncio
import json
import sys
import types
import uuid
from unittest.mock import AsyncMock

import pytest
from app.database import Base
from app.models.notification import Notification
from app.schemas.preferences import NotificationPreferences
from app.services import nats_consumer
from app.services.dispatchers.base import DispatchMessage
from app.services.preferences_client import PreferencesClient, set_preferences_client
from herd_common.outbox import event_dedupe_key
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


# --- idempotency: dedupe key resolution (outbox event_id, issue #21) ---


def test_dedupe_key_prefers_payload_event_id():
    # With a stamped event_id the key is the stable id, regardless of sequence.
    eid = str(uuid.uuid4())
    msg = _FakeMsg(b"{}", stream_seq=42, stream="HERD_RESERVATIONS")
    assert event_dedupe_key({"event": "reservation.created", "event_id": eid}, msg) == eid


def test_dedupe_key_falls_back_to_stream_and_sequence():
    # No event_id (a pre-outbox event): fall back to "<stream>:<sequence>".
    msg = _FakeMsg(b"{}", stream_seq=42, stream="HERD_RESERVATIONS")
    assert event_dedupe_key({"event": "reservation.created"}, msg) == "HERD_RESERVATIONS:42"


def test_dedupe_key_none_when_no_event_id_and_no_sequence():
    # The default _FakeMsg has no sequence metadata (non-JetStream stub).
    assert event_dedupe_key({"event": "reservation.created"}, _FakeMsg(b"{}")) is None


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


@pytest.mark.asyncio
async def test_republished_event_same_id_new_sequence_dedupes_to_one():
    """The outbox property (issue #21): two deliveries carrying the same payload
    `event_id` but DIFFERENT stream sequences (a relay republish after an outage
    lands the event under a new JetStream sequence) collapse to a single
    notification row, because dedup now keys on the stable event_id."""
    user_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    payload = json.dumps(
        {
            "event": "reservation.created",
            "event_id": event_id,
            "user_id": user_id,
            "device_ids": [str(uuid.uuid4())],
            "end_time": "2026-04-21T00:00:00+00:00",
        }
    ).encode()
    js = AsyncMock()

    # Different stream sequences: a pre-outbox <stream>:<seq> key would treat
    # these as two distinct messages and create two rows.
    first = _FakeMsg(payload, num_delivered=1, stream_seq=7)
    second = _FakeMsg(payload, num_delivered=1, stream_seq=99)

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
    # The row is keyed on the stable event_id, not the stream sequence.
    assert rows[0].dedupe_key == event_id


@pytest.mark.asyncio
async def test_no_event_id_falls_back_to_stream_sequence_key():
    """Backward compat: a pre-outbox event (no `event_id`) is keyed on
    "<stream>:<sequence>", so two deliveries at different sequences are treated
    as distinct messages and each persists its own row."""
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

    first = _FakeMsg(payload, num_delivered=1, stream_seq=7)
    second = _FakeMsg(payload, num_delivered=1, stream_seq=8)

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
    keys = sorted(r.dedupe_key for r in rows)
    assert keys == ["HERD_RESERVATIONS:7", "HERD_RESERVATIONS:8"]


# --- start_nats_consumer / stop_nats_consumer (connection + subscription wiring) ---


class _FakeApp:
    """Minimal stand-in for the FastAPI app: just an attribute bag for state."""

    def __init__(self):
        self.state = types.SimpleNamespace()


class _FakeSubscription:
    """A JetStream pull-style subscription whose `.messages` yields a fixed
    list once, then blocks forever (mirrors the real never-ending stream)."""

    def __init__(self, msgs):
        self._msgs = msgs

    @property
    def messages(self):
        async def _gen():
            for m in self._msgs:
                yield m
            # The real subscription never ends; block so the loop task stays
            # alive until the test cancels it (as stop_nats_consumer would).
            await asyncio.Event().wait()

        return _gen()


class _FakeJetStream:
    def __init__(self, subs_by_subject=None, add_stream_error=False):
        self._subs = subs_by_subject or {}
        self.added_streams = []
        self.subscribe_calls = []
        self.published = []
        self._add_stream_error = add_stream_error

    async def add_stream(self, name, subjects):
        if self._add_stream_error:
            raise RuntimeError("stream exists / cannot update")
        self.added_streams.append((name, tuple(subjects)))

    async def subscribe(self, subject_pattern, durable, manual_ack, config):
        self.subscribe_calls.append(
            {"subject": subject_pattern, "durable": durable, "config": config}
        )
        return self._subs.get(subject_pattern, _FakeSubscription([]))

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


class _FakeNatsConn:
    def __init__(self, js):
        self._js = js
        self.closed = False

    def jetstream(self):
        return self._js

    async def close(self):
        self.closed = True


def _install_fake_nats(monkeypatch, connect_impl):
    """Inject fake `nats` and `nats.js.api` modules so the in-function imports
    in start_nats_consumer resolve to our stubs (no real broker)."""
    fake_nats = types.ModuleType("nats")
    fake_nats.connect = connect_impl
    fake_js = types.ModuleType("nats.js")
    fake_js_api = types.ModuleType("nats.js.api")

    class _ConsumerConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_js_api.ConsumerConfig = _ConsumerConfig
    monkeypatch.setitem(sys.modules, "nats", fake_nats)
    monkeypatch.setitem(sys.modules, "nats.js", fake_js)
    monkeypatch.setitem(sys.modules, "nats.js.api", fake_js_api)
    return _ConsumerConfig


@pytest.mark.asyncio
async def test_start_nats_consumer_wires_both_subscriptions(monkeypatch):
    js = _FakeJetStream()
    conn = _FakeNatsConn(js)

    async def _connect(url):
        return conn

    _install_fake_nats(monkeypatch, _connect)

    app = _FakeApp()
    await nats_consumer.start_nats_consumer(app)
    try:
        # Both streams created, both durable consumers subscribed.
        assert (nats_consumer.NATS_STREAM, (nats_consumer.NATS_SUBJECT_PATTERN,)) in (
            js.added_streams
        )
        assert (nats_consumer.HEALTH_STREAM, (nats_consumer.HEALTH_SUBJECT_PATTERN,)) in (
            js.added_streams
        )
        durables = {c["durable"] for c in js.subscribe_calls}
        assert durables == {nats_consumer.NATS_DURABLE, nats_consumer.HEALTH_DURABLE}
        # Both background loop tasks are running.
        assert not app.state.nats_consumer_task.done()
        assert not app.state.nats_health_consumer_task.done()
        assert app.state.nats is conn
    finally:
        await nats_consumer.stop_nats_consumer(app)

    assert app.state.nats_consumer_task.cancelled()
    assert conn.closed


@pytest.mark.asyncio
async def test_start_nats_consumer_loop_processes_a_message(monkeypatch):
    """A message delivered on the subscription is run through process_message,
    which dispatches an in-app notification and acks."""
    user_id = str(uuid.uuid4())
    payload = json.dumps(
        {
            "event": "reservation.created",
            "user_id": user_id,
            "device_ids": [str(uuid.uuid4())],
            "end_time": "2026-04-21T00:00:00+00:00",
        }
    ).encode()
    msg = _FakeMsg(payload, stream_seq=11)

    res_sub = _FakeSubscription([msg])
    js = _FakeJetStream(subs_by_subject={nats_consumer.NATS_SUBJECT_PATTERN: res_sub})
    conn = _FakeNatsConn(js)

    async def _connect(url):
        return conn

    _install_fake_nats(monkeypatch, _connect)

    # The session factory built inside start_nats_consumer imports
    # AsyncSessionLocal from app.database; point it at this suite's in-memory
    # engine so the dispatched row lands in the DB we then query.
    import app.database as app_db

    monkeypatch.setattr(app_db, "AsyncSessionLocal", _SessionLocal)

    app = _FakeApp()
    await nats_consumer.start_nats_consumer(app)
    try:
        # Let the loop task pull and process the single queued message.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if msg.ack.await_count:
                break
        msg.ack.assert_awaited_once()
    finally:
        await nats_consumer.stop_nats_consumer(app)

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


@pytest.mark.asyncio
async def test_start_nats_consumer_loop_swallows_unexpected_errors(monkeypatch):
    """If process_message itself raises, the loop logs and keeps draining rather
    than crashing the task."""
    bad_msg = _FakeMsg(b"{}", stream_seq=1)
    sub = _FakeSubscription([bad_msg])
    js = _FakeJetStream(subs_by_subject={nats_consumer.NATS_SUBJECT_PATTERN: sub})
    conn = _FakeNatsConn(js)

    async def _connect(url):
        return conn

    _install_fake_nats(monkeypatch, _connect)

    async def _boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(nats_consumer, "process_message", _boom)

    app = _FakeApp()
    await nats_consumer.start_nats_consumer(app)
    try:
        # Give the loop several turns to pull the message and hit the raising
        # process_message; the except arm must swallow it.
        for _ in range(10):
            await asyncio.sleep(0.01)
        # The loop task must still be alive (error swallowed, not propagated).
        assert not app.state.nats_consumer_task.done()
    finally:
        await nats_consumer.stop_nats_consumer(app)


@pytest.mark.asyncio
async def test_start_nats_consumer_tolerates_add_stream_failure(monkeypatch):
    """add_stream failing (stream already exists) is logged and the subscribe
    still proceeds."""
    js = _FakeJetStream(add_stream_error=True)
    conn = _FakeNatsConn(js)

    async def _connect(url):
        return conn

    _install_fake_nats(monkeypatch, _connect)

    app = _FakeApp()
    await nats_consumer.start_nats_consumer(app)
    try:
        # add_stream raised for both, but both subscriptions were still created.
        assert len(js.subscribe_calls) == 2
    finally:
        await nats_consumer.stop_nats_consumer(app)


@pytest.mark.asyncio
async def test_start_nats_consumer_swallows_connect_failure(monkeypatch):
    """A broker that is unreachable is logged; the app boots without consumers."""

    async def _connect(url):
        raise RuntimeError("connection refused")

    _install_fake_nats(monkeypatch, _connect)

    app = _FakeApp()
    # Must not raise: notifications degrades to no event-driven delivery.
    await nats_consumer.start_nats_consumer(app)
    assert not hasattr(app.state, "nats_consumer_task")


@pytest.mark.asyncio
async def test_stop_nats_consumer_is_noop_when_nothing_started():
    app = _FakeApp()
    # No tasks, no connection: stop must be a clean no-op.
    await nats_consumer.stop_nats_consumer(app)


@pytest.mark.asyncio
async def test_stop_nats_consumer_swallows_close_failure():
    app = _FakeApp()

    class _BadConn:
        async def close(self):
            raise RuntimeError("already closed")

    app.state.nats = _BadConn()
    # Close failure is logged, not raised.
    await nats_consumer.stop_nats_consumer(app)
