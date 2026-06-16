"""Tests for app/main.py startup helpers.

These are unit-level: they assert _ensure_dlq_stream binds the right stream
name and subject pattern, is safe when NATS is down, and swallows broker
errors so the service still boots. They do NOT prove a DLQ'd message is
actually retained under a live broker; that needs the stack up (see
the integration coverage note in docs/GAPS.md / OPERATIONS.md).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.main import _ensure_dlq_stream


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
