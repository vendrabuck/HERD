"""Unit tests for the registry, loop, and lifespan helpers of health_scheduler.

Complements test_health_scheduler.py (which covers fire_poll, _claim_row,
_due_rows, _next_poll_at, _decide_transition). Here we cover:

- _fetch_registry: token-missing, transport error, non-200, malformed JSON,
  per-row parse skips, happy path.
- _refresh_registry_if_due: cadence gating, fetch-failure leaves cache alone,
  successful refresh seeds missing status rows.
- _seed_missing_status_rows: empty input, SQLite fallback path, idempotency.
- run_health_scheduler_loop: one tick that polls a due device then cancels;
  a device dropped from the registry is pushed forward; a crashing fire_poll
  is swallowed.
- start_health_scheduler / stop_health_scheduler lifespan wiring.

All async work is awaited; no real timers run. The loop is exercised by
monkeypatching asyncio.sleep to raise CancelledError after the first tick.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app.database import Base
from app.models.device_health_status import DeviceHealthStatus
from app.services import health_scheduler
from app.services.health_scheduler import (
    _fetch_registry,
    _refresh_registry_if_due,
    _seed_missing_status_rows,
    run_health_scheduler_loop,
    start_health_scheduler,
    stop_health_scheduler,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    health_scheduler._registry.clear()
    health_scheduler._registry_last_refresh = None
    health_scheduler._template_cache.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# --- Fake httpx client driven by a single canned response or exception ---


class _FakeResp:
    def __init__(self, status_code: int, json_data=None, text: str = "", raise_json=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json_data


class _FakeClient:
    def __init__(self, resp=None, exc: Exception | None = None):
        self._resp = resp
        self._exc = exc

    async def get(self, url, headers=None, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


# --- _fetch_registry ---


@pytest.mark.asyncio
async def test_fetch_registry_no_token_returns_none(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "internal_api_token", "")
    result = await _fetch_registry(_FakeClient())
    assert result is None


@pytest.mark.asyncio
async def test_fetch_registry_transport_error_returns_none(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "internal_api_token", "tok")
    client = _FakeClient(exc=httpx.ConnectError("down"))
    assert await _fetch_registry(client) is None


@pytest.mark.asyncio
async def test_fetch_registry_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "internal_api_token", "tok")
    client = _FakeClient(resp=_FakeResp(503, text="upstream sad"))
    assert await _fetch_registry(client) is None


@pytest.mark.asyncio
async def test_fetch_registry_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "internal_api_token", "tok")
    client = _FakeClient(resp=_FakeResp(200, raise_json=True))
    assert await _fetch_registry(client) is None


@pytest.mark.asyncio
async def test_fetch_registry_parses_rows_and_skips_bad_ones(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "internal_api_token", "tok")
    good_id = uuid.uuid4()
    payload = [
        {"device_id": str(good_id), "resolved_interval_seconds": 90},
        {"device_id": "not-a-uuid", "resolved_interval_seconds": 60},  # ValueError -> skip
        {"resolved_interval_seconds": 60},  # KeyError -> skip
        {"device_id": str(uuid.uuid4()), "resolved_interval_seconds": "x"},  # ValueError -> skip
    ]
    client = _FakeClient(resp=_FakeResp(200, json_data=payload))
    result = await _fetch_registry(client)
    assert result == {good_id: 90}


# --- _refresh_registry_if_due ---


@pytest.mark.asyncio
async def test_refresh_registry_skips_when_not_due(monkeypatch):
    """Within the refresh window, _fetch_registry must not be called."""
    now = datetime.now(timezone.utc)
    health_scheduler._registry_last_refresh = now
    fetch = AsyncMock()
    monkeypatch.setattr(health_scheduler, "_fetch_registry", fetch)

    async with TestSessionLocal() as db:
        await _refresh_registry_if_due(_FakeClient(), now + timedelta(seconds=1), db)

    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_registry_fetch_failure_leaves_cache(monkeypatch):
    """A failed fetch (None) must not wipe the existing registry."""
    now = datetime.now(timezone.utc)
    existing_id = uuid.uuid4()
    health_scheduler._registry[existing_id] = 120
    monkeypatch.setattr(health_scheduler, "_fetch_registry", AsyncMock(return_value=None))

    async with TestSessionLocal() as db:
        await _refresh_registry_if_due(_FakeClient(), now, db)

    assert health_scheduler._registry == {existing_id: 120}


@pytest.mark.asyncio
async def test_refresh_registry_success_updates_cache_and_seeds(monkeypatch):
    now = datetime.now(timezone.utc)
    dev_id = uuid.uuid4()
    monkeypatch.setattr(health_scheduler, "_fetch_registry", AsyncMock(return_value={dev_id: 45}))

    async with TestSessionLocal() as db:
        await _refresh_registry_if_due(_FakeClient(), now, db)

    assert health_scheduler._registry == {dev_id: 45}
    assert health_scheduler._registry_last_refresh == now
    # A status row was seeded for the new device, due immediately.
    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, dev_id)
        assert row is not None
        assert row.last_status in (None, "UNKNOWN")


# --- _seed_missing_status_rows ---


@pytest.mark.asyncio
async def test_seed_missing_status_rows_empty_is_noop():
    async with TestSessionLocal() as db:
        await _seed_missing_status_rows(db, [], datetime.now(timezone.utc))
        rows = (await db.execute(select(DeviceHealthStatus))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_seed_missing_status_rows_falls_back_to_per_row(monkeypatch):
    """If the bulk ON CONFLICT insert raises, the per-row IntegrityError path runs.

    We force the first bulk execute to raise so the SQLite fallback (rollback
    then one-by-one add/commit) is exercised. One device already has a row, so
    its per-row insert trips IntegrityError and is swallowed; the other is added.
    """
    now = datetime.now(timezone.utc)
    existing = uuid.uuid4()
    new_one = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(DeviceHealthStatus(device_id=existing, next_poll_at=now, last_status="HEALTHY"))
        await db.commit()

    async with TestSessionLocal() as db:
        real_execute = db.execute
        calls = {"n": 0}

        async def _flaky_execute(*args, **kwargs):
            # Fail only the first call (the bulk pg_insert), then defer to real.
            if calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("bulk insert unsupported")
            return await real_execute(*args, **kwargs)

        monkeypatch.setattr(db, "execute", _flaky_execute)
        await _seed_missing_status_rows(db, [existing, new_one], now)

    async with TestSessionLocal() as db:
        rows = {r.device_id for r in (await db.execute(select(DeviceHealthStatus))).scalars()}
        assert rows == {existing, new_one}


@pytest.mark.asyncio
async def test_seed_missing_status_rows_per_row_generic_error_logged(monkeypatch):
    """In the fallback path, a non-IntegrityError on a per-row commit is logged, not raised.

    Force the bulk insert to fail (enter the fallback), then make every commit
    raise a generic RuntimeError so the per-row `except Exception` branch runs.
    The call must complete without propagating the error.
    """
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as db:

        async def _bulk_fail(*args, **kwargs):
            raise RuntimeError("bulk unsupported")

        async def _commit_fail():
            raise RuntimeError("commit boom")

        monkeypatch.setattr(db, "execute", _bulk_fail)
        monkeypatch.setattr(db, "commit", _commit_fail)

        logged = []
        monkeypatch.setattr(
            health_scheduler.logger, "exception", lambda msg, *a, **kw: logged.append(msg)
        )

        # Must not raise even though every commit fails.
        await _seed_missing_status_rows(db, [uuid.uuid4()], now)

    assert logged  # the generic-error branch logged at least once


@pytest.mark.asyncio
async def test_seed_missing_status_rows_inserts_new_and_skips_existing():
    now = datetime.now(timezone.utc)
    existing = uuid.uuid4()
    new_one = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(DeviceHealthStatus(device_id=existing, next_poll_at=now, last_status="HEALTHY"))
        await db.commit()

    async with TestSessionLocal() as db:
        await _seed_missing_status_rows(db, [existing, new_one], now)

    async with TestSessionLocal() as db:
        rows = {r.device_id: r for r in (await db.execute(select(DeviceHealthStatus))).scalars()}
        assert set(rows) == {existing, new_one}
        # The pre-existing row keeps its status; the new one is a stub.
        assert rows[existing].last_status == "HEALTHY"


# --- run_health_scheduler_loop ---


def _device_data():
    return {
        "id": str(uuid.uuid4()),
        "template_id": str(uuid.uuid4()),
        "name": "dev",
    }


async def _seed_due_row(device_id: uuid.UUID, now: datetime) -> None:
    async with TestSessionLocal() as db:
        db.add(
            DeviceHealthStatus(
                device_id=device_id,
                next_poll_at=now - timedelta(seconds=5),
                last_status="UNKNOWN",
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_loop_polls_due_device_then_cancels(monkeypatch):
    """One tick fires fire_poll for a registered, due device; then sleep cancels."""
    device_id = uuid.uuid4()
    await _seed_due_row(device_id, datetime.now(timezone.utc))
    health_scheduler._registry[device_id] = 60
    # Skip the registry refresh network call entirely.
    monkeypatch.setattr(health_scheduler, "_refresh_registry_if_due", AsyncMock())

    fired = []

    async def _fake_fire(session_factory, dev_id, interval, nc=None):
        fired.append((dev_id, interval))

    monkeypatch.setattr(health_scheduler, "fire_poll", _fake_fire)

    # First sleep ends the loop.
    async def _sleep_then_cancel(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_scheduler.asyncio, "sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await run_health_scheduler_loop(TestSessionLocal)

    assert fired == [(device_id, 60)]


@pytest.mark.asyncio
async def test_loop_pushes_forward_device_dropped_from_registry(monkeypatch):
    """A due row whose device is no longer in the registry is deferred, not polled."""
    device_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await _seed_due_row(device_id, now)
    # Registry intentionally empty: device dropped.
    monkeypatch.setattr(health_scheduler, "_refresh_registry_if_due", AsyncMock())

    fire = AsyncMock()
    monkeypatch.setattr(health_scheduler, "fire_poll", fire)

    async def _sleep_then_cancel(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_scheduler.asyncio, "sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await run_health_scheduler_loop(TestSessionLocal)

    fire.assert_not_called()
    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        nxt = row.next_poll_at
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        # Pushed out by the registry refresh interval so it does not resweep.
        assert nxt > now + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_loop_swallows_fire_poll_crash(monkeypatch):
    """A fire_poll that raises is logged and does not break the tick."""
    device_id = uuid.uuid4()
    await _seed_due_row(device_id, datetime.now(timezone.utc))
    health_scheduler._registry[device_id] = 60
    monkeypatch.setattr(health_scheduler, "_refresh_registry_if_due", AsyncMock())

    async def _boom(*a, **kw):
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(health_scheduler, "fire_poll", _boom)

    async def _sleep_then_cancel(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_scheduler.asyncio, "sleep", _sleep_then_cancel)

    # Must not raise RuntimeError; the CancelledError from sleep ends the loop.
    with pytest.raises(asyncio.CancelledError):
        await run_health_scheduler_loop(TestSessionLocal)


@pytest.mark.asyncio
async def test_loop_cancelled_mid_tick_exits(monkeypatch):
    """A CancelledError raised inside the tick body propagates out of the loop."""

    async def _cancel(*a, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_scheduler, "_refresh_registry_if_due", _cancel)

    # If the loop wrongly swallowed CancelledError it would reach sleep; guard.
    monkeypatch.setattr(
        health_scheduler.asyncio,
        "sleep",
        AsyncMock(side_effect=AssertionError("should not sleep after cancel")),
    )

    with pytest.raises(asyncio.CancelledError):
        await run_health_scheduler_loop(TestSessionLocal)


@pytest.mark.asyncio
async def test_loop_skips_when_claim_lost(monkeypatch):
    """A due, registered device whose claim is lost to a racing tick is not polled."""
    device_id = uuid.uuid4()
    await _seed_due_row(device_id, datetime.now(timezone.utc))
    health_scheduler._registry[device_id] = 60
    monkeypatch.setattr(health_scheduler, "_refresh_registry_if_due", AsyncMock())
    monkeypatch.setattr(health_scheduler, "_claim_row", AsyncMock(return_value=False))

    fire = AsyncMock()
    monkeypatch.setattr(health_scheduler, "fire_poll", fire)

    async def _sleep_then_cancel(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_scheduler.asyncio, "sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await run_health_scheduler_loop(TestSessionLocal)

    fire.assert_not_called()


@pytest.mark.asyncio
async def test_loop_tick_failure_backs_off(monkeypatch):
    """A tick that raises is caught; the loop sleeps on the backoff path then cancels."""
    # Make the refresh raise to drive the tick-failed branch.
    monkeypatch.setattr(
        health_scheduler,
        "_refresh_registry_if_due",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    sleeps = []

    async def _record_then_cancel(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_scheduler.asyncio, "sleep", _record_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await run_health_scheduler_loop(TestSessionLocal)

    # On a failed tick the backoff is base*2 (base tick = 30 by default).
    assert sleeps and sleeps[0] >= health_scheduler.settings.health_poll_scheduler_tick_seconds


# --- start_health_scheduler / stop_health_scheduler ---


@pytest.mark.asyncio
async def test_start_health_scheduler_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_scheduler_enabled", False)
    app = SimpleNamespace(state=SimpleNamespace())
    await start_health_scheduler(app)
    assert not hasattr(app.state, "health_scheduler_task")


@pytest.mark.asyncio
async def test_start_and_stop_health_scheduler_roundtrip(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_scheduler_enabled", True)

    # Replace the loop body with one that idles until cancelled, so the task
    # starts cleanly and stop can cancel/await it without real polling.
    started = asyncio.Event()

    async def _idle_loop(session_factory, nc=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(health_scheduler, "run_health_scheduler_loop", _idle_loop)

    app = SimpleNamespace(state=SimpleNamespace(nats=None))
    await start_health_scheduler(app)
    task = app.state.health_scheduler_task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await stop_health_scheduler(app)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_start_health_scheduler_surfaces_crash(monkeypatch):
    """If the loop task dies with an exception, the done-callback logs it.

    Drives the _surface_crash callback's exception branch: the loop raises
    immediately, the task completes with that exception, and the callback reads
    t.exception() and logs an error (we assert the log was emitted).
    """
    monkeypatch.setattr(health_scheduler.settings, "health_poll_scheduler_enabled", True)

    async def _crash_loop(session_factory, nc=None):
        raise RuntimeError("loop crashed on startup")

    monkeypatch.setattr(health_scheduler, "run_health_scheduler_loop", _crash_loop)

    logged = []
    monkeypatch.setattr(
        health_scheduler.logger,
        "error",
        lambda msg, *a, **kw: logged.append(msg % a if a else msg),
    )

    app = SimpleNamespace(state=SimpleNamespace(nats=None))
    await start_health_scheduler(app)
    task = app.state.health_scheduler_task

    # Let the task run to completion so the done-callback fires.
    with pytest.raises(RuntimeError):
        await task

    assert any("exited unexpectedly" in m for m in logged)


@pytest.mark.asyncio
async def test_stop_health_scheduler_no_task_is_noop():
    app = SimpleNamespace(state=SimpleNamespace())
    # No health_scheduler_task attribute: must return without raising.
    await stop_health_scheduler(app)
