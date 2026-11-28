"""Tests for nats_consumer.py: consumer start/stop lifecycle and the
redelivery idempotency guard (action_already_succeeded).

The device-set L1/L2/L3 provisioning tests that used to live here were retired
with the legacy resolvers/executors in ADR 0009 phase 7; the surviving wiring
coverage is the fork-driven reconcile suite (test_nats_consumer_wiring_changed,
test_nats_consumer_l2_reconcile, test_nats_consumer_l3_reconcile). The fetch
DEVICE/TEMPLATE transient-vs-404 helper tests are covered in
test_nats_consumer.py.
"""

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# app.main calls setup_logging at import, which clears the root handlers; import
# it here, before caplog installs its handler, not inside a test.
from app import main as main_module
from app.database import Base
from app.models.driver_cache import DriverCache  # noqa: F401 (register with Base)
from app.models.execution_run import ExecutionRun
from app.models.l1_connection_assignment import L1ConnectionAssignment  # noqa: F401 (register)
from app.services.nats_consumer import stop_nats_consumer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


SWITCH_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


# --- consumer start/stop lifecycle ---


class _StubTimeoutError(Exception):
    pass


class _StubPullSub:
    """Fake pull subscription: fetch() returns the queued messages once, then
    raises TimeoutError after yielding (mirrors an idle pull consumer)."""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self._drained = False
        self.batch_calls = []

    async def fetch(self, batch, timeout=None):
        self.batch_calls.append(batch)
        if not self._drained and self._msgs:
            self._drained = True
            return self._msgs
        await asyncio.sleep(0.02)
        raise _StubTimeoutError()


def _patched_nats_modules(mock_nats):
    """Register mocks for `nats`, `nats.js`, and `nats.js.api` so the consumer's
    `from nats.js.api import ConsumerConfig` import resolves during tests."""
    nats_js_api = MagicMock()
    nats_js_api.ConsumerConfig = MagicMock(return_value=MagicMock())
    nats_js = MagicMock()
    nats_js.api = nats_js_api
    # The pull loop catches nats.errors.TimeoutError; it must be a real class.
    mock_nats.errors = MagicMock()
    mock_nats.errors.TimeoutError = _StubTimeoutError
    return {
        "nats": mock_nats,
        "nats.js": nats_js,
        "nats.js.api": nats_js_api,
    }


@pytest.mark.asyncio
async def test_start_nats_consumer_connection_failure():
    """NATS connection failure logs warning but does not raise."""
    from app.services.nats_consumer import start_nats_consumer

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock(side_effect=Exception("NATS unreachable"))

    with patch.dict("sys.modules", _patched_nats_modules(mock_nats)):
        await start_nats_consumer(mock_app)


@pytest.mark.asyncio
async def test_stop_nats_consumer_no_task():
    """Stop when no consumer task exists."""
    mock_app = MagicMock()
    mock_app.state = MagicMock(spec=[])  # no nats_consumer_task attribute
    await stop_nats_consumer(mock_app)


@pytest.mark.asyncio
async def test_stop_nats_consumer_with_task_and_connection():
    """Stop cancels the task and closes the connection."""
    mock_app = MagicMock()

    async def _forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    mock_nc = AsyncMock()

    mock_app.state.nats_consumer_task = task
    mock_app.state.nats = mock_nc

    await stop_nats_consumer(mock_app)
    assert task.cancelled()
    mock_nc.close.assert_called_once()


@pytest.mark.asyncio
async def test_stop_nats_consumer_close_error():
    """Close error is logged but not raised."""
    mock_app = MagicMock()
    mock_app.state = MagicMock(spec=[])

    mock_nc = AsyncMock()
    mock_nc.close.side_effect = Exception("close failed")
    mock_app.state.nats = mock_nc

    type(mock_app.state).nats_consumer_task = None

    await stop_nats_consumer(mock_app)


