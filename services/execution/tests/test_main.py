"""Tests for app/main.py startup helpers.

These are unit-level: they assert _ensure_dlq_stream binds the right stream
name and subject pattern, is safe when NATS is down, and swallows broker
errors so the service still boots. They do NOT prove a DLQ'd message is
actually retained under a live broker; that needs the stack up (see
the integration coverage note in docs/GAPS.md / OPERATIONS.md).
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app import main as main_module
from app.config import settings
from app.database import engine
from app.main import (
    _ensure_dlq_stream,
    _ensure_health_stream,
    start_outbox_relay,
    stop_outbox_relay,
)

# Exception text a broker error could carry; no warning may repeat it.
_EXC_TEXT = "nats://herd-user:tok3n-value@broker:4222 replied with an error"


def _main_warnings(caplog):
    """app.main's WARNING messages. A record with a traceback is left out, so a
    warning that logs exc_info again (and with it the exception text) fails the
    equality checks below (issue #1418)."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.main" and r.levelno == logging.WARNING and r.exc_info is None
    ]


@pytest.mark.asyncio
async def test_ensure_dlq_stream_binds_shared_stream(monkeypatch):
    """Creates HERD_DLQ over the herd.*.dlq.> subject space via ensure_stream."""
    mock_js = AsyncMock()
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    ensure_stream_mock = AsyncMock()
    monkeypatch.setattr(main_module, "ensure_stream", ensure_stream_mock)

    app = MagicMock()
    app.state.nats = mock_nc

    await _ensure_dlq_stream(app)

    ensure_stream_mock.assert_awaited_once_with(
        mock_js,
        name="HERD_DLQ",
        subjects=["herd.*.dlq.>"],
        max_age_seconds=settings.nats_stream_max_age_seconds,
    )


@pytest.mark.asyncio
async def test_ensure_dlq_stream_noop_when_nats_down():
    """No NATS connection: helper returns without touching JetStream."""
    app = MagicMock()
    app.state.nats = None

    # Must not raise.
    await _ensure_dlq_stream(app)


@pytest.mark.asyncio
async def test_ensure_dlq_stream_swallows_broker_error(monkeypatch, caplog):
    """ensure_stream failure is logged, not raised, so the service still boots.
    Issue #1418 pins the line: the class name only, never the exception text,
    and what a missing HERD_DLQ means for a dead-lettered message."""
    mock_js = AsyncMock()
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    ensure_stream_mock = AsyncMock(side_effect=Exception(_EXC_TEXT))
    monkeypatch.setattr(main_module, "ensure_stream", ensure_stream_mock)

    app = MagicMock()
    app.state.nats = mock_nc

    # Must not raise.
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _ensure_dlq_stream(app)
    ensure_stream_mock.assert_awaited_once()
    assert _main_warnings(caplog) == [
        "JetStream stream HERD_DLQ could not be created or updated (Exception); if it "
        "exists, dead-lettered messages are kept on it, and while it is missing a "
        "dead-letter publish fails and the original message is acked, so that "
        "message is kept nowhere"
    ]
    assert "tok3n-value" not in caplog.text


@pytest.mark.asyncio
async def test_ensure_health_stream_failure_says_health_events_wait_in_the_outbox(
    monkeypatch, caplog
):
    """Issue #1418: a failed HERD_HEALTH ensure keeps the connection (the
    relay still publishes) and logs the class name only."""
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr(
        main_module, "ensure_stream", AsyncMock(side_effect=RuntimeError(_EXC_TEXT))
    )
    app = SimpleNamespace(state=SimpleNamespace(nats=mock_nc))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _ensure_health_stream(app)

    assert _main_warnings(caplog) == [
        "JetStream stream HERD_HEALTH could not be created or updated (RuntimeError); "
        "the connection is kept, so health events publish if the stream exists, and "
        "while it is missing they wait in the outbox and the relay retries them every "
        "tick"
    ]
    assert "tok3n-value" not in caplog.text
    assert app.state.nats is mock_nc


# --- outbox relay lifespan wiring (issue #21) ---


@pytest.mark.asyncio
async def test_start_outbox_relay_starts_task_and_stop_cancels(monkeypatch):
    """start_outbox_relay schedules the relay; stop_outbox_relay cancels it.

    Also pins (issue #682) that the relay's engine and the four outbox
    settings (tick/batch/retention/wake_on_write) are forwarded, closing the
    gap docs/ENV_VARS.md used to flag: execution previously passed only `name`.
    """
    started = asyncio.Event()
    captured: dict = {}

    async def _idle_relay(session_factory, get_nats, model, **kwargs):
        # Stand in for run_outbox_relay: confirm wiring, then idle until cancel.
        captured.update(kwargs)
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

    assert captured["tick_seconds"] == settings.outbox_relay_tick_seconds
    assert captured["batch_size"] == settings.outbox_batch_size
    assert captured["retention_seconds"] == settings.outbox_retention_seconds
    assert captured["engine"] is engine
    assert captured["wake_on_write"] == settings.outbox_wake_on_write

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
