"""Lifecycle tests for app.services.nats_consumer: start_nats_consumer and
stop_nats_consumer (the lifespan-wired connect/subscribe/background-task/close
machinery). test_nats_consumer.py already covers handle_event and
process_message thoroughly with a stubbed js/msg; this file covers the
surrounding start/stop control flow, modeled on
services/execution/tests/test_nats_consumer_full.py's pattern of
patch.dict("sys.modules", ...) to satisfy the module-local `import nats` /
`from nats.js.api import ConsumerConfig` without a real broker.

Issue #831 added a second durable consumer (HERD_HEALTH / herd.health.*)
started from the same `start_nats_consumer` call, so `_FakeJetStream` below
routes `pull_subscribe` by subject pattern (mirroring
services/notifications/tests/test_nats_consumer.py's `_FakeJetStream`) instead
of returning one fixed stub for every call: with two real subscriptions now
wired by every successful start, a single shared AsyncMock stub would have had
both consumer loops racing to drain the same one-shot fake subscription.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import Base
from app.services import nats_consumer
from app.services.nats_consumer import start_nats_consumer, stop_nats_consumer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class _StubTimeoutError(Exception):
    """Stands in for nats.errors.TimeoutError so the consumer loop's except
    clause (which references it by class) matches during a stubbed test."""


class _StubPullSub:
    """Fake pull subscription: fetch() yields the queued messages once, then
    raises the stub TimeoutError on every subsequent call (an idle consumer)."""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self._drained = False
        self.batch_calls = []

    async def fetch(self, batch, timeout=None):
        self.batch_calls.append(batch)
        if not self._drained:
            self._drained = True
            return self._msgs
        await asyncio.sleep(0.02)
        raise _StubTimeoutError()


class _FakeJetStream:
    """Routes pull_subscribe by subject pattern so the reservations and health
    subscriptions each get their own independent fake subscription instead of
    racing over one shared stub. `subs_by_subject` maps a subject pattern to
    the `_StubPullSub` (or equivalent) to return for it; an unlisted pattern
    gets a fresh empty (idle) `_StubPullSub([])`."""

    def __init__(self, subs_by_subject=None, add_stream_error=False):
        self._subs = dict(subs_by_subject or {})
        self.added_streams = []
        self.subscribe_calls = []
        self.published = []
        self._add_stream_error = add_stream_error

    async def add_stream(self, name, subjects):
        if self._add_stream_error:
            raise RuntimeError("stream exists / cannot update")
        self.added_streams.append((name, tuple(subjects)))

    async def pull_subscribe(self, subject_pattern, durable, config):
        self.subscribe_calls.append(
            {"subject": subject_pattern, "durable": durable, "config": config}
        )
        return self._subs.setdefault(subject_pattern, _StubPullSub([]))

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


async def _fake_ensure_stream_exists(js, *, name, subjects):
    """Stand-in for herd_common.jetstream.ensure_stream_exists: calls straight
    through to the fake JetStream's add_stream so existing assertions on
    `_FakeJetStream.added_streams` keep exercising the same call site without
    depending on the real helper's stream_info-first internals."""
    await js.add_stream(name, subjects)


def _patched_nats_modules(mock_nats):
    """Register `nats`, `nats.js`, and `nats.js.api` in sys.modules so the
    consumer's local imports (`import nats`, `from nats.js.api import
    ConsumerConfig`) resolve to test doubles instead of a real broker."""
    nats_js_api = MagicMock()
    nats_js_api.ConsumerConfig = MagicMock(return_value=MagicMock())
    nats_js = MagicMock()
    nats_js.api = nats_js_api
    mock_nats.errors = MagicMock()
    mock_nats.errors.TimeoutError = _StubTimeoutError
    return {
        "nats": mock_nats,
        "nats.js": nats_js,
        "nats.js.api": nats_js_api,
    }


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _cancel_both(mock_app):
    for attr in ("nats_consumer_task", "nats_health_consumer_task"):
        task = getattr(mock_app.state, attr, None)
        if task is not None:
            await _cancel(task)


# --- start_nats_consumer -----------------------------------------------------


