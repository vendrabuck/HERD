"""Tests for nats_consumer.py: full event handling flow, start/stop, fetch helpers."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models.driver_cache import DriverCache  # noqa: F401 (register with Base)
from app.models.execution_run import ExecutionRun  # noqa: F401 (register with Base)
from app.services.nats_consumer import (
    TransientUpstreamError,
    _fetch_connections_for_device,
    _fetch_device,
    _fetch_template,
    handle_reservation_event,
    stop_nats_consumer,
)
from sqlalchemy import select
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


def _db_session_factory():
    """Returns a callable that produces async context managers (matching NATS consumer pattern).

    The nats consumer code does: async with get_db_session() as db:
    So get_db_session() must directly return an async context manager (not a coroutine).
    """

    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


SWITCH_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())

SWITCH_DATA = {
    "id": SWITCH_ID,
    "name": "L1-Switch",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}

TEMPLATE_DATA = {
    "id": "tmpl-1",
    "name": "L1 Template",
    "sections": [{"name": "Net", "fields": [{"key": "ip", "type": "string"}]}],
}

SUCCESS_RESULT = {
    "success": True,
    "output": {"result": True},
    "error": None,
    "duration_ms": 50,
}

FAILURE_RESULT = {
    "success": False,
    "output": None,
    "error": "Connection refused",
    "duration_ms": 20,
}


def _as_naive_utc(dt):
    """Normalize a datetime to naive-UTC for ordering comparisons.

    SQLite roundtrips datetimes as naive while Postgres keeps them tz-aware, so
    tests must not assume one or the other. Aware values are converted to UTC
    then stripped of tzinfo; naive values are assumed to already be UTC.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _make_event(event_type="reservation.created"):
    return {
        "event": event_type,
        "reservation_id": RES_ID,
        "user_id": USER_ID,
        "device_ids": ["dut-1", "dut-2"],
    }


OPS = [
    {"switch_device_id": SWITCH_ID, "switch_port_a": "0/0/1", "switch_port_b": "0/0/2"},
]


# --- _fetch_connections_for_device ---


@pytest.mark.asyncio
async def test_fetch_connections_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"items": [{"id": "conn-1"}]}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_connections_for_device("device-1")
    assert len(result) == 1


@pytest.mark.asyncio
async def test_fetch_connections_5xx_raises_transient():
    """A 5xx is a transient upstream failure: it must raise (so the message
    NAKs and retries), not be swallowed as an empty connection list."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(TransientUpstreamError):
            await _fetch_connections_for_device("device-1")


@pytest.mark.asyncio
async def test_fetch_connections_sub_500_returns_empty():
    """A non-200 below 500 (e.g. 404) is treated as no connections, not transient."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_connections_for_device("device-1")
    assert result == []


