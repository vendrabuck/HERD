"""Lifecycle tests for app.services.nats_consumer: start_nats_consumer and
stop_nats_consumer (the lifespan-wired connect/subscribe/background-task/close
machinery). test_nats_consumer.py already covers handle_event and
process_message thoroughly with a stubbed js/msg; this file covers the
surrounding start/stop control flow, modeled on
services/execution/tests/test_nats_consumer_full.py's pattern of
patch.dict("sys.modules", ...) to satisfy the module-local `import nats` /
`from nats.js.api import ConsumerConfig` without a real broker.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import Base
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

    async def fetch(self, batch, timeout=None):
        if not self._drained:
            self._drained = True
            return self._msgs
        await asyncio.sleep(0.02)
        raise _StubTimeoutError()


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

    # No task or connection was stashed since connect() never returned.
    assert not hasattr(mock_app.state, "nats_consumer_task") or not isinstance(
        mock_app.state.nats_consumer_task, asyncio.Task
    )


async def test_start_nats_consumer_stream_ensure_failure_still_starts_consumer():
    """ensure_stream_exists failing (e.g. transient broker error while the
    stream already exists) is logged but must not block the pull subscription
    from being set up; the stream is owned by the producing service, not this
    consumer."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = AsyncMock()
    mock_js.pull_subscribe = AsyncMock(return_value=_StubPullSub([]))
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

    ensure_stream_mock.assert_awaited_once()
    mock_js.pull_subscribe.assert_called_once()
    assert isinstance(mock_app.state.nats_consumer_task, asyncio.Task)

    mock_app.state.nats_consumer_task.cancel()
    try:
        await mock_app.state.nats_consumer_task
    except asyncio.CancelledError:
        pass


async def test_start_nats_consumer_success_processes_fetched_message():
    """A successful connect wires the stream, the pull subscription with the
    documented ConsumerConfig knobs, and a background task that drains and
    acks a fetched message end to end through the real process_message path."""
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

    mock_js = AsyncMock()
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_msg = MagicMock()
    mock_msg.data = json.dumps(
        {"event": "reservation.created", "reservation_id": str(uuid.uuid4())}
    ).encode()
    mock_msg.metadata = MagicMock(num_delivered=1)
    mock_msg.ack = AsyncMock()
    mock_msg.nak = AsyncMock()

    mock_js.pull_subscribe = AsyncMock(return_value=_StubPullSub([mock_msg]))

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    ensure_stream_mock = AsyncMock()
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", ensure_stream_mock),
        patch("app.database.AsyncSessionLocal", test_session_factory),
    ):
        await start_nats_consumer(mock_app)

        ensure_stream_mock.assert_awaited_once_with(
            mock_js, name="HERD_RESERVATIONS", subjects=["herd.reservations.*"]
        )
        mock_js.pull_subscribe.assert_called_once()
        call_kwargs = mock_js.pull_subscribe.call_args
        assert call_kwargs.args[0] == "herd.reservations.*"
        assert call_kwargs.kwargs["durable"] == "integration-webhooks-consumer"

        assert mock_app.state.nats is mock_nc
        assert isinstance(mock_app.state.nats_consumer_task, asyncio.Task)

        # Let the background loop drain the queued message. handle_event has
        # no matching subscriptions (none registered), so this exercises the
        # real process_message to handle_event to ack path end to end.
        for _ in range(50):
            if mock_msg.ack.await_count:
                break
            await asyncio.sleep(0.01)

        mock_msg.ack.assert_awaited_once()
        mock_msg.nak.assert_not_awaited()

        mock_app.state.nats_consumer_task.cancel()
        try:
            await mock_app.state.nats_consumer_task
        except asyncio.CancelledError:
            pass

    await engine.dispose()


async def test_consumer_loop_survives_fetch_exception_and_retries():
    """A non-timeout exception from fetch() (e.g. a transient broker error)
    must not kill the background loop; it sleeps and retries rather than
    propagating out of the task."""
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
    mock_js = AsyncMock()
    mock_js.pull_subscribe = AsyncMock(return_value=flaky)
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
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

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_consumer_loop_continues_after_idle_timeout():
    """A plain nats.errors.TimeoutError (no messages this fetch cycle) must be
    swallowed and the loop must keep polling, not exit or propagate."""
    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = AsyncMock()
    mock_js.pull_subscribe = AsyncMock(return_value=_StubPullSub([]))
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
        patch("app.services.nats_consumer.NATS_FETCH_TIMEOUT_SECONDS", 0.01),
    ):
        await start_nats_consumer(mock_app)
        task = mock_app.state.nats_consumer_task

        # The stub raises the timeout error on every fetch; let several idle
        # cycles pass to prove the loop's `continue` keeps it alive.
        await asyncio.sleep(0.1)
        assert not task.done()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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

    mock_js = AsyncMock()
    mock_js.pull_subscribe = AsyncMock(return_value=_StubPullSub([mock_msg]))
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(return_value=mock_nc)

    process_message_mock = AsyncMock(side_effect=RuntimeError("unexpected escape"))
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats)),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
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

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --- stop_nats_consumer -------------------------------------------------------


async def test_stop_nats_consumer_no_task_or_connection_is_a_noop():
    """Stopping before a successful start (state has neither attribute) must
    not raise."""
    mock_app = MagicMock()
    mock_app.state = MagicMock(spec=[])  # no nats_consumer_task, no nats

    await stop_nats_consumer(mock_app)


async def test_stop_nats_consumer_cancels_task_and_closes_connection():
    mock_app = MagicMock()

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

    # No nats_consumer_task attribute on the spec'd Mock.
    await stop_nats_consumer(mock_app)

    mock_nc.close.assert_awaited_once()
