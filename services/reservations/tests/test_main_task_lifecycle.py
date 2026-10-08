"""Issue #702: the purpose-classify reconciler runs on its own asyncio task,
separate from expiration_task, at its own interval, and is cancelled
alongside expiration_task at shutdown. Mirrors auth's
test_lifespan_starts_ldap_sync_loop_when_enabled (services/auth/tests/
test_main.py): fakes for both loop bodies signal they started and then block
until cancelled, proving the tasks are actually scheduled, not just that
create_task would have been reachable.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings


@pytest.mark.asyncio
async def test_lifespan_creates_and_cancels_expiration_and_purpose_classify_tasks():
    from app.main import lifespan

    expiration_started = asyncio.Event()
    purpose_classify_started = asyncio.Event()
    expiration_intervals: list[int] = []
    purpose_classify_intervals: list[int] = []

    async def fake_expiration_loop(interval_seconds=60):
        expiration_intervals.append(interval_seconds)
        expiration_started.set()
        await asyncio.Event().wait()

    async def fake_purpose_classify_loop(interval_seconds=60):
        purpose_classify_intervals.append(interval_seconds)
        purpose_classify_started.set()
        await asyncio.Event().wait()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("nats.connect", new=AsyncMock(side_effect=Exception("no nats in test env"))),
            patch("app.tasks.expiration.expiration_loop", new=fake_expiration_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_purpose_classify_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                await expiration_started.wait()
                await purpose_classify_started.wait()

    # If shutdown cancellation ever regresses to a hang, fail the test instead
    # of hanging the whole run.
    await asyncio.wait_for(_run(), timeout=10)

    assert expiration_started.is_set()
    assert purpose_classify_started.is_set()
    assert expiration_intervals == [settings.expiration_interval_seconds]
    assert purpose_classify_intervals == [settings.purpose_classify_interval_seconds]


@pytest.mark.asyncio
async def test_lifespan_purpose_classify_task_uses_its_own_interval_setting():
    """The two tasks are independently configurable: a distinct
    purpose_classify_interval_seconds is threaded through to
    purpose_classify_loop, not expiration_interval_seconds."""
    from app.main import lifespan

    purpose_classify_started = asyncio.Event()
    seen_intervals: list[int] = []

    async def fake_expiration_loop(interval_seconds=60):
        await asyncio.Event().wait()

    async def fake_purpose_classify_loop(interval_seconds=60):
        seen_intervals.append(interval_seconds)
        purpose_classify_started.set()
        await asyncio.Event().wait()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("nats.connect", new=AsyncMock(side_effect=Exception("no nats in test env"))),
            patch.object(settings, "purpose_classify_interval_seconds", 12345),
            patch("app.tasks.expiration.expiration_loop", new=fake_expiration_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_purpose_classify_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                await purpose_classify_started.wait()

    await asyncio.wait_for(_run(), timeout=10)
    assert seen_intervals == [12345]


@pytest.mark.asyncio
async def test_lifespan_starts_without_nats_using_the_real_client(monkeypatch, caplog):
    """Issue #1083: with the REAL nats client and a broker that refuses
    connections, startup finishes (the bounded first connect raises) and the
    documented logged-and-continue branch runs, rather than the lifespan
    waiting forever inside nats.connect. No mock of nats.connect here: a mock
    that raises proved a path the real client never took."""
    import herd_common.jetstream as jetstream
    from app.main import lifespan

    monkeypatch.setattr(jetstream, "NATS_INITIAL_CONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(jetstream, "NATS_RECONNECT_TIME_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(settings, "nats_url", "nats://127.0.0.1:1")

    async def fake_loop(interval_seconds=60):
        await asyncio.Event().wait()

    entered = asyncio.Event()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("app.tasks.expiration.expiration_loop", new=fake_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                entered.set()
                assert mock_app.state.nats is None

    with caplog.at_level("WARNING", logger="app.main"):
        await asyncio.wait_for(_run(), timeout=10)

    assert entered.is_set()
    assert "NATS unavailable at nats://127.0.0.1:1, events will be skipped" in caplog.text