async def test_start_nats_consumer_connection_failure_is_swallowed():
    """A broker that refuses the connection must not crash the lifespan; the
    service still boots and simply never delivers webhooks."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(side_effect=ConnectionRefusedError("nats unreachable"))

    with patch.dict("sys.modules", _patched_nats_modules(mock_nats)):
        await start_nats_consumer(mock_app)

    # Neither task was stashed since connect() never returned.
    assert not hasattr(mock_app.state, "nats_consumer_task") or not isinstance(
        mock_app.state.nats_consumer_task, asyncio.Task
    )
    assert not hasattr(mock_app.state, "nats_health_consumer_task") or not isinstance(
        mock_app.state.nats_health_consumer_task, asyncio.Task
    )


async def test_start_nats_consumer_stream_ensure_failure_still_starts_both_consumers():
    """ensure_stream_exists failing for a stream (e.g. transient broker error
    while the stream already exists) is logged but must not block either pull
    subscription from being set up; the streams are owned by their producing
    services, not this consumer."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = _FakeJetStream(add_stream_error=True)
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    ensure_stream_mock = AsyncMock(side_effect=RuntimeError("stream ensure failed"))
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", ensure_stream_mock),
    ):
        await start_nats_consumer(mock_app)

    assert ensure_stream_mock.await_count == 2
    assert len(mock_js.subscribe_calls) == 2
    assert isinstance(mock_app.state.nats_consumer_task, asyncio.Task)
    assert isinstance(mock_app.state.nats_health_consumer_task, asyncio.Task)

    await _cancel_both(mock_app)


async def test_start_nats_consumer_wires_both_subscriptions():
    """A successful connect wires both streams and both durable pull
    subscriptions with the documented knobs, one background task each."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = _FakeJetStream()
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
    ):
        await start_nats_consumer(mock_app)

        assert (nats_consumer.NATS_STREAM, (nats_consumer.NATS_SUBJECT_PATTERN,)) in (
            mock_js.added_streams
        )
        assert (nats_consumer.HEALTH_STREAM, (nats_consumer.HEALTH_SUBJECT_PATTERN,)) in (
            mock_js.added_streams
        )

        durables = {c["durable"] for c in mock_js.subscribe_calls}
        assert durables == {nats_consumer.NATS_DURABLE, nats_consumer.HEALTH_DURABLE}
        subjects = {c["subject"] for c in mock_js.subscribe_calls}
        assert subjects == {
            nats_consumer.NATS_SUBJECT_PATTERN,
            nats_consumer.HEALTH_SUBJECT_PATTERN,
        }

        assert mock_app.state.nats is mock_nc
        assert isinstance(mock_app.state.nats_consumer_task, asyncio.Task)
        assert isinstance(mock_app.state.nats_health_consumer_task, asyncio.Task)
        assert not mock_app.state.nats_consumer_task.done()
        assert not mock_app.state.nats_health_consumer_task.done()

        await _cancel_both(mock_app)


async def test_start_nats_consumer_success_processes_fetched_reservation_message():
    """The reservations subscription drains and acks a fetched message end to
    end through the real process_message path."""
    # start_nats_consumer imports AsyncSessionLocal from app.database inline
    # and hands it straight to the real handle_event, so the consumer loop
    # needs a real (in-memory) webhook schema to query against, not a bare
    # MagicMock session factory.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    test_session_factory = async_sessionmaker(engine, expire_on_commit=False)

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_msg = MagicMock()
    mock_msg.data = json.dumps(
        {"event": "reservation.created", "reservation_id": str(uuid.uuid4())}
    ).encode()
    mock_msg.metadata = MagicMock(num_delivered=1)
    mock_msg.ack = AsyncMock()
    mock_msg.nak = AsyncMock()

    mock_js = _FakeJetStream(
        subs_by_subject={nats_consumer.NATS_SUBJECT_PATTERN: _StubPullSub([mock_msg])}
    )
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
        patch("app.database.AsyncSessionLocal", test_session_factory),
    ):
        await start_nats_consumer(mock_app)

        # Let the background loop drain the queued message. handle_event has
        # no matching subscriptions (none registered), so this exercises the
        # real process_message to handle_event to ack path end to end.
        for _ in range(50):
            if mock_msg.ack.await_count:
                break
            await asyncio.sleep(0.01)

        mock_msg.ack.assert_awaited_once()
        mock_msg.nak.assert_not_awaited()

        await _cancel_both(mock_app)

    await engine.dispose()


async def test_start_nats_consumer_success_processes_fetched_health_message():
    """Issue #831: the health subscription drains and acks a fetched
    device.health_transition message through the same process_message path,
    independently of the reservations subscription."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    test_session_factory = async_sessionmaker(engine, expire_on_commit=False)

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_msg = MagicMock()
    mock_msg.data = json.dumps(
        {
            "event": "device.health_transition",
            "event_id": str(uuid.uuid4()),
            "device_id": str(uuid.uuid4()),
        }
    ).encode()
    mock_msg.metadata = MagicMock(num_delivered=1)
    mock_msg.ack = AsyncMock()
    mock_msg.nak = AsyncMock()

    mock_js = _FakeJetStream(
        subs_by_subject={nats_consumer.HEALTH_SUBJECT_PATTERN: _StubPullSub([mock_msg])}
    )
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
        patch("app.database.AsyncSessionLocal", test_session_factory),
    ):
        await start_nats_consumer(mock_app)

        for _ in range(50):
            if mock_msg.ack.await_count:
                break
            await asyncio.sleep(0.01)

        mock_msg.ack.assert_awaited_once()
        mock_msg.nak.assert_not_awaited()

        await _cancel_both(mock_app)

    await engine.dispose()


