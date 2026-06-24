"""Tests for the idle-conversation background sweeper.

The loop is a forever-loop that swallows per-cycle exceptions so a transient
DB issue never kills the task. We drive it by patching the module's
_run_sweeper_cycle and asyncio.sleep so a bounded number of cycles run, then a
sentinel exception breaks the loop. _run_sweeper_cycle itself is exercised
against the real in-memory DB so the expire_idle integration is covered too.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app import config as config_module
from app.database import AsyncSessionLocal, Base, engine
from app.models.conversation import AssistantConversation
from app.tasks import conversation_sweeper as sweeper
from sqlalchemy import select


class _StopLoop(Exception):
    """Sentinel raised from a patched asyncio.sleep to break the forever loop."""


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert_conversation(*, last_used_age_hours: float) -> uuid.UUID:
    """Insert a conversation whose last_used_at is `age` hours in the past."""
    conv_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        conv = AssistantConversation(
            id=conv_id,
            user_id=uuid.uuid4(),
            reservation_id=uuid.uuid4(),
            seed_block="seed",
            last_used_at=datetime.now(timezone.utc) - timedelta(hours=last_used_age_hours),
        )
        db.add(conv)
        await db.commit()
    return conv_id


# --- _run_sweeper_cycle against the real DB ---


async def test_run_sweeper_cycle_deletes_idle_conversations(monkeypatch):
    monkeypatch.setattr(config_module.settings, "assistant_conversation_ttl_hours", 24)
    await _insert_conversation(last_used_age_hours=48)  # idle, must be swept
    fresh = await _insert_conversation(last_used_age_hours=1)  # recent, must remain

    expired = await sweeper._run_sweeper_cycle()
    assert expired == 1

    async with AsyncSessionLocal() as db:
        remaining = (await db.execute(select(AssistantConversation))).scalars().all()
    assert [c.id for c in remaining] == [fresh]


async def test_run_sweeper_cycle_returns_zero_when_nothing_idle(monkeypatch):
    monkeypatch.setattr(config_module.settings, "assistant_conversation_ttl_hours", 24)
    await _insert_conversation(last_used_age_hours=1)
    assert await sweeper._run_sweeper_cycle() == 0


# --- conversation_sweeper_loop forever-loop branches ---


async def test_loop_logs_cycle_when_conversations_expired(monkeypatch, caplog):
    """A cycle returning >0 logs conversation_sweeper_cycle then sleeps."""
    calls = {"cycles": 0, "sleeps": 0}

    async def fake_cycle():
        calls["cycles"] += 1
        return 3

    async def fake_sleep(_seconds):
        calls["sleeps"] += 1
        raise _StopLoop

    monkeypatch.setattr(sweeper, "_run_sweeper_cycle", fake_cycle)
    monkeypatch.setattr(sweeper.asyncio, "sleep", fake_sleep)

    import logging

    with caplog.at_level(logging.INFO):
        with pytest.raises(_StopLoop):
            await sweeper.conversation_sweeper_loop(interval_seconds=10)

    assert calls == {"cycles": 1, "sleeps": 1}
    messages = [r.message for r in caplog.records]
    assert "conversation_sweeper_started" in messages
    assert "conversation_sweeper_cycle" in messages
    cycle_rec = next(r for r in caplog.records if r.message == "conversation_sweeper_cycle")
    assert cycle_rec.__dict__.get("expired") == 3


async def test_loop_skips_cycle_log_when_nothing_expired(monkeypatch, caplog):
    """A cycle returning 0 does NOT emit conversation_sweeper_cycle, still sleeps."""

    async def fake_cycle():
        return 0

    async def fake_sleep(_seconds):
        raise _StopLoop

    monkeypatch.setattr(sweeper, "_run_sweeper_cycle", fake_cycle)
    monkeypatch.setattr(sweeper.asyncio, "sleep", fake_sleep)

    import logging

    with caplog.at_level(logging.INFO):
        with pytest.raises(_StopLoop):
            await sweeper.conversation_sweeper_loop(interval_seconds=10)

    assert "conversation_sweeper_cycle" not in [r.message for r in caplog.records]


async def test_loop_swallows_cycle_exception_and_keeps_running(monkeypatch, caplog):
    """A cycle raising must be caught, logged as failed, and the loop must
    continue (sleep still runs after the failure). This is the resilience
    invariant: a transient DB error must not kill the sweeper task.
    """

    async def boom_cycle():
        raise RuntimeError("transient db error")

    slept = {"n": 0}

    async def fake_sleep(_seconds):
        slept["n"] += 1
        raise _StopLoop

    monkeypatch.setattr(sweeper, "_run_sweeper_cycle", boom_cycle)
    monkeypatch.setattr(sweeper.asyncio, "sleep", fake_sleep)

    import logging

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StopLoop):
            await sweeper.conversation_sweeper_loop(interval_seconds=10)

    # The exception was caught and logged, not propagated, and the loop still
    # reached the sleep at the end of the cycle.
    assert slept["n"] == 1
    failed = [r for r in caplog.records if r.message == "conversation_sweeper_failed"]
    assert failed, "expected conversation_sweeper_failed log record"
    assert failed[0].levelname == "ERROR"
    assert failed[0].exc_info is not None
