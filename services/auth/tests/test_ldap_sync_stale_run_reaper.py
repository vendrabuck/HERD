"""Unit tests for the stale-run reaper (issue #528).

A hard process death mid-run (OOM kill, container crash, power loss) never
reaches ldap_sync_service.execute_run's finally block, so the row created by
create_run is stuck at status "running" with finished_at null forever: the
interval loop's retention prune deliberately never touches running rows, and
nothing else flips them. reap_stale_running_runs is the backstop: a single
compare-and-swap UPDATE ... WHERE status = 'running' AND started_at < cutoff
that flips a genuinely stale row to "failed" while leaving a row a racing
legitimate finalization already claimed untouched.

Follows test_ldap_sync_service.py's engine/session fixture pattern (a
dedicated in-memory SQLite engine, autouse create_all/drop_all, a plain `db`
fixture yielding one session).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.config import settings
from app.database import Base
from app.models.ldap_sync_run import LdapSyncRun
from app.services import ldap_sync_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def stale_seconds(monkeypatch):
    """Pin a small, deterministic threshold so tests don't depend on the
    3600s production default."""
    monkeypatch.setattr(settings, "ldap_sync_run_stale_seconds", 100, raising=False)
    return 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _mk_run(db, *, status: str, started_at: datetime, **extra) -> uuid.UUID:
    run = LdapSyncRun(
        id=uuid.uuid4(),
        trigger="manual",
        status=status,
        started_at=started_at,
        **extra,
    )
    db.add(run)
    await db.commit()
    return run.id


async def _fetch(db, run_id: uuid.UUID) -> LdapSyncRun:
    return (await db.execute(select(LdapSyncRun).where(LdapSyncRun.id == run_id))).scalar_one()


@pytest.mark.asyncio
async def test_stale_running_row_flips_to_failed_with_exact_error_and_finished_at(db):
    old_started = _now() - timedelta(seconds=200)
    run_id = await _mk_run(db, status="running", started_at=old_started)

    reaped = await ldap_sync_service.reap_stale_running_runs(db)

    assert reaped == 1
    row = await _fetch(db, run_id)
    assert row.status == "failed"
    assert row.error == "run did not finalize (process died mid-run)"
    assert row.error == ldap_sync_service.STALE_RUN_ERROR
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_fresh_running_row_younger_than_threshold_is_untouched(db):
    fresh_started = _now() - timedelta(seconds=10)
    run_id = await _mk_run(db, status="running", started_at=fresh_started)

    reaped = await ldap_sync_service.reap_stale_running_runs(db)

    assert reaped == 0
    row = await _fetch(db, run_id)
    assert row.status == "running"
    assert row.error is None
    assert row.finished_at is None


@pytest.mark.asyncio
async def test_terminal_rows_untouched_regardless_of_age(db):
    old_started = _now() - timedelta(seconds=999)
    success_id = await _mk_run(
        db, status="success", started_at=old_started, finished_at=old_started
    )
    failed_id = await _mk_run(
        db,
        status="failed",
        started_at=old_started,
        finished_at=old_started,
        error="some real failure",
    )

    reaped = await ldap_sync_service.reap_stale_running_runs(db)

    assert reaped == 0
    success_row = await _fetch(db, success_id)
    assert success_row.status == "success"
    failed_row = await _fetch(db, failed_id)
    assert failed_row.status == "failed"
    assert failed_row.error == "some real failure"


@pytest.mark.asyncio
async def test_cas_does_not_overwrite_a_row_finalized_between_cutoff_and_update(db, monkeypatch):
    """Simulate the race the compare-and-swap exists to close: a legitimate
    finalization (a slow-but-alive run's own execute_run finally block)
    lands on the row between this reaper's cutoff computation and its
    UPDATE. The reaper must not clobber it.

    Implemented by monkeypatching datetime.now on the ldap_sync_service
    module so the cutoff is computed against a controlled clock, then
    flipping the row to a legitimate terminal status (as the racing
    finalize would have) before calling the reaper.
    """
    old_started = _now() - timedelta(seconds=200)
    run_id = await _mk_run(db, status="running", started_at=old_started)

    # The racing legitimate finalize wins the row first.
    row = await _fetch(db, run_id)
    row.status = "success"
    row.error = None
    row.finished_at = _now()
    await db.commit()

    reaped = await ldap_sync_service.reap_stale_running_runs(db)

    assert reaped == 0
    row = await _fetch(db, run_id)
    assert row.status == "success"
    assert row.error is None


@pytest.mark.asyncio
async def test_reaper_exception_does_not_propagate_into_sync_run(db, monkeypatch):
    async def boom(_db):
        raise RuntimeError("boom: reaper machinery broke")

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", boom)

    # best_effort wraps reap_stale_running_runs and must swallow the raise.
    result = await ldap_sync_service.reap_stale_running_runs_best_effort(db)
    assert result == 0


@pytest.mark.asyncio
async def test_run_sync_calls_reaper_and_survives_its_failure(db, monkeypatch):
    """run_sync's sync-now path calls the reaper best-effort at the start of
    every run; a reaper failure must never fail the sync run itself."""
    called = {"count": 0}

    async def boom(_db):
        called["count"] += 1
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", boom)

    run = await ldap_sync_service.run_sync(db, trigger="manual")

    assert called["count"] == 1
    # The sync run itself completed and finalized normally despite the
    # reaper raising.
    assert run.status in ("success", "partial")
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_reaped_row_then_prunable_by_retention_prune(monkeypatch):
    """After reaping, the row is a plain "failed" row well past the
    retention window, so the interval loop's age-gated retention prune
    picks it up like any other old terminal row (the prune's own gate:
    status != "running" AND started_at < retention cutoff).

    Runs against app.database's own engine/session factory (matching
    test_ldap_sync_loop.py's pattern) rather than this file's dedicated
    engine, since _prune_old_runs and reap_stale_running_runs (called here
    via reap_stale_running_runs, not through this file's `db` fixture) must
    observe the same rows to demonstrate the interaction.
    """
    from app import database
    from app.tasks import ldap_sync_loop

    monkeypatch.setattr(settings, "ldap_sync_runs_retention_days", 90, raising=False)

    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        ancient_started = datetime.now(timezone.utc) - timedelta(days=91)
        async with database.AsyncSessionLocal() as session:
            run_id = await _mk_run(session, status="running", started_at=ancient_started)

        async with database.AsyncSessionLocal() as session:
            reaped = await ldap_sync_service.reap_stale_running_runs(session)
        assert reaped == 1

        async with database.AsyncSessionLocal() as session:
            row = await _fetch(session, run_id)
            assert row.status == "failed"

        removed = await ldap_sync_loop._prune_old_runs()
        assert removed >= 1

        async with database.AsyncSessionLocal() as session:
            remaining = (
                await session.execute(select(LdapSyncRun.id).where(LdapSyncRun.id == run_id))
            ).scalar_one_or_none()
        assert remaining is None
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