@pytest.mark.asyncio
async def test_start_nats_consumer_success():
    """Successful NATS connection sets up stream, subscription, and consumer task."""
    from app.services.nats_consumer import start_nats_consumer

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    # Build mock NATS client and JetStream
    # nc.jetstream() is a sync call that returns a JetStream context
    mock_js = AsyncMock()
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    # Subscription that yields one message then stops
    mock_msg = MagicMock()
    mock_msg.data = json.dumps(
        {
            "event": "reservation.created",
            "reservation_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
        }
    ).encode()
    mock_msg.ack = AsyncMock()

    # Pull subscription: fetch returns the message once, then times out.
    # Every add_consumer and pull_subscribe as (operation, durable), in the order
    # they happened. The durable add_consumer creates is read from the config AT
    # the call: ensure_consumer sets it there, and the config object is a mock any
    # later write would change.
    durable_calls = []

    async def add_consumer(stream, config):
        durable_calls.append(("add_consumer", config.durable_name, stream))

    async def pull_subscribe(subject, durable, config):
        durable_calls.append(("pull_subscribe", durable, None))
        return _StubPullSub([mock_msg])

    mock_js.add_consumer = AsyncMock(side_effect=add_consumer)
    mock_js.pull_subscribe = AsyncMock(side_effect=pull_subscribe)

    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)

    patched_modules = _patched_nats_modules(mock_nats_module)
    ensure_stream_exists_mock = AsyncMock()
    with (
        patch.dict("sys.modules", patched_modules),
        patch("app.services.nats_consumer.ensure_stream_exists", ensure_stream_exists_mock),
    ):
        started = await start_nats_consumer(mock_app)

    # The consumer is running, and the start says so (issue #1592).
    assert started is True
    # Verify the stream was confirmed to exist (this consumer does not own its
    # config; see herd_common.jetstream.ensure_stream_exists)
    ensure_stream_exists_mock.assert_awaited_once_with(
        mock_js, name="HERD_RESERVATIONS", subjects=["herd.reservations.*"]
    )
    # issue #895: the ConsumerConfig built for this durable carries no `backoff`,
    # and the durable is created-or-updated via add_consumer BEFORE pull_subscribe
    # binds to it (herd_common.jetstream.ensure_consumer), not left to
    # pull_subscribe's own create-if-missing-else-bind path.
    consumer_config_kwargs = patched_modules["nats.js.api"].ConsumerConfig.call_args.kwargs
    assert "backoff" not in consumer_config_kwargs
    assert consumer_config_kwargs["ack_wait"] == 30
    mock_js.add_consumer.assert_awaited_once()
    # Verify subscription was created
    mock_js.pull_subscribe.assert_called_once()
    # The pairing, read from ONE ordered log BY DURABLE NAME, as the notifications
    # and integration start tests read theirs: the durable pull_subscribe binds must
    # be the one add_consumer created or updated, and created first. Two separate
    # "was called once" checks hold whichever came first, a call-order check alone
    # holds when ensure_consumer created some other durable, and pull_subscribe
    # ahead of add_consumer binds to the durable's stale config.
    assert [op for op, durable, _ in durable_calls if durable == "execution-consumer"] == [
        "add_consumer",
        "pull_subscribe",
    ], durable_calls
    # The durable is created on its own stream, asserted as the whole map.
    assert {d: s for op, d, s in durable_calls if op == "add_consumer"} == {
        "execution-consumer": "HERD_RESERVATIONS"
    }
    # Verify consumer task was stored
    assert hasattr(mock_app.state, "nats_consumer_task")

    # Let the consumer task process the message
    task = mock_app.state.nats_consumer_task
    await asyncio.sleep(0.2)
    # Clean up
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_nats_consumer_loop_fetches_one_message_at_a_time():
    """Pins the issue #648 fix: the consumer loop must call fetch with batch == 1
    on every call, never a larger batch.

    nats-py's multi-message fetch (`_fetch_n`) holds already-received messages
    until the batch fills or the fetch's deadline expires, so a batch of 10 with
    fewer than 10 events in flight added up to NATS_FETCH_TIMEOUT_SECONDS of
    latency to every event before it was processed (measured on CI: a 4.995s
    hold stacked with a 4.74s outbox tick against a 10s test budget). The
    batch=1 path (`_fetch_one`) drains the client's pending queue and returns
    the first processable message immediately. If a future change reintroduces
    a batch > 1 here, it must first confront why #648 moved off it."""
    from app.services.nats_consumer import start_nats_consumer

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = AsyncMock()
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_msg = MagicMock()
    mock_msg.data = json.dumps(
        {
            "event": "reservation.created",
            "reservation_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "device_ids": [],
        }
    ).encode()
    mock_msg.ack = AsyncMock()

    stub_sub = _StubPullSub([mock_msg])
    mock_js.pull_subscribe = AsyncMock(return_value=stub_sub)

    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)

    ensure_stream_exists_mock = AsyncMock()
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)),
        patch("app.services.nats_consumer.ensure_stream_exists", ensure_stream_exists_mock),
    ):
        await start_nats_consumer(mock_app)

    task = mock_app.state.nats_consumer_task
    # Give the loop several turns: one that returns the message, and a few
    # idle-timeout cycles after, so more than one call is recorded.
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert stub_sub.batch_calls, "fetch was never called"
    assert all(batch == 1 for batch in stub_sub.batch_calls)


