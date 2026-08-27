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
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

    async def fetch(self, batch, timeout=None):
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
    mock_js.pull_subscribe = AsyncMock(return_value=_StubPullSub([mock_msg]))

    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)

    ensure_stream_exists_mock = AsyncMock()
    with (
        patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)),
        patch("app.services.nats_consumer.ensure_stream_exists", ensure_stream_exists_mock),
    ):
        await start_nats_consumer(mock_app)

    # Verify the stream was confirmed to exist (this consumer does not own its
    # config; see herd_common.jetstream.ensure_stream_exists)
    ensure_stream_exists_mock.assert_awaited_once_with(
        mock_js, name="HERD_RESERVATIONS", subjects=["herd.reservations.*"]
    )
    # Verify subscription was created
    mock_js.pull_subscribe.assert_called_once()
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
async def test_start_nats_consumer_stream_create_failure():
    """Stream creation failure is logged but consumer still starts."""
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
    ):
        await start_nats_consumer(mock_app)

    # Stream creation failed but subscribe still happened
    mock_js.pull_subscribe.assert_called_once()

    # Clean up
    task = mock_app.state.nats_consumer_task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


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