@pytest.mark.asyncio
async def test_fetch_connections_list_response():
    """Response is a plain list (not dict with items key)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"id": "conn-1"}, {"id": "conn-2"}]

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_connections_for_device("device-1")
    assert len(result) == 2


# --- _fetch_device ---


@pytest.mark.asyncio
async def test_fetch_device_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "d1", "name": "dev"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_device("d1")
    assert result["name"] == "dev"


@pytest.mark.asyncio
async def test_fetch_device_not_found():
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_device("d1")
    assert result is None


# --- _fetch_template ---


@pytest.mark.asyncio
async def test_fetch_template_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "t1", "name": "tmpl"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_template("t1")
    assert result["name"] == "tmpl"


@pytest.mark.asyncio
async def test_fetch_template_not_found():
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.nats_consumer.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_template("t1")
    assert result is None


# --- handle_reservation_event full flow ---

# The nats consumer code does:
#   async with get_db_session() as db:
# where get_db_session is an async function returning an async context manager.
# We need our factory to match that contract.


def _event_patches(execute_fn=None, load_driver_fn=None, fetch_device_fn=None, ops=None):
    """Build a list of patches for handle_reservation_event tests."""
    if ops is None:
        ops = OPS
    if fetch_device_fn is None:
        fetch_device_fn = AsyncMock(return_value=SWITCH_DATA)
    if load_driver_fn is None:
        load_driver_fn = AsyncMock(return_value="/tmp/driver")
    if execute_fn is None:

        def execute_fn(*a, **kw):
            return SUCCESS_RESULT

    patches = [
        patch(
            "app.services.nats_consumer._resolve_l1_switch_operations",
            new=AsyncMock(return_value=ops),
        ),
        patch("app.services.nats_consumer._fetch_device", new=fetch_device_fn),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=load_driver_fn),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        # Mock L2 operations to avoid HTTP calls in L1-focused tests
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new=AsyncMock(),
        ),
    ]
    return patches


@pytest.mark.asyncio
async def test_handle_event_full_success():
    """Full flow: login, connect_ports, logout all succeed."""
    event = _make_event("reservation.created")
    patches = _event_patches()
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_handle_event_l1_started_at_captured_before_driver_call():
    """Regression: started_at must reflect when each driver call began, not when
    it returned. execute_driver_method sleeps and records, per action, the
    wall-clock instant it was entered; each persisted run's started_at must be at
    or before its own action's entry instant (the timestamp is taken just before
    the call), and never after completed_at. With the old post-call ordering,
    started_at would land after that action's sleep and thus after its entry
    instant, so the per-action comparison fails. A single shared timestamp would
    not catch a regression on an earlier call (login or port op), since the final
    call (logout) would mask it; keying by action is what makes this tight."""
    event = _make_event("reservation.created")
    entry_ts: dict[str, datetime] = {}

    def slow_execute(driver_path, action, context, **kwargs):
        entry_ts[action] = datetime.now(timezone.utc)
        time.sleep(0.02)
        return SUCCESS_RESULT

    patches = _event_patches(execute_fn=slow_execute)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()

    async with TestSessionLocal() as session:
        runs = list((await session.execute(select(ExecutionRun))).scalars().all())

    assert runs, "expected at least one persisted ExecutionRun"
    actions_seen = {run.action for run in runs}
    # The single-op L1 flow runs login, the port action, and logout.
    assert {"login", "logout"} <= actions_seen
    for run in runs:
        assert run.started_at is not None
        assert run.completed_at is not None
        started = _as_naive_utc(run.started_at)
        # started before finished
        assert started <= _as_naive_utc(run.completed_at)
        # started captured before this action's driver call entered its sleep
        assert run.action in entry_ts
        assert started <= _as_naive_utc(entry_ts[run.action])


@pytest.mark.asyncio
async def test_l2_provision_started_at_captured_before_driver_call():
    """L2 counterpart of the started_at regression: every persisted run on the
    provision path (login, create_vlan, add_to_vlan, logout) must have started_at
    captured before its own action's driver call entered its sleep, and never
    after completed_at. Keying the recorded entry time by action (rather than a
    single shared slot) keeps the check tight against a regression on any one of
    the four calls."""
    from app.services.nats_consumer import _execute_l2_switch_operations

    l2_switch_id = str(uuid.uuid4())
    l2_switch_data = {**SWITCH_DATA, "id": l2_switch_id, "connection_type": "Layer 2 Switch"}
    ops = [{"switch_device_id": l2_switch_id, "switch_port": "eth1", "tag": "tagged"}]
    res_id = str(uuid.uuid4())

    entry_ts: dict[str, datetime] = {}

    def slow_execute(driver_path, action, context, **kwargs):
        entry_ts[action] = datetime.now(timezone.utc)
        time.sleep(0.02)
        return SUCCESS_RESULT

    async def _assign(operations, reservation_id, get_db_session):
        for op in operations:
            op["vlan_id"] = 100
        return operations

    patches = [
        patch(
            "app.services.nats_consumer._resolve_l2_switch_operations",
            new=AsyncMock(return_value=ops),
        ),
        patch("app.services.nats_consumer._assign_vlans_to_operations", new=_assign),
        patch(
            "app.services.nats_consumer._fetch_device", new=AsyncMock(return_value=l2_switch_data)
        ),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=slow_execute),
    ]
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dev-1"],
            l2_action="provision",
            reservation_id=res_id,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    async with TestSessionLocal() as session:
        runs = list((await session.execute(select(ExecutionRun))).scalars().all())

    assert runs, "expected at least one persisted ExecutionRun"
    actions_seen = {run.action for run in runs}
    assert {"login", "create_vlan", "add_to_vlan", "logout"} <= actions_seen
    for run in runs:
        assert run.started_at is not None
        assert run.completed_at is not None
        started = _as_naive_utc(run.started_at)
        assert started <= _as_naive_utc(run.completed_at)
        assert run.action in entry_ts
        assert started <= _as_naive_utc(entry_ts[run.action])


@pytest.mark.asyncio
async def test_handle_event_login_failure_skips_ports():
    """If login fails, port operations are skipped."""
    event = _make_event("reservation.created")
    call_actions = []

    def mock_execute(driver_path, action, context, **kwargs):
        call_actions.append(action)
        if action == "login":
            return FAILURE_RESULT
        return SUCCESS_RESULT

    patches = _event_patches(execute_fn=mock_execute)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()
    assert call_actions == ["login"]


@pytest.mark.asyncio
async def test_handle_event_port_op_failure_continues():
    """Port operation failure still proceeds to logout."""
    event = _make_event("reservation.created")
    call_actions = []

    def mock_execute(driver_path, action, context, **kwargs):
        call_actions.append(action)
        if action == "connect_ports":
            return FAILURE_RESULT
        return SUCCESS_RESULT

    patches = _event_patches(execute_fn=mock_execute)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()
    assert "login" in call_actions
    assert "connect_ports" in call_actions
    assert "logout" in call_actions


@pytest.mark.asyncio
async def test_handle_event_driver_load_failure():
    """Driver load failure skips switch processing."""
    event = _make_event("reservation.created")
    patches = _event_patches(
        load_driver_fn=AsyncMock(side_effect=RuntimeError("download failed")),
    )
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_handle_event_logout_failure():
    """Logout failure is recorded as FAILED but does not raise."""
    event = _make_event("reservation.created")

    def mock_execute(driver_path, action, context, **kwargs):
        if action == "logout":
            return FAILURE_RESULT
        return SUCCESS_RESULT

    patches = _event_patches(execute_fn=mock_execute)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_handle_event_multiple_switches():
    """Multiple switches are each processed."""
    switch_2_id = str(uuid.uuid4())
    switch_2_data = {**SWITCH_DATA, "id": switch_2_id}
    ops = [
        {"switch_device_id": SWITCH_ID, "switch_port_a": "0/0/1", "switch_port_b": "0/0/2"},
        {"switch_device_id": switch_2_id, "switch_port_a": "0/1/1", "switch_port_b": "0/1/2"},
    ]
    event = _make_event("reservation.created")

    async def mock_fetch_device(device_id, client=None):
        if device_id == SWITCH_ID:
            return SWITCH_DATA
        return switch_2_data

    call_count = {"login": 0, "logout": 0}

    def mock_execute(driver_path, action, context, **kwargs):
        if action in call_count:
            call_count[action] += 1
        return SUCCESS_RESULT

    patches = _event_patches(
        execute_fn=mock_execute,
        fetch_device_fn=mock_fetch_device,
        ops=ops,
    )
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()
    assert call_count["login"] == 2
    assert call_count["logout"] == 2


@pytest.mark.asyncio
async def test_handle_event_disconnect_action():
    """Cancelled event maps to disconnect_ports action."""
    event = _make_event("reservation.cancelled")
    call_actions = []

    def mock_execute(driver_path, action, context, **kwargs):
        call_actions.append(action)
        return SUCCESS_RESULT

    patches = _event_patches(execute_fn=mock_execute)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(event, _db_session_factory())
    finally:
        for p in patches:
            p.stop()
    assert "disconnect_ports" in call_actions


# --- start/stop_nats_consumer ---


def _patched_nats_modules(mock_nats):
    """Register mocks for `nats`, `nats.js`, and `nats.js.api` so the consumer's
    `from nats.js.api import ConsumerConfig` import resolves during tests."""
    nats_js_api = MagicMock()
    nats_js_api.ConsumerConfig = MagicMock(return_value=MagicMock())
    nats_js = MagicMock()
    nats_js.api = nats_js_api
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

    # Create an async iterator that yields one message then raises CancelledError
    async def _message_iter():
        yield mock_msg
        await asyncio.sleep(3600)

    mock_sub = MagicMock()
    mock_sub.messages = _message_iter()
    mock_js.subscribe = AsyncMock(return_value=mock_sub)

    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)

    with patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)):
        await start_nats_consumer(mock_app)

    # Verify stream was created
    mock_js.add_stream.assert_called_once()
    # Verify subscription was created
    mock_js.subscribe.assert_called_once()
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
    mock_js.add_stream.side_effect = Exception("stream error")

    # Subscription with no messages
    async def _empty_iter():
        await asyncio.sleep(3600)
        yield  # never reached

    mock_sub = MagicMock()
    mock_sub.messages = _empty_iter()
    mock_js.subscribe = AsyncMock(return_value=mock_sub)

    mock_nats_module = MagicMock()
    mock_nats_module.connect = AsyncMock(return_value=mock_nc)

    with patch.dict("sys.modules", _patched_nats_modules(mock_nats_module)):
        await start_nats_consumer(mock_app)

    # Stream creation failed but subscribe still happened
    mock_js.subscribe.assert_called_once()

    # Clean up
    task = mock_app.state.nats_consumer_task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --- NATS-redelivery idempotency (issue #133) ---


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


def _recording_execute():
    """An execute_driver_method stub that records each (action, kwargs) it runs."""
    calls: list[tuple[str, dict]] = []

    def _execute(driver_path, action, context, **kwargs):
        calls.append((action, kwargs.get("method_kwargs") or {}))
        return SUCCESS_RESULT

    return calls, _execute


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


@pytest.mark.asyncio
async def test_l1_redelivery_skips_already_connected_ports():
    """A redelivery (same dedupe_key) whose only port pair already SUCCEEDED runs
    no driver method at all: the switch is skipped before login."""
    key = "HERD_RESERVATIONS:42"
    await _seed_success_run("connect_ports", "0/0/1", "0/0/2", dedupe_key=key, device_id=SWITCH_ID)
    calls, execute_fn = _recording_execute()

    patches = _event_patches(execute_fn=execute_fn)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(
            _make_event("reservation.created"), _db_session_factory(), key
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == [], f"expected no driver calls on full replay, ran {calls}"


@pytest.mark.asyncio
async def test_l1_fresh_delivery_executes_ports():
    """Baseline contrast: a new dedupe_key with no prior run executes connect_ports."""
    calls, execute_fn = _recording_execute()

    patches = _event_patches(execute_fn=execute_fn)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(
            _make_event("reservation.created"), _db_session_factory(), "HERD_RESERVATIONS:100"
        )
    finally:
        for p in patches:
            p.stop()

    ran = [action for action, _ in calls]
    assert "connect_ports" in ran


@pytest.mark.asyncio
async def test_l2_redelivery_skips_completed_vlan_ops():
    """On an L2 provision replay, a create_vlan and the add_to_vlan for a port that
    already SUCCEEDED under this dedupe_key are not re-run; an un-applied port is."""
    from app.services.nats_consumer import _execute_l2_switch_operations

    l2_switch_id = str(uuid.uuid4())
    l2_switch_data = {**SWITCH_DATA, "id": l2_switch_id, "connection_type": "Layer 2 Switch"}
    ops = [
        {"switch_device_id": l2_switch_id, "switch_port": "eth1", "tag": "tagged"},
        {"switch_device_id": l2_switch_id, "switch_port": "eth2", "tag": "tagged"},
    ]
    key = "HERD_RESERVATIONS:55"
    # Pretend a prior delivery created the VLAN and added eth1, then died before eth2.
    await _seed_success_run("create_vlan", dedupe_key=key, device_id=l2_switch_id)
    await _seed_success_run("add_to_vlan", "eth1", dedupe_key=key, device_id=l2_switch_id)

    calls, execute_fn = _recording_execute()

    async def _assign(operations, reservation_id, get_db_session):
        for op in operations:
            op["vlan_id"] = 100
        return operations

    patches = [
        patch(
            "app.services.nats_consumer._resolve_l2_switch_operations",
            new=AsyncMock(return_value=ops),
        ),
        patch("app.services.nats_consumer._assign_vlans_to_operations", new=_assign),
        patch(
            "app.services.nats_consumer._fetch_device", new=AsyncMock(return_value=l2_switch_data)
        ),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dev-1"],
            l2_action="provision",
            reservation_id=str(uuid.uuid4()),
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=key,
        )
    finally:
        for p in patches:
            p.stop()

    ran_actions = [action for action, _ in calls]
    # create_vlan and the eth1 add were already applied: not re-run.
    assert "create_vlan" not in ran_actions
    add_ports = [kw.get("port") for action, kw in calls if action == "add_to_vlan"]
    assert add_ports == ["eth2"], f"only the un-applied port should be added, got {add_ports}"