@pytest.mark.asyncio
async def test_start_nats_consumer_stream_create_failure(caplog):
    """Stream creation failure is logged but consumer still starts. Issue #1418
    pins the line: the class name only, and that the setup goes ahead."""
    from app.services.nats_consumer import start_nats_consumer

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    mock_js = AsyncMock()
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    # Pull subscription with no messages (fetch always times out).
    mock_js.pull_subscribe = AsyncMock(return_value=_StubPullSub([]))

    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)

    ensure_stream_exists_mock = AsyncMock(side_effect=Exception("stream error"))
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)),
        patch("app.services.nats_consumer.ensure_stream_exists", ensure_stream_exists_mock),
        caplog.at_level(logging.WARNING, logger=_LOGGER),
    ):
        await start_nats_consumer(mock_app)

    # Stream creation failed but subscribe still happened
    mock_js.pull_subscribe.assert_called_once()
    assert _warnings(caplog) == [
        "JetStream stream HERD_RESERVATIONS could not be checked or created (Exception); "
        "the consumer setup on it goes ahead, and fails if the stream is missing"
    ]

    # Clean up
    task = mock_app.state.nats_consumer_task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


_LOGGER = "app.services.nats_consumer"
# Exception text a broker error could carry; no warning may repeat it.
_EXC_TEXT = "nats://herd-user:tok3n-value@broker:4222 replied with an error"
_SETUP_FAILURE_LINE = (
    "NATS consumer setup failed for durable execution-consumer on stream "
    "HERD_RESERVATIONS (RuntimeError); not running: execution-consumer; still "
    "running: none. The connection is kept, but nothing retries the setup, so a "
    "consumer that is not running starts only when the service is restarted; events "
    "published to its stream meanwhile wait there, kept up to the stream's max age, "
    "and are processed after that restart"
)


def _records(caplog):
    return [r for r in caplog.records if r.name == _LOGGER]


def _warnings(caplog):
    return [r.getMessage() for r in _records(caplog) if r.levelno == logging.WARNING]


def _connect_then_fail_setup(failing_call: str):
    """A connect that succeeds to a JetStream whose `failing_call`
    (`add_consumer`, reached through ensure_consumer, or `pull_subscribe`)
    raises (issue #1418)."""
    mock_js = AsyncMock()
    getattr(mock_js, failing_call).side_effect = RuntimeError(_EXC_TEXT)
    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)
    return mock_nats_module, mock_nc


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_call", ["add_consumer", "pull_subscribe"])
async def test_setup_failure_after_a_good_connect_is_not_called_a_connect_failure(
    caplog, failing_call
):
    """Issue #1418: the connect succeeded and the durable's setup raised. The
    warning names the durable and its stream, keeps the connection, and carries
    no exception text or traceback; no consumer task is started."""
    from app.services.nats_consumer import start_nats_consumer

    mock_nats_module, mock_nc = _connect_then_fail_setup(failing_call)
    app = SimpleNamespace(state=SimpleNamespace())
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
        caplog.at_level(logging.INFO, logger=_LOGGER),
    ):
        started = await start_nats_consumer(app)

    # The failure is reported to the caller too, for the schema gate (issue #1592).
    assert started is False
    assert _warnings(caplog) == [_SETUP_FAILURE_LINE]
    # No "NATS consumer started" line, and no traceback on the warning.
    assert [r.levelno for r in _records(caplog)] == [logging.WARNING]
    assert _records(caplog)[0].exc_info is None
    assert "tok3n-value" not in caplog.text
    assert app.state.nats is mock_nc
    # "The connection is kept": the failed setup neither closes nor drains it.
    mock_nc.close.assert_not_awaited()
    mock_nc.drain.assert_not_awaited()
    assert not hasattr(app.state, "nats_consumer_task")


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["connect", "setup"])
async def test_a_cancelled_start_propagates_the_cancellation(caplog, step):
    """Issue #1418 review: the start catches `Exception` only, so a
    CancelledError raised by the connect or by a setup step leaves
    start_nats_consumer instead of being logged as a failed start."""
    from app.services.nats_consumer import start_nats_consumer

    mock_nats_module, _ = _connect_then_fail_setup("add_consumer")
    mock_js = mock_nats_module.connect.return_value.jetstream.return_value
    mock_js.add_consumer.side_effect = asyncio.CancelledError()
    if step == "connect":
        mock_nats_module.connect.side_effect = asyncio.CancelledError()
    app = SimpleNamespace(state=SimpleNamespace())
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
        caplog.at_level(logging.WARNING, logger=_LOGGER),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_nats_consumer(app)

    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_setup_failure_keeps_the_connection_for_the_stream_ensures(caplog, monkeypatch):
    """Issue #1418, the lifespan's consumer step: after a setup failure the
    connection is kept, so `_start_consumer_and_streams` still ensures
    HERD_HEALTH and HERD_DLQ on it (the outbox relay reads the same
    app.state.nats), which is what the warning's "the connection is kept"
    promises."""
    mock_nats_module, mock_nc = _connect_then_fail_setup("add_consumer")
    ensure_stream_mock = AsyncMock()
    monkeypatch.setattr(main_module, "ensure_stream", ensure_stream_mock)
    app = SimpleNamespace(state=SimpleNamespace())
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
        caplog.at_level(logging.WARNING, logger=_LOGGER),
    ):
        await main_module._start_consumer_and_streams(app)

    assert _warnings(caplog) == [_SETUP_FAILURE_LINE]
    assert app.state.nats is mock_nc
    assert [call.kwargs["name"] for call in ensure_stream_mock.await_args_list] == [
        "HERD_HEALTH",
        "HERD_DLQ",
    ]


