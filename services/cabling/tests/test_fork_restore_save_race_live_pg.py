"""Postgres-live coverage for the fork row lock closing issue #626.

restore_fork_version_internal and the save path (save_fork_internal calling
fork_save_service.save_fork) both used to load the fork row through a plain
SELECT with no lock. A restore that committed between a save's own read and its
own commit was silently overwritten: the save's stale in-memory
draft_restored_from_id (already decided as the clear, None) won the write, and
the restore's freshly set marker was lost even though the invariant is that a
restore marker is either consumed by exactly one appended fork_versions row or
still present on the fork row, never lost. The fix (app/routes/forks.py's
``_load_fork(..., for_update=True)``) takes a real row lock that every mutating
route now holds from its load through its own final commit or rollback.

SQLAlchemy only emits FOR UPDATE on dialects that support it, so the SQLite
unit suite (test_fork_versions.py's
test_save_after_restore_carries_marker_and_clears_it) exercises the
marker-consuming logic but proves nothing about actual blocking: a SQLite
session never contends for the row at all. This file is the missing other
half, proving the lock genuinely serializes two independent Postgres sessions
in both commit orders, using the real ORM models and the real production
functions (``_load_fork``, ``save_fork``) against a live database.

Env contract identical to services/common/tests/test_advisory_lock_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an
                            unreachable server into a hard failure instead of
                            the normal skip.

This file creates exactly one throwaway reservation_fork row per test (a
random UUID reservation_id, no cross-schema FK, so nothing else in the
database references it) and deletes it in a finally; fork_versions rows
cascade-delete with it (ON DELETE CASCADE on fork_versions.fork_id).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from app.models.fork import ForkConnection, ForkVersion, ReservationFork
from app.routes.forks import _load_fork
from app.services.fork_save_service import save_fork
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")


async def _pg_reachable() -> bool:
    engine = create_async_engine(PG_DSN)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def _run_sync_reachable() -> bool:
    return asyncio.run(_pg_reachable())


_PG_REACHABLE = _run_sync_reachable()

pytestmark = pytest.mark.skipif(
    not _PG_REQUIRED and not _PG_REACHABLE,
    reason=(
        f"No Postgres reachable at {PG_DSN!r}; set HERD_TEST_PG_DSN to point at one "
        "(e.g. the gate stack's published postgres port, 5433) to run this suite."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _PG_REQUIRED and not _PG_REACHABLE:
        pytest.fail(
            f"HERD_TEST_PG_REQUIRED is set but no Postgres is reachable at {PG_DSN!r}; "
            "start one or unset HERD_TEST_PG_REQUIRED."
        )


@pytest.fixture(autouse=True)
def _use_cabling_schema():
    """Point the ORM Table objects at the real "cabling" schema for this file only.

    services/cabling/conftest.py forces DB_SCHEMA="" for the SQLite unit suite
    (Settings() is a process-wide singleton fixed at first import), so
    ReservationFork/ForkVersion/ForkConnection's mapped Table objects carry
    schema=None by the time this module's tests run. The live database's
    migrations created these tables under the real "cabling" schema, so this
    fixture restores it around each test only, never at import time: a skipped
    run here (no live Postgres reachable) never mutates anything, since skip
    happens before fixture setup, and a combined `pytest tests/` run leaves
    every other file's SQLite queries untouched between tests because the
    schema is reset in the finally before the next test runs.
    """
    tables = (ReservationFork.__table__, ForkVersion.__table__, ForkConnection.__table__)
    original = tuple(t.schema for t in tables)
    for t in tables:
        t.schema = "cabling"
    try:
        yield
    finally:
        for t, schema in zip(tables, original, strict=True):
            t.schema = schema


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(PG_DSN)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database, the
    # production session factory shape every service actually runs with.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


V1_CANVAS = {"nodes": [{"id": "v1-marker"}], "edges": []}
SAVE_CANVAS = {"nodes": [{"id": "save-marker"}], "edges": []}


async def _make_fork_with_v1(session_factory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Persist a throwaway ACTIVE fork with a v1 snapshot.

    Returns (reservation_id, fork_id, v1_id).
    """
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        fork = ReservationFork(reservation_id=reservation_id, canvas_data=V1_CANVAS)
        db.add(fork)
        await db.flush()
        v1 = ForkVersion(fork_id=fork.id, version_number=1, canvas_data=V1_CANVAS)
        db.add(v1)
        await db.commit()
        return reservation_id, fork.id, v1.id


async def _delete_fork(session_factory, fork_id: uuid.UUID) -> None:
    async with session_factory() as db:
        await db.execute(delete(ReservationFork).where(ReservationFork.id == fork_id))
        await db.commit()


