"""Tests for app/main.py startup helpers.

These are unit-level: they assert _ensure_dlq_stream binds the right stream
name and subject pattern, is safe when NATS is down, and swallows broker
errors so the service still boots. They do NOT prove a DLQ'd message is
actually retained under a live broker; that needs the stack up (see
the integration coverage note in docs/GAPS.md / OPERATIONS.md).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app import main as main_module
from app.main import _ensure_dlq_stream, start_outbox_relay, stop_outbox_relay


@pytest.mark.asyncio
async def test_ensure_dlq_stream_binds_shared_stream():
    """Creates HERD_DLQ over the herd.*.dlq.> subject space."""
    mock_js = AsyncMock()
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    app = MagicMock()
    app.state.nats = mock_nc

    await _ensure_dlq_stream(app)

    mock_js.add_stream.assert_awaited_once_with(name="HERD_DLQ", subjects=["herd.*.dlq.>"])


@pytest.mark.asyncio
async def test_ensure_dlq_stream_noop_when_nats_down():
    """No NATS connection: helper returns without touching JetStream."""
    app = MagicMock()
    app.state.nats = None

    # Must not raise.
    await _ensure_dlq_stream(app)


@pytest.mark.asyncio
async def test_ensure_dlq_stream_swallows_broker_error():
    """add_stream failure is logged, not raised, so the service still boots."""
    mock_js = AsyncMock()
    mock_js.add_stream.side_effect = Exception("stream error")
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    app = MagicMock()
    app.state.nats = mock_nc

    # Must not raise.
    await _ensure_dlq_stream(app)
    mock_js.add_stream.assert_awaited_once()


# --- outbox relay lifespan wiring (issue #21) ---


@pytest.mark.asyncio
async def test_start_outbox_relay_starts_task_and_stop_cancels(monkeypatch):
    """start_outbox_relay schedules the relay; stop_outbox_relay cancels it."""
    started = asyncio.Event()

    async def _idle_relay(session_factory, get_nats, model, **kwargs):
        # Stand in for run_outbox_relay: confirm wiring, then idle until cancel.
        started.set()
        assert get_nats() is None  # reads app.state.nats lazily
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "run_outbox_relay", _idle_relay)

    app = SimpleNamespace(state=SimpleNamespace(nats=None))
    await start_outbox_relay(app)

    task = app.state.outbox_relay_task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert not task.done()

    await stop_outbox_relay(app)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_stop_outbox_relay_noop_when_never_started():
    """stop_outbox_relay is safe when no relay task was ever stored."""
    app = SimpleNamespace(state=SimpleNamespace())
    # Must not raise.
    await stop_outbox_relay(app)


# --- consumer schema gate lifespan wiring (issue #463) ---


def _stub_lifespan_periphery(monkeypatch):
    """Stub every lifespan start/stop except schema init and the consumer gate."""
    for name in (
        "start_health_scheduler",
        "stop_health_scheduler",
        "start_outbox_relay",
        "stop_outbox_relay",
        "start_wiring_retry_scheduler",
        "stop_wiring_retry_scheduler",
        "_ensure_health_stream",
        "_ensure_dlq_stream",
    ):
        monkeypatch.setattr(main_module, name, AsyncMock())


@pytest.mark.asyncio
async def test_lifespan_starts_consumer_immediately_when_schema_ready(monkeypatch):
    """No managed-path drift: the consumer starts inline, no gate task exists."""
    from herd_common.schema_init import SchemaInitOutcome, SchemaInitResult

    _stub_lifespan_periphery(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_all_and_stamp",
        AsyncMock(return_value=SchemaInitOutcome(SchemaInitResult.ALREADY_MANAGED)),
    )
    started = AsyncMock()
    monkeypatch.setattr(main_module, "start_nats_consumer", started)

    app = SimpleNamespace(state=SimpleNamespace())
    async with main_module.lifespan(app):
        started.assert_awaited_once_with(app)
        assert getattr(app.state, "consumer_schema_gate_task", None) is None
        # The stream ensures still run right after consumer start.
        main_module._ensure_health_stream.assert_awaited_once_with(app)
        main_module._ensure_dlq_stream.assert_awaited_once_with(app)


@pytest.mark.asyncio
async def test_lifespan_defers_consumer_on_managed_schema_missing_tables(monkeypatch):
    """The issue #463 window: consumer start is gated; shutdown cancels the gate.

    The rest of the lifespan (schedulers, outbox relay) starts as usual; only
    the consumer waits. The gate's poll interval is the 5s default, so within
    this context nothing touches the real database engine.
    """
    from herd_common.schema_init import SchemaInitOutcome, SchemaInitResult

    _stub_lifespan_periphery(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_all_and_stamp",
        AsyncMock(
            return_value=SchemaInitOutcome(
                SchemaInitResult.ALREADY_MANAGED, frozenset({"l2_port_assignments"})
            )
        ),
    )
    started = AsyncMock()
    monkeypatch.setattr(main_module, "start_nats_consumer", started)

    app = SimpleNamespace(state=SimpleNamespace())
    async with main_module.lifespan(app):
        started.assert_not_awaited()
        gate_task = app.state.consumer_schema_gate_task
        assert gate_task is not None and not gate_task.done()
        # Everything else started despite the gated consumer.
        main_module.start_health_scheduler.assert_awaited_once()
        main_module.start_outbox_relay.assert_awaited_once()
        main_module.start_wiring_retry_scheduler.assert_awaited_once()
        # The stream ensures ride the deferred consumer start, so not yet.
        main_module._ensure_dlq_stream.assert_not_awaited()

    assert gate_task.cancelled()
    started.assert_not_awaited()