async def test_consumer_loop_fetches_one_message_at_a_time_on_both_subscriptions():
    """Pins the issue #648 fix on BOTH subscriptions: the consumer loop must
    call fetch with batch == 1 on every call, never a larger batch, for the
    reservations subscription and the health subscription alike (both run
    the same shared `_consumer_loop` closure).

    nats-py's multi-message fetch (`_fetch_n`) holds already-received messages
    until the batch fills or the fetch's deadline expires, so a batch of 10 with
    fewer than 10 events in flight added up to NATS_FETCH_TIMEOUT_SECONDS of
    latency to every event before it was processed (issue #648). The batch=1
    path (`_fetch_one`) drains the client's pending queue and returns the first
    processable message immediately. If a future change reintroduces a batch >
    1 here, it must first confront why #648 moved off it."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    test_session_factory = async_sessionmaker(engine, expire_on_commit=False)

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    res_msg = MagicMock()
    res_msg.data = json.dumps(
        {"event": "reservation.created", "reservation_id": str(uuid.uuid4())}
    ).encode()
    res_msg.metadata = MagicMock(num_delivered=1)
    res_msg.ack = AsyncMock()
    res_msg.nak = AsyncMock()

    health_msg = MagicMock()
    health_msg.data = json.dumps(
        {"event": "device.health_transition", "event_id": str(uuid.uuid4())}
    ).encode()
    health_msg.metadata = MagicMock(num_delivered=1)
    health_msg.ack = AsyncMock()
    health_msg.nak = AsyncMock()

    res_sub = _StubPullSub([res_msg])
    health_sub = _StubPullSub([health_msg])
    mock_js = _FakeJetStream(
        subs_by_subject={
            nats_consumer.NATS_SUBJECT_PATTERN: res_sub,
            nats_consumer.HEALTH_SUBJECT_PATTERN: health_sub,
        }
    )
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
        patch("app.database.AsyncSessionLocal", test_session_factory),
        patch("app.services.nats_consumer.NATS_FETCH_TIMEOUT_SECONDS", 0.01),
    ):
        await start_nats_consumer(mock_app)

        for _ in range(50):
            if res_msg.ack.await_count and health_msg.ack.await_count:
                break
            await asyncio.sleep(0.01)
        res_msg.ack.assert_awaited_once()
        health_msg.ack.assert_awaited_once()
        # A few more idle-fetch cycles so more than one call is recorded on
        # each subscription.
        await asyncio.sleep(0.05)

        await _cancel_both(mock_app)

    await engine.dispose()

    assert res_sub.batch_calls, "reservations fetch was never called"
    assert health_sub.batch_calls, "health fetch was never called"
    assert all(batch == 1 for batch in res_sub.batch_calls)
    assert all(batch == 1 for batch in health_sub.batch_calls)


async def test_consumer_loop_survives_fetch_exception_and_retries():
    """A non-timeout exception from fetch() (e.g. a transient broker error)
    must not kill the background loop; it sleeps and retries rather than
    propagating out of the task. Exercised on the reservations subscription;
    both subscriptions share the identical closure body."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    class _FlakyPullSub:
        def __init__(self):
            self.calls = 0

        async def fetch(self, batch, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient broker hiccup")
            await asyncio.sleep(0.02)
            raise _StubTimeoutError()

    flaky = _FlakyPullSub()
    mock_js = _FakeJetStream(subs_by_subject={nats_consumer.NATS_SUBJECT_PATTERN: flaky})
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
        patch("app.services.nats_consumer.NATS_FETCH_TIMEOUT_SECONDS", 0.01),
    ):
        await start_nats_consumer(mock_app)
        task = mock_app.state.nats_consumer_task

        for _ in range(100):
            if flaky.calls >= 2:
                break
            await asyncio.sleep(0.01)

        assert flaky.calls >= 2  # the loop survived the first exception
        assert not task.done()  # and is still running, not crashed

        await _cancel_both(mock_app)


