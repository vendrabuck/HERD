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
