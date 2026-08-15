"""Unit tests for the ADR 0011 phase 5 interval loop (app/tasks/ldap_sync_loop.py).

The loop is a forever-loop that sleeps a full interval before its first sync
(decision: no boot-time sync burst on a rolling restart), swallows
SyncBusyError as a routine skip (another replica or an overlapping run
already holds the serialization slot), and swallows any other tick exception
so a transient failure never kills the task. We drive it with the repo's
established sleep-sentinel idiom (services/ai-orchestrator/tests/
test_conversation_sweeper.py, services/reservations/tests/
test_coverage_gaps.py): patch the module's asyncio.sleep with a fake that
counts calls and raises a sentinel after N ticks, bounding the
otherwise-infinite loop.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from app.database import AsyncSessionLocal, Base, engine
from app.models.ldap_sync_run import LdapSyncRun
from app.services import ldap_sync_service
from app.tasks import ldap_sync_loop as loop_module
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


def _fake_sleep_after(n_ticks: int):
    """A fake asyncio.sleep that returns normally for the first n_ticks calls
    (letting that many loop iterations run past the sleep) then raises
    _StopLoop on the next call, breaking the forever loop deterministically."""
    calls = {"n": 0}

    async def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > n_ticks:
            raise _StopLoop

    return fake_sleep


def _patch_clock(monkeypatch, *timestamps: datetime) -> None:
    """Patch loop_module._utcnow to return each of `timestamps` in order, one
    per call. The loop calls _utcnow() exactly once per tick (the cadence
    check), so `timestamps` should have one entry per tick under test;
    exhausting the sequence raises StopIteration, which surfaces as a clear
    test failure if a test under-supplies values."""
    clock = iter(timestamps)
    monkeypatch.setattr(loop_module, "_utcnow", lambda: next(clock))


# --- ldap_group_sync_loop_enabled: the lifespan gating helper ---


def test_loop_enabled_requires_both_ldap_method_and_flag(monkeypatch):
    monkeypatch.setattr(loop_module.settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(loop_module.settings, "ldap_group_sync_enabled", True, raising=False)
    assert loop_module.ldap_group_sync_loop_enabled() is True


def test_loop_disabled_when_flag_off_even_in_ldap_mode(monkeypatch):
    monkeypatch.setattr(loop_module.settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(loop_module.settings, "ldap_group_sync_enabled", False, raising=False)
    assert loop_module.ldap_group_sync_loop_enabled() is False


def test_loop_disabled_in_local_mode_even_when_flag_on(monkeypatch):
    monkeypatch.setattr(loop_module.settings, "auth_method", "local", raising=False)
    monkeypatch.setattr(loop_module.settings, "ldap_group_sync_enabled", True, raising=False)
    assert loop_module.ldap_group_sync_loop_enabled() is False


# --- (a) first tick sleeps before syncing ---


async def test_first_tick_sleeps_before_syncing(monkeypatch):
    order = []
    calls = {"n": 0}

    async def fake_sleep(_seconds):
        calls["n"] += 1
        order.append("sleep")
        if calls["n"] >= 2:
            raise _StopLoop

    async def fake_tick():
        order.append("tick")

    monkeypatch.setattr(loop_module, "_run_interval_tick", fake_tick)
    monkeypatch.setattr(loop_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await loop_module.ldap_sync_loop(interval_seconds=5)

    # Exactly one tick ran, and the sleep before it completed first: no
    # boot-time sync burst.
    assert order == ["sleep", "tick", "sleep"]


# --- (b) a tick invokes the run machinery with trigger "interval" ---


async def test_tick_invokes_run_sync_with_interval_trigger(monkeypatch):
    mock_run_sync = AsyncMock()
    monkeypatch.setattr(loop_module.ldap_sync_service, "run_sync", mock_run_sync)
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(1))

    with pytest.raises(_StopLoop):
        await loop_module.ldap_sync_loop(interval_seconds=5)

    mock_run_sync.assert_awaited_once()
    _, kwargs = mock_run_sync.call_args
    assert kwargs.get("trigger") == "interval"


# --- (c) SyncBusyError is swallowed as a skip, never an error ---


async def test_sync_busy_error_is_swallowed_as_skip(monkeypatch, caplog):
    async def busy_run_sync(db, *, trigger):
        raise ldap_sync_service.SyncBusyError("in_process")

    monkeypatch.setattr(loop_module.ldap_sync_service, "run_sync", busy_run_sync)
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(2))

    import logging

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(_StopLoop):
            await loop_module.ldap_sync_loop(interval_seconds=5)

    # Two ticks ran (the loop was never killed by the busy slot), and neither
    # was logged as an error: busy is routine contention, not a failure.
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    skip_records = [r for r in caplog.records if "busy" in r.getMessage()]
    assert len(skip_records) == 2
    assert skip_records[0].levelname == "INFO"


# --- (d) an arbitrary tick exception is logged and the loop continues ---


async def test_tick_exception_is_logged_and_loop_continues(monkeypatch, caplog):
    async def boom_run_sync(db, *, trigger):
        raise RuntimeError("directory unreachable")

    monkeypatch.setattr(loop_module.ldap_sync_service, "run_sync", boom_run_sync)
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(2))

    import logging

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StopLoop):
            await loop_module.ldap_sync_loop(interval_seconds=5)

    failed = [r for r in caplog.records if r.message == "LDAP interval sync tick failed"]
    # Both ticks raised and were caught; the loop reached the sleep after each.
    assert len(failed) == 2
    assert failed[0].levelname == "ERROR"
    assert failed[0].exc_info is not None


# --- (e) retention prune: only old, non-running rows; at most once per 24h ---


async def test_prune_old_runs_deletes_only_old_non_running_rows(monkeypatch):
    monkeypatch.setattr(loop_module.settings, "ldap_sync_runs_retention_days", 90, raising=False)
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(days=91)
    recent_ts = now - timedelta(days=1)

    old_finished_id = uuid.uuid4()
    old_running_id = uuid.uuid4()
    recent_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                LdapSyncRun(
                    id=old_finished_id, started_at=old_ts, trigger="interval", status="success"
                ),
                # A "running" row past the cutoff must survive: it is either a
                # genuinely in-flight run or a crashed one, either way live
                # state, never prunable history.
                LdapSyncRun(
                    id=old_running_id, started_at=old_ts, trigger="interval", status="running"
                ),
                LdapSyncRun(
                    id=recent_id, started_at=recent_ts, trigger="interval", status="success"
                ),
            ]
        )
        await db.commit()

    removed = await loop_module._prune_old_runs()
    assert removed == 1

    async with AsyncSessionLocal() as db:
        remaining_ids = set((await db.execute(select(LdapSyncRun.id))).scalars().all())
    assert remaining_ids == {old_running_id, recent_id}


async def test_first_tick_prunes_because_last_prune_starts_unseeded(monkeypatch):
    """Item 2: last_prune seeds None, so the FIRST due tick after startup
    prunes unconditionally, not after waiting out a full 24h post-restart.
    A single clock value suffices: _utcnow() is consulted only for the
    cadence check now, never to seed a start time."""
    prune_calls = []

    async def fake_prune():
        prune_calls.append(1)
        return 0

    _patch_clock(monkeypatch, datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(loop_module, "_prune_old_runs", fake_prune)
    monkeypatch.setattr(loop_module, "_run_interval_tick", AsyncMock())
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(1))

    with pytest.raises(_StopLoop):
        await loop_module.ldap_sync_loop(interval_seconds=5)

    assert len(prune_calls) == 1


async def test_prune_runs_at_most_once_per_24h_across_two_ticks(monkeypatch):
    """After the first (unconditional) prune, the 24h cadence gate is a pure
    rate limit: a second tick well inside 24h of the first must not prune
    again."""
    prune_calls = []

    async def fake_prune():
        prune_calls.append(1)
        return 0

    _patch_clock(
        monkeypatch,
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),  # tick1: last_prune None, prunes
        datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),  # tick2: 11h later, must not prune
    )
    monkeypatch.setattr(loop_module, "_prune_old_runs", fake_prune)
    monkeypatch.setattr(loop_module, "_run_interval_tick", AsyncMock())
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(2))

    with pytest.raises(_StopLoop):
        await loop_module.ldap_sync_loop(interval_seconds=5)

    assert len(prune_calls) == 1


async def test_failed_prune_retries_next_tick_succeeding_prune_then_rate_limits(monkeypatch):
    """Item 3, pinned end to end: a raising prune does NOT advance
    last_prune, so the very next tick retries (rather than waiting out a
    full 24h with nothing pruned); once a prune succeeds, last_prune DOES
    advance, and a subsequent tick within 24h of that success does not
    prune again."""
    prune_calls = []

    async def flaky_then_ok_prune():
        prune_calls.append(1)
        if len(prune_calls) == 1:
            raise RuntimeError("db unavailable")
        return 0

    _patch_clock(
        monkeypatch,
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),  # tick1: attempts, raises
        datetime(
            2026, 1, 1, 0, 1, tzinfo=timezone.utc
        ),  # tick2: last_prune still None, retries, succeeds
        datetime(
            2026, 1, 1, 12, 0, tzinfo=timezone.utc
        ),  # tick3: 12h after the success, must not retry
    )
    monkeypatch.setattr(loop_module, "_prune_old_runs", flaky_then_ok_prune)
    monkeypatch.setattr(loop_module, "_run_interval_tick", AsyncMock())
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(3))

    with pytest.raises(_StopLoop):
        await loop_module.ldap_sync_loop(interval_seconds=5)

    # Called on tick1 (fails) and tick2 (retries, succeeds); tick3 must NOT
    # call it again, since the success on tick2 reset the 24h window.
    assert len(prune_calls) == 2


async def test_prune_failure_is_logged_and_loop_continues(monkeypatch, caplog):
    """A prune-cadence hit whose delete raises must not kill the loop either;
    it gets the same log-and-continue treatment as a tick failure. Only one
    clock value is needed: last_prune starts None, so the single tick here
    attempts the (failing) prune unconditionally."""

    async def boom_prune():
        raise RuntimeError("db unavailable")

    _patch_clock(monkeypatch, datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(loop_module, "_prune_old_runs", boom_prune)
    monkeypatch.setattr(loop_module, "_run_interval_tick", AsyncMock())
    monkeypatch.setattr(loop_module.asyncio, "sleep", _fake_sleep_after(1))

    import logging

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StopLoop):
            await loop_module.ldap_sync_loop(interval_seconds=5)

    failed = [r for r in caplog.records if r.message == "LDAP sync run retention prune failed"]
    assert failed


# --- item 4: effective_interval_seconds, the production-only interval floor ---


def test_effective_interval_seconds_passes_through_at_or_above_floor():
    assert loop_module.effective_interval_seconds(60) == 60
    assert loop_module.effective_interval_seconds(3600) == 3600


def test_effective_interval_seconds_clamps_and_warns_below_floor(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        result = loop_module.effective_interval_seconds(5)

    assert result == 60
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings
    assert "clamping" in warnings[0].getMessage()


def test_effective_interval_seconds_clamps_zero():
    # 0 is the realistic misconfiguration (an unset or blanked-out numeric
    # env var resolving to its type default), not just a small positive typo.
    assert loop_module.effective_interval_seconds(0) == 60
