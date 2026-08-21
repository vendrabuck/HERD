"""Unit tests for the stale-run reaper (issue #528).

A hard process death mid-run (OOM kill, container crash, power loss) never
reaches ldap_sync_service.execute_run's finally block, so the row created by
create_run is stuck at status "running" with finished_at null forever: the
interval loop's retention prune deliberately never touches running rows, and
nothing else flips them. reap_stale_running_runs is the backstop: a single
compare-and-swap UPDATE ... WHERE status = 'running' AND started_at < cutoff
that flips a genuinely stale row to "failed" while leaving a row a racing
legitimate finalization already claimed untouched.

Three properties beyond the happy path are pinned here because each one is a
way the reaper could damage live audit state rather than repair dead state:

- the CAS really is a CAS (test_cas_leaves_a_row_a_racing_finalize_claimed_
  between_cutoff_and_update drives a genuine interleave from a second
  session, not a pre-arranged row state);
- the threshold used is the CLAMPED one from
  config.effective_ldap_sync_run_stale_seconds, never the raw setting, so a
  too-short tuning value cannot fail runs that are merely slow;
- the reap fires INSIDE the run-serialization slot, so a caller that ends in
  SyncBusyError (the one moment another run is provably alive) reaps
  nothing.

Two tests then drive the WHOLE path, one per production entry point
(run_sync and start_background_run): a corpse seeded in the same database
the reaper will open, the real entry point called with nothing about the
reaper stubbed, and the corpse asserted flipped afterwards. They run against
app.database, the DEFAULT session factory, precisely because that default is
what production depends on: unlike the other LDAP sync test files, this one
installs NO module-wide use_reap_session_factory override (only two isolated
tests below opt into one), so a broken default cannot hide behind it here.

Follows test_ldap_sync_service.py's engine/session fixture pattern (a
dedicated in-memory SQLite engine, autouse create_all/drop_all, a plain `db`
fixture yielding one session) for everything that calls the reaper directly.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.config import effective_ldap_sync_run_stale_seconds, settings
from app.database import Base
from app.models.ldap_sync_run import LdapSyncRun
from app.services import ldap_sync_service
from sqlalchemy import Update, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Pinned so the effective threshold is deterministic AND above its own floor:
# the clamp is max(60, 2 * effective interval), which is 120 here, so 400
# passes through untouched and a row's age can be compared against it
# directly. Tests that exercise the clamp itself re-pin both values.
INTERVAL_SECONDS = 60
STALE_SECONDS = 400


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
    7200s production default, and pin the interval it is floored against so
    the pinned threshold is not silently clamped out from under them."""
    monkeypatch.setattr(settings, "ldap_sync_interval_seconds", INTERVAL_SECONDS, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_run_stale_seconds", STALE_SECONDS, raising=False)
    return STALE_SECONDS


def _records(caplog, logger_name: str, action: str) -> list[logging.LogRecord]:
    """Caplog records from ONE logger carrying ONE structured action.

    Filtering on the `extra` attribute alone would AttributeError the moment
    any other library logs a record during the block, and filtering on the
    logger alone would pick up unrelated records from the same module: both
    are read for attributes (.count, .floor_seconds) that only these records
    carry, so the filter has to be the narrow one.
    """
    return [
        r for r in caplog.records if r.name == logger_name and getattr(r, "action", None) == action
    ]


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


def test_pinned_threshold_is_above_its_own_floor():
    """Guards the fixture: every age-based assertion below is written against
    STALE_SECONDS, which is only meaningful while the clamp leaves it alone."""
    assert effective_ldap_sync_run_stale_seconds() == STALE_SECONDS


@pytest.mark.asyncio
async def test_stale_running_row_flips_to_failed_with_exact_error_and_finished_at(db):
    old_started = _now() - timedelta(seconds=STALE_SECONDS * 2)
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
    old_started = _now() - timedelta(seconds=STALE_SECONDS * 5)
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
async def test_multiple_stale_rows_are_all_reaped_in_one_pass(db, caplog):
    """A crash loop leaves one corpse per restart, so the reaper must be a
    set operation, not a "newest corpse" special case: the rowcount it
    returns and the count it logs are the whole matched set."""
    old_started = _now() - timedelta(seconds=STALE_SECONDS * 2)
    run_ids = [await _mk_run(db, status="running", started_at=old_started) for _ in range(3)]
    fresh_id = await _mk_run(db, status="running", started_at=_now())

    with caplog.at_level(logging.WARNING, logger="app.services.ldap_sync_service"):
        reaped = await ldap_sync_service.reap_stale_running_runs(db)

    assert reaped == 3
    for run_id in run_ids:
        row = await _fetch(db, run_id)
        assert row.status == "failed"
        assert row.error == ldap_sync_service.STALE_RUN_ERROR
        assert row.finished_at is not None
    assert (await _fetch(db, fresh_id)).status == "running"

    reaped_records = _records(
        caplog, "app.services.ldap_sync_service", "ldap_sync_stale_run_reaped"
    )
    assert len(reaped_records) == 1
    assert reaped_records[0].count == 3
    assert reaped_records[0].getMessage().startswith("Reaped")


@pytest.mark.asyncio
async def test_cas_leaves_a_row_a_racing_finalize_claimed_between_cutoff_and_update(
    tmp_path, monkeypatch
):
    """The race the compare-and-swap exists to close, driven for real: a
    legitimate finalization (a slow-but-alive run's own execute_run finally
    block, on its OWN session) commits between this reaper's cutoff
    computation and its UPDATE. The reaper must not clobber it.

    The interleave is forced by wrapping the reaper session's execute(): the
    wrapper sees the reaper's UPDATE statement (so the cutoff is already
    computed), lets a SECOND session flip the row to "success" and commit,
    and only then runs the UPDATE. The WHERE status = 'running' clause is
    what makes that UPDATE match zero rows.

    Runs against a FILE-backed SQLite database in tmp_path, not this
    module's in-memory engine: two genuinely independent connections are the
    point of the test, and a ":memory:" engine hands every session the same
    StaticPool connection, which would make the "second session" a fiction.
    """
    race_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db", echo=False)
    RaceSessionLocal = async_sessionmaker(race_engine, expire_on_commit=False)
    try:
        async with race_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        old_started = _now() - timedelta(seconds=STALE_SECONDS * 2)
        async with RaceSessionLocal() as seed:
            run_id = await _mk_run(seed, status="running", started_at=old_started)

        interleaves = {"count": 0}
        async with RaceSessionLocal() as reaper_session:
            original_execute = reaper_session.execute

            async def execute_with_racing_finalize(statement, *args, **kwargs):
                if isinstance(statement, Update) and interleaves["count"] == 0:
                    interleaves["count"] += 1
                    async with RaceSessionLocal() as racer:
                        row = await _fetch(racer, run_id)
                        row.status = "success"
                        row.error = None
                        row.finished_at = _now()
                        await racer.commit()
                return await original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(reaper_session, "execute", execute_with_racing_finalize)
            reaped = await ldap_sync_service.reap_stale_running_runs(reaper_session)

        assert interleaves["count"] == 1, "the racing finalize never ran; the test proved nothing"
        assert reaped == 0
        async with RaceSessionLocal() as check:
            row = await _fetch(check, run_id)
            assert row.status == "success"
            assert row.error is None
    finally:
        await race_engine.dispose()


@pytest.mark.asyncio
async def test_own_session_reap_swallows_a_raising_reaper(monkeypatch, use_reap_session_factory):
    """_reap_stale_running_runs_on_own_session in isolation: the reaper is
    housekeeping, so a raise from it becomes a logged 0, never an exception
    its caller (a sync run) has to survive."""

    async def boom(_db):
        raise RuntimeError("boom: reaper machinery broke")

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", boom)
    use_reap_session_factory(TestSessionLocal)

    result = await ldap_sync_service._reap_stale_running_runs_on_own_session()
    assert result == 0


def test_reap_session_factory_defaults_to_the_application_factory():
    """The production default, asserted rather than assumed: this file's
    end-to-end tests below seed their corpse into app.database on the strength
    of it, and nothing in this module overrides it."""
    from app import database

    assert ldap_sync_service._reap_session_factory is None
    assert ldap_sync_service.reap_session_factory() is database.AsyncSessionLocal


class _TeardownRaisingSession:
    """A session context manager that does its job and then explodes on
    CLOSE: the shape of a connection handed back to a broken pool. Nothing
    inside the reap fails, so only a try that wraps the whole `async with`
    catches this."""

    def __init__(self, inner):
        self._inner = inner

    async def __aenter__(self):
        return await self._inner.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        await self._inner.__aexit__(exc_type, exc, tb)
        raise RuntimeError("session teardown exploded")


@pytest.mark.asyncio
async def test_run_sync_survives_a_reap_session_whose_teardown_raises(
    db, use_reap_session_factory, caplog
):
    """The reaper's "never fails the sync run" contract covers the session's
    own teardown, not just the UPDATE: with the try inside the `async with`,
    a raising __aexit__ escapes into run_sync and takes a healthy sync run
    down with it.

    The reap still COMMITS before the explosion, so the corpse is flipped and
    the run finalizes normally: exactly one swallowed-failure record, and no
    exception out of run_sync.
    """
    old_started = _now() - timedelta(seconds=STALE_SECONDS * 2)
    async with TestSessionLocal() as seed:
        corpse_id = await _mk_run(seed, status="running", started_at=old_started)

    use_reap_session_factory(lambda: _TeardownRaisingSession(TestSessionLocal()))

    with caplog.at_level(logging.ERROR, logger="app.services.ldap_sync_service"):
        run = await ldap_sync_service.run_sync(db, trigger="manual")

    assert run.status in ("success", "partial")
    assert run.finished_at is not None
    async with TestSessionLocal() as check:
        corpse = await _fetch(check, corpse_id)
        assert corpse.status == "failed"
        assert corpse.error == ldap_sync_service.STALE_RUN_ERROR
    failures = _records(caplog, "app.services.ldap_sync_service", "ldap_sync_stale_run_reap_failed")
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_run_sync_reaps_inside_the_slot_and_survives_a_reaper_failure(db, monkeypatch):
    """run_sync calls the reaper at the start of every run, INSIDE the
    run-serialization slot (asserted by checking the module lock is held at
    reap time), and a reaper failure must never fail the sync run itself."""
    calls = {"count": 0, "lock_held": []}

    async def boom(_db):
        calls["count"] += 1
        calls["lock_held"].append(ldap_sync_service._sync_lock.locked())
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", boom)

    run = await ldap_sync_service.run_sync(db, trigger="manual")

    assert calls["count"] == 1
    assert calls["lock_held"] == [True]
    # The sync run itself completed and finalized normally despite the
    # reaper raising.
    assert run.status in ("success", "partial")
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_run_sync_on_a_busy_slot_does_not_reap(db, monkeypatch):
    """The busy path is exactly when a concurrent run is alive and its
    "running" row must not be touched, so SyncBusyError must come out of
    run_sync with the reaper never having been called."""
    calls = {"count": 0}

    async def counting_reap(_db):
        calls["count"] += 1
        return 0

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", counting_reap)

    await ldap_sync_service._sync_lock.acquire()
    try:
        with pytest.raises(ldap_sync_service.SyncBusyError) as exc_info:
            await ldap_sync_service.run_sync(db, trigger="manual")
    finally:
        ldap_sync_service._sync_lock.release()

    assert exc_info.value.reason == "in_process"
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_start_background_run_reaps_inside_the_slot_and_survives_a_failure(monkeypatch):
    """The router's sync-now entry point reaps too, on the same in-slot
    cadence, and a reaper failure must not stop the run from being created
    and scheduled.

    Runs against app.database's engine (matching test_ldap_sync_loop.py's
    pattern) because start_background_run creates the run row on its own
    session from that factory, not on any session a test can hand it.
    """
    from app import database

    calls = {"count": 0, "lock_held": []}
    scheduled: list[uuid.UUID] = []

    async def boom(_db):
        calls["count"] += 1
        calls["lock_held"].append(ldap_sync_service._sync_lock.locked())
        raise RuntimeError("reaper exploded")

    async def fake_background(run_id, lock_conn):
        # Stands in for the real reconcile pass, which is not what this test
        # is about, but keeps its ONE side effect that matters here: the
        # background half owns both locks from the moment it is scheduled
        # and must release them, or every later test sees a busy slot.
        scheduled.append(run_id)
        await ldap_sync_service._release_sync_locks(lock_conn)

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", boom)
    monkeypatch.setattr(ldap_sync_service, "_run_in_background", fake_background)

    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        run_id = await ldap_sync_service.start_background_run("manual")
        if ldap_sync_service._background_tasks:
            await asyncio.gather(*list(ldap_sync_service._background_tasks))

        assert calls["count"] == 1
        assert calls["lock_held"] == [True]
        assert scheduled == [run_id]
        assert not ldap_sync_service._sync_lock.locked()

        async with database.AsyncSessionLocal() as session:
            row = await _fetch(session, run_id)
            assert row.status == "running"
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_start_background_run_on_a_busy_slot_does_not_reap(monkeypatch):
    """Same invariant as run_sync's busy path, on the router's entry point."""
    calls = {"count": 0}

    async def counting_reap(_db):
        calls["count"] += 1
        return 0

    monkeypatch.setattr(ldap_sync_service, "reap_stale_running_runs", counting_reap)

    await ldap_sync_service._sync_lock.acquire()
    try:
        with pytest.raises(ldap_sync_service.SyncBusyError) as exc_info:
            await ldap_sync_service.start_background_run("manual")
    finally:
        ldap_sync_service._sync_lock.release()

    assert exc_info.value.reason == "in_process"
    assert calls["count"] == 0


# ---------------------------------------------------------------------------
# End to end, one per production entry point. Everything above stubs some
# part of the reap to isolate a property; these two stub NOTHING about it.
# The corpse is seeded into app.database, the entry point is the real one,
# and the assertion is that the row came out flipped. That is the only shape
# that proves the wiring: an entry point could call a reaper that quietly
# looks at the wrong database and every isolated test above would still pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sync_end_to_end_flips_a_corpse_in_the_default_database():
    """run_sync, called for real against app.database (the reaper's default
    session factory), flips a corpse that was already sitting there."""
    from app import database

    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        old_started = _now() - timedelta(seconds=STALE_SECONDS * 2)
        async with database.AsyncSessionLocal() as session:
            corpse_id = await _mk_run(session, status="running", started_at=old_started)

        async with database.AsyncSessionLocal() as session:
            run = await ldap_sync_service.run_sync(session, trigger="manual")
            run_id = run.id
            run_status = run.status

        assert run_id != corpse_id
        assert run_status in ("success", "partial")

        async with database.AsyncSessionLocal() as session:
            corpse = await _fetch(session, corpse_id)
            assert corpse.status == "failed"
            assert corpse.error == ldap_sync_service.STALE_RUN_ERROR
            assert corpse.error == "run did not finalize (process died mid-run)"
            assert corpse.finished_at is not None
            # This run's OWN row was created after the reap and is far too
            # young for it: the reaper must never touch the run reaping it.
            live = await _fetch(session, run_id)
            assert live.status == run_status
            assert live.error != ldap_sync_service.STALE_RUN_ERROR
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_start_background_run_end_to_end_flips_a_corpse_in_the_default_database():
    """Same proof on the router's entry point, including its real background
    half (awaited here so the run finalizes before the assertions)."""
    from app import database

    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        old_started = _now() - timedelta(seconds=STALE_SECONDS * 2)
        async with database.AsyncSessionLocal() as session:
            corpse_id = await _mk_run(session, status="running", started_at=old_started)

        run_id = await ldap_sync_service.start_background_run("manual")
        if ldap_sync_service._background_tasks:
            await asyncio.gather(*list(ldap_sync_service._background_tasks))

        assert run_id != corpse_id
        assert not ldap_sync_service._sync_lock.locked()

        async with database.AsyncSessionLocal() as session:
            corpse = await _fetch(session, corpse_id)
            assert corpse.status == "failed"
            assert corpse.error == ldap_sync_service.STALE_RUN_ERROR
            assert corpse.error == "run did not finalize (process died mid-run)"
            assert corpse.finished_at is not None
            live = await _fetch(session, run_id)
            assert live.status in ("success", "partial")
            assert live.error != ldap_sync_service.STALE_RUN_ERROR
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


# --- the effective-threshold clamp (config.effective_ldap_sync_run_stale_seconds) ---


def test_effective_stale_seconds_passes_a_value_above_both_floor_terms_through(monkeypatch):
    monkeypatch.setattr(settings, "ldap_sync_interval_seconds", 3600, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_run_stale_seconds", 86400, raising=False)

    assert effective_ldap_sync_run_stale_seconds() == 86400


def test_effective_stale_seconds_clamps_below_the_absolute_floor_and_warns(monkeypatch, caplog):
    """A tiny interval makes the 2x term smaller than 60, so the absolute
    floor is the binding term: 0 (or any typo) still cannot make the reaper
    fail every run it sees."""
    monkeypatch.setattr(settings, "ldap_sync_interval_seconds", 1, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_run_stale_seconds", 0, raising=False)

    with caplog.at_level(logging.WARNING, logger="app.config"):
        result = effective_ldap_sync_run_stale_seconds()

    # The interval clamps to its own 60s floor first, so 2x it is 120.
    assert result == 120
    clamp_records = _records(caplog, "app.config", "ldap_sync_run_stale_seconds_clamped")
    assert len(clamp_records) == 1
    assert clamp_records[0].configured_seconds == 0
    assert clamp_records[0].floor_seconds == 120


def test_effective_stale_seconds_clamps_a_value_under_twice_the_interval(monkeypatch, caplog):
    """The defect this floor closes: a threshold at or below the interval
    lets the reaper fail a run that is merely slower than one tick, not
    dead. 3600 == the interval, which is why it is not left alone."""
    monkeypatch.setattr(settings, "ldap_sync_interval_seconds", 3600, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_run_stale_seconds", 3600, raising=False)

    with caplog.at_level(logging.WARNING, logger="app.config"):
        result = effective_ldap_sync_run_stale_seconds()

    assert result == 7200
    clamp_records = _records(caplog, "app.config", "ldap_sync_run_stale_seconds_clamped")
    assert [r.floor_seconds for r in clamp_records] == [7200]


def test_production_default_stale_seconds_is_twice_the_default_interval():
    """The shipped defaults must sit exactly ON the floor, not under it: a
    stock deployment must never log the clamp warning."""
    fresh = type(settings).model_fields
    assert fresh["ldap_sync_run_stale_seconds"].default == 7200
    assert fresh["ldap_sync_interval_seconds"].default == 3600


@pytest.mark.asyncio
async def test_reaper_uses_the_clamped_threshold_not_the_raw_setting(db, monkeypatch):
    """A row older than a too-low CONFIGURED threshold but younger than the
    clamped floor must survive: this is the merely-slow run the floor exists
    to protect."""
    monkeypatch.setattr(settings, "ldap_sync_interval_seconds", 60, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_run_stale_seconds", 10, raising=False)
    assert effective_ldap_sync_run_stale_seconds() == 120

    protected_id = await _mk_run(db, status="running", started_at=_now() - timedelta(seconds=60))
    corpse_id = await _mk_run(db, status="running", started_at=_now() - timedelta(seconds=300))

    reaped = await ldap_sync_service.reap_stale_running_runs(db)

    assert reaped == 1
    assert (await _fetch(db, protected_id)).status == "running"
    assert (await _fetch(db, corpse_id)).status == "failed"


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