# --- NATS-redelivery idempotency guard (issue #133) ---


async def _seed_success_run(action, port_a=None, port_b=None, *, dedupe_key, device_id):
    """Persist a SUCCESS ExecutionRun standing in for a prior, acked delivery."""
    async with TestSessionLocal() as session:
        session.add(
            ExecutionRun(
                device_id=uuid.UUID(device_id),
                driver_id=uuid.UUID(DRIVER_ID),
                driver_sha256="sha256abc",
                action=action,
                status="SUCCESS",
                user_id=uuid.UUID(USER_ID),
                input_params={},
                port_a=port_a,
                port_b=port_b,
                dedupe_key=dedupe_key,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_action_already_succeeded_matches_only_exact_success():
    """The guard matches a SUCCESS run for the same (key, device, action, ports)
    and ignores a null key, a different key, and a non-SUCCESS run."""
    from app.services.execution_service import action_already_succeeded

    key = "HERD_RESERVATIONS:7"
    await _seed_success_run("connect_ports", "0/0/1", "0/0/2", dedupe_key=key, device_id=SWITCH_ID)

    async with TestSessionLocal() as db:
        dev = uuid.UUID(SWITCH_ID)
        # Exact match.
        assert await action_already_succeeded(db, key, dev, "connect_ports", "0/0/1", "0/0/2")
        # Null key never matches (preserves the un-keyed at-least-once path).
        assert not await action_already_succeeded(db, None, dev, "connect_ports", "0/0/1", "0/0/2")
        # Different source message.
        assert not await action_already_succeeded(
            db, "HERD_RESERVATIONS:8", dev, "connect_ports", "0/0/1", "0/0/2"
        )
        # Different ports.
        assert not await action_already_succeeded(db, key, dev, "connect_ports", "0/0/9", "0/0/2")


@pytest.mark.asyncio
async def test_action_already_succeeded_ignores_failed_run():
    """A FAILED prior attempt must NOT suppress a retry."""
    from app.services.execution_service import action_already_succeeded

    key = "HERD_RESERVATIONS:9"
    async with TestSessionLocal() as session:
        session.add(
            ExecutionRun(
                device_id=uuid.UUID(SWITCH_ID),
                driver_id=uuid.UUID(DRIVER_ID),
                driver_sha256="sha256abc",
                action="connect_ports",
                status="FAILED",
                user_id=uuid.UUID(USER_ID),
                input_params={},
                port_a="0/0/1",
                port_b="0/0/2",
                dedupe_key=key,
            )
        )
        await session.commit()

    async with TestSessionLocal() as db:
        assert not await action_already_succeeded(
            db, key, uuid.UUID(SWITCH_ID), "connect_ports", "0/0/1", "0/0/2"
        )