async def _reread_fork(session_factory, fork_id: uuid.UUID) -> ReservationFork:
    async with session_factory() as db:
        return (
            await db.execute(select(ReservationFork).where(ReservationFork.id == fork_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_save_holds_lock_so_a_racing_restore_waits_and_wins_last(session_factory):
    """Ordering (a): a save in flight blocks a concurrent restore's load.

    Session A (save) loads the fork FOR UPDATE and holds the transaction open.
    Session B (restore), started concurrently, must not complete its own FOR
    UPDATE load while A holds the lock: asyncio.wait_for on a shielded handle to
    B's task raises TimeoutError, proving B is genuinely blocked at the database,
    not just slow. A then completes its save, releasing the lock; B's blocked
    load then proceeds against A's already-committed row and completes within
    2s. Since B commits strictly after A in real time, B's write legitimately
    wins last (canvas is last-writer-wins by design, and here nothing raced
    B's own write): the final row's draft_restored_from_id and canvas_data both
    reflect B's restore, not A's save.
    """
    reservation_id, fork_id, v1_id = await _make_fork_with_v1(session_factory)
    db_a = session_factory()
    try:
        fork_a = await _load_fork(db_a, reservation_id, for_update=True)

        async def _restore() -> None:
            async with session_factory() as db_b:
                fork_b = await _load_fork(db_b, reservation_id, for_update=True)
                version = await db_b.get(ForkVersion, v1_id)
                fork_b.canvas_data = version.canvas_data
                fork_b.draft_restored_from_id = version.id
                await db_b.commit()

        task_b = asyncio.create_task(_restore())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), timeout=0.5)
        assert not task_b.done(), (
            "restore's FOR UPDATE load should still be blocked by the save's row lock"
        )

        # A completes its save while it was the only one holding the lock.
        # SAVE_CANVAS has no device nodes, so an empty member set is a no-op for
        # both the endpoint-membership check (#701) and the port-claim lock (#721).
        await save_fork(
            db_a, fork_a, canvas_data=SAVE_CANVAS, member_device_ids=set(), created_by="race-test-a"
        )
        await db_a.close()

        await asyncio.wait_for(task_b, timeout=2.0)

        final = await _reread_fork(session_factory, fork_id)
        assert final.draft_restored_from_id == v1_id
        assert final.canvas_data == V1_CANVAS
    finally:
        # AsyncSession.close() rolls back any open transaction on its own and is
        # safe to call more than once, so no need to branch on session state here.
        await db_a.close()
        await _delete_fork(session_factory, fork_id)


@pytest.mark.asyncio
async def test_restore_holds_lock_so_a_racing_save_consumes_the_fresh_marker(session_factory):
    """Ordering (b): a restore in flight blocks a concurrent save's load.

    Session B (restore) loads the fork FOR UPDATE and holds the transaction
    open, then commits a fresh marker. Session A (save), started concurrently,
    must not complete its own FOR UPDATE load while B holds the lock: this is
    the exact scenario issue #626 describes, a save in flight while a restore
    lands. Without the lock, A would have captured a stale (pre-restore) marker
    value and clobbered B's fresh one on its own later commit. With the lock,
    A's blocked load only proceeds once it can see B's already-committed row,
    so it captures B's fresh marker and correctly consumes it: the appended
    version carries restored_from_id == v1_id and the row's marker is cleared.
    """
    reservation_id, fork_id, v1_id = await _make_fork_with_v1(session_factory)
    db_b = session_factory()
    try:
        fork_b = await _load_fork(db_b, reservation_id, for_update=True)
        version = await db_b.get(ForkVersion, v1_id)

        async def _save():
            async with session_factory() as db_a:
                fork_a = await _load_fork(db_a, reservation_id, for_update=True)
                # SAVE_CANVAS has no device nodes, so an empty member set is a
                # no-op for the endpoint-membership check (#701) and the
                # port-claim lock (#721).
                return await save_fork(
                    db_a,
                    fork_a,
                    canvas_data=SAVE_CANVAS,
                    member_device_ids=set(),
                    created_by="race-test-b",
                )

        task_a = asyncio.create_task(_save())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_a), timeout=0.5)
        assert not task_a.done(), (
            "save's FOR UPDATE load should still be blocked by the restore's row lock"
        )

        # B completes its restore while it was the only one holding the lock.
        fork_b.canvas_data = version.canvas_data
        fork_b.draft_restored_from_id = version.id
        await db_b.commit()
        await db_b.close()

        result = await asyncio.wait_for(task_a, timeout=2.0)
        assert result.version_number == 2

        final = await _reread_fork(session_factory, fork_id)
        assert final.draft_restored_from_id is None
        assert final.canvas_data == SAVE_CANVAS

        async with session_factory() as db:
            versions = (
                (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
                .scalars()
                .all()
            )
        appended = next(v for v in versions if v.version_number == 2)
        assert appended.restored_from_id == v1_id
    finally:
        # AsyncSession.close() rolls back any open transaction on its own and is
        # safe to call more than once, so no need to branch on session state here.
        await db_b.close()
        await _delete_fork(session_factory, fork_id)