async def test_consumer_loop_continues_after_idle_timeout():
    """A plain nats.errors.TimeoutError (no messages this fetch cycle) must be
    swallowed and the loop must keep polling, not exit or propagate. Both
    subscriptions are idle here (no queued messages for either subject)."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = _FakeJetStream()
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
        patch("app.services.nats_consumer.NATS_FETCH_TIMEOUT_SECONDS", 0.01),
    ):
        await start_nats_consumer(mock_app)

        # The stub raises the timeout error on every fetch; let several idle
        # cycles pass to prove the loop's `continue` keeps both tasks alive.
        await asyncio.sleep(0.1)
        assert not mock_app.state.nats_consumer_task.done()
        assert not mock_app.state.nats_health_consumer_task.done()

        await _cancel_both(mock_app)


async def test_consumer_loop_logs_and_continues_on_process_message_exception():
    """process_message itself is expected never to raise (it maps every
    outcome to ack/nak internally), but the loop's own try/except around the
    call is a defensive backstop: an unexpected escape must be logged and
    swallowed rather than killing the background task."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_msg = MagicMock()
    mock_msg.data = json.dumps({"event": "reservation.created"}).encode()
    mock_msg.metadata = MagicMock(num_delivered=1)
    mock_msg.ack = AsyncMock()
    mock_msg.nak = AsyncMock()

    mock_js = _FakeJetStream(
        subs_by_subject={nats_consumer.NATS_SUBJECT_PATTERN: _StubPullSub([mock_msg])}
    )
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    process_message_mock = AsyncMock(side_effect=RuntimeError("unexpected escape"))
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
        patch("app.services.nats_consumer.process_message", process_message_mock),
    ):
        await start_nats_consumer(mock_app)
        task = mock_app.state.nats_consumer_task

        for _ in range(50):
            if process_message_mock.await_count:
                break
            await asyncio.sleep(0.01)

        process_message_mock.assert_awaited()
        # The task survived the exception raised inside the loop body.
        await asyncio.sleep(0.02)
        assert not task.done()

        await _cancel_both(mock_app)


async def test_consumer_loop_passes_health_dlq_subject_for_health_subscription():
    """The health subscription's consumer loop must call process_message with
    dlq_subject=HEALTH_DLQ_SUBJECT, not the reservations DLQ subject, so a
    poison health message routes to herd.health.dlq.integration."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    health_msg = MagicMock()
    health_msg.data = b"not-json"
    health_msg.metadata = MagicMock(num_delivered=1)
    health_msg.ack = AsyncMock()
    health_msg.nak = AsyncMock()

    mock_js = _FakeJetStream(
        subs_by_subject={nats_consumer.HEALTH_SUBJECT_PATTERN: _StubPullSub([health_msg])}
    )
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", _fake_ensure_stream_exists),
    ):
        await start_nats_consumer(mock_app)

        for _ in range(50):
            if mock_js.published:
                break
            await asyncio.sleep(0.01)

        await _cancel_both(mock_app)

    assert mock_js.published, "the poison health message was never DLQ'd"
    dlq_subject, dlq_payload = mock_js.published[0]
    assert dlq_subject == nats_consumer.HEALTH_DLQ_SUBJECT
    assert dlq_payload == b"not-json"
    health_msg.ack.assert_awaited_once()
    health_msg.nak.assert_not_awaited()


# --- stop_nats_consumer -------------------------------------------------------


async def test_stop_nats_consumer_no_task_or_connection_is_a_noop():
    """Stopping before a successful start (state has neither attribute) must
    not raise."""
    mock_app = MagicMock()
    mock_app.state = MagicMock(spec=[])  # no consumer tasks, no nats

    await stop_nats_consumer(mock_app)


async def test_stop_nats_consumer_cancels_both_tasks_and_closes_connection():
    mock_app = MagicMock()

    async def _forever():
        await asyncio.sleep(3600)

    reservations_task = asyncio.create_task(_forever())
    health_task = asyncio.create_task(_forever())
    mock_nc = AsyncMock()

    mock_app.state.nats_consumer_task = reservations_task
    mock_app.state.nats_health_consumer_task = health_task
    mock_app.state.nats = mock_nc

    await stop_nats_consumer(mock_app)

    assert reservations_task.cancelled()
    assert health_task.cancelled()
    mock_nc.close.assert_awaited_once()


async def test_stop_nats_consumer_only_reservations_task_present_is_fine():
    """A partial start (e.g. the health subscription's pull_subscribe raised
    before its task was stashed) must still stop cleanly; stop_nats_consumer
    does not assume both task attributes exist."""
    mock_app = MagicMock()
    mock_app.state = MagicMock(spec=["nats_consumer_task", "nats"])

    async def _forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    mock_nc = AsyncMock()
    mock_app.state.nats_consumer_task = task
    mock_app.state.nats = mock_nc

    await stop_nats_consumer(mock_app)

    assert task.cancelled()
    mock_nc.close.assert_awaited_once()


async def test_stop_nats_consumer_close_failure_is_swallowed():
    """A close() failure (e.g. the socket already dropped) is logged, not
    raised, so shutdown always completes."""
    mock_app = MagicMock()
    mock_app.state = MagicMock(spec=["nats"])

    mock_nc = AsyncMock()
    mock_nc.close = AsyncMock(side_effect=RuntimeError("close failed"))
    mock_app.state.nats = mock_nc

    # No consumer task attributes on the spec'd Mock.
    await stop_nats_consumer(mock_app)

    mock_nc.close.assert_awaited_once()
