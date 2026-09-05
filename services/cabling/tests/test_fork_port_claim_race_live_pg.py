"""Postgres-live coverage for the fork port-claim lock (issue #721, ADR 0006
Decision 4 amendment).

ADR 0006 Decision 4's cross-reservation port-claim check
(``fork_save_service.assert_no_port_claims``) used to be a plain SELECT against
other ACTIVE forks' committed rows, run with no coordination at all. The
engine runs at Postgres default READ COMMITTED
(``herd_common/database.py``), so two concurrent saves on two DIFFERENT
forks each ran that SELECT against the other's still-uncommitted insert:
textbook write skew. Neither saw a conflict, both inserted, both committed,
and two ACTIVE forks ended up holding a fork_connections row for the exact
same physical ``(device_id, port)`` endpoint, the very thing Decision 4 says
must never happen. The version-allocation retry loop that was meant to catch
the residual window only ever fires on a SAME-fork version collision, which
this check never flags (its own rows are excluded), so it never closed this
gap.

The fix (``fork_save_service.lock_port_claims``) takes a transaction-scoped
Postgres advisory lock over every claimed ``(device_id, port)`` pair,
keyed exactly like reservations' ``_acquire_device_locks``, immediately
before the claim SELECT. The second writer blocks at the database until the
first writer's transaction commits or rolls back, so its own claim check
then reads the winner's committed rows and 409s correctly.

SQLite (this suite's usual dialect) never emits real advisory-lock SQL, so no
test in test_forks.py can prove two independent sessions actually contend at
the database: a SQLite session never blocks another SQLite session at all.
This file is that missing proof, run against a real Postgres, using the same
env contract as test_fork_restore_save_race_live_pg.py:

    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an
                            unreachable server into a hard failure instead of
                            the normal skip.

This file creates exactly one throwaway Connection row and two throwaway
reservation_fork rows per test (random UUID reservation_ids, no cross-schema
FK, so nothing else in the database references them) and deletes them in a
finally; fork_versions and fork_connections rows cascade-delete with their
fork (ON DELETE CASCADE on fork_versions.fork_id / fork_connections.fork_id).

Proof that this file fails on main without the fix: comment out the
``await lock_port_claims(db, to_build)`` line in
``fork_save_service.save_fork``'s ``reconcile()`` closure (leaving the
``assert_no_port_claims`` call immediately below it in place) and rerun
``test_concurrent_saves_on_two_forks_cannot_both_claim_the_same_port``: both
saves succeed (the ``len(successes) == 1`` assertion fails, and the
fork_connections count assertion finds two rows instead of one), then
restore the line.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from app.models.connection import Connection
from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkVersion, ReservationFork
from app.services import fork_save_service
from app.services.fork_save_service import save_fork
from fastapi import HTTPException
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

    Mirrors test_fork_restore_save_race_live_pg.py's fixture of the same name:
    services/cabling/conftest.py forces DB_SCHEMA="" for the SQLite unit suite, so
    every cabling model's mapped Table carries schema=None by the time this module's
    tests run. The live database's migrations created these tables under the real
    "cabling" schema. Includes Connection alongside the fork models since this file
    also seeds one throwaway physical connection.
    """
    tables = (
        ReservationFork.__table__,
        ForkVersion.__table__,
        ForkConnection.__table__,
        Connection.__table__,
    )
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


async def _make_physical(session_factory, da, pa, db_dev, pb) -> uuid.UUID:
    async with session_factory() as db:
        conn = Connection(
            device_a_id=da, port_a=pa, device_b_id=db_dev, port_b=pb, created_by="lock-race-test"
        )
        db.add(conn)
        await db.commit()
        return conn.id


async def _make_empty_active_fork(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Persist a throwaway ACTIVE fork with an empty v1 snapshot (no wiring yet).

    Returns (reservation_id, fork_id).
    """
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        fork = ReservationFork(reservation_id=reservation_id, status=ForkStatus_ACTIVE)
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        await db.commit()
        return reservation_id, fork.id


async def _delete_fork(session_factory, fork_id: uuid.UUID) -> None:
    async with session_factory() as db:
        await db.execute(delete(ReservationFork).where(ReservationFork.id == fork_id))
        await db.commit()


async def _delete_connection(session_factory, connection_id: uuid.UUID) -> None:
    async with session_factory() as db:
        await db.execute(delete(Connection).where(Connection.id == connection_id))
        await db.commit()


def _canvas(source: uuid.UUID, target: uuid.UUID) -> dict:
    return {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(source)}}},
            {"id": "n2", "data": {"device": {"id": str(target)}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }


@pytest.mark.asyncio
async def test_concurrent_saves_on_two_forks_cannot_both_claim_the_same_port(session_factory):
    """The core issue #721 proof: two ACTIVE forks concurrently save wiring that
    resolves to the SAME physical (device, port) pair on both ends. An
    ``asyncio.Barrier`` instruments ``assert_no_port_claims`` so that if a save's
    claim check ever runs with nothing blocking it, it waits briefly for a second
    concurrent check to also land before either save proceeds to insert and commit:
    this deterministically forces the write-skew window the bug lived in, rather
    than leaving the interleaving to scheduler luck. With ``lock_port_claims`` in
    place immediately before the claim check, only ONE save's check can ever run at
    a time (the other blocks acquiring the advisory lock until the winner's
    transaction ends), so the barrier wait always just times out for the winner
    with nobody else arriving, and the loser's own check runs only after the
    winner already committed, finds the winner's row, and 409s naming it: exactly
    one save succeeds, and a third, independent session finds exactly one
    fork_connections row for the contested (device, port) pair across both forks.

    Without the lock (temporarily commented out to reproduce the bug, see the
    module docstring), both checks land inside the barrier concurrently, neither
    sees the other's uncommitted row, both proceed to insert and commit, and both
    assertions below fail: two saves succeed and two fork_connections rows exist
    for the same physical port.
    """
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    connection_id = await _make_physical(session_factory, s1, "eth5", s2, "eth6")
    rid_a, fork_a_id = await _make_empty_active_fork(session_factory)
    rid_b, fork_b_id = await _make_empty_active_fork(session_factory)
    canvas = _canvas(s1, s2)

    barrier = asyncio.Barrier(2)
    original_assert_no_port_claims = fork_save_service.assert_no_port_claims

    async def instrumented_assert_no_port_claims(db, fork_id, to_build):
        # Run the real check first: on the buggy path (no lock) this passes clean
        # for BOTH concurrent callers, since neither has committed yet. On the
        # fixed path, only the winner ever reaches this line concurrently with
        # nobody else; the loser's own call arrives strictly after the winner's
        # commit and raises here instead of ever reaching the barrier below.
        await original_assert_no_port_claims(db, fork_id, to_build)
        try:
            # Force both callers to reach this point before either is allowed to
            # proceed to insert+commit. On the fixed path nobody else ever
            # arrives (the other caller is blocked earlier, inside
            # lock_port_claims), so this always just times out quickly; that
            # timeout IS the lock doing its job, not a test failure.
            await asyncio.wait_for(barrier.wait(), timeout=0.5)
        except TimeoutError:
            pass

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                fork_save_service, "assert_no_port_claims", instrumented_assert_no_port_claims
            )

            async def _do_save(fork_id: uuid.UUID):
                async with session_factory() as db:
                    fork = await db.get(ReservationFork, fork_id)
                    return await save_fork(
                        db,
                        fork,
                        canvas_data=canvas,
                        member_device_ids={s1, s2},
                        created_by="lock-race-test",
                    )

            results = await asyncio.gather(
                _do_save(fork_a_id), _do_save(fork_b_id), return_exceptions=True
            )

        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1, f"expected exactly one save to win, got: {results!r}"
        assert len(failures) == 1, f"expected exactly one save to be refused, got: {results!r}"
        [failure] = failures
        assert isinstance(failure, HTTPException), f"unexpected exception type: {failure!r}"
        assert failure.status_code == 409
        conflicts = failure.detail["conflicts"]
        assert len(conflicts) >= 1
        assert conflicts[0]["reservation_id"] in (str(rid_a), str(rid_b))

        winner = successes[0]
        assert winner.fork_id in (fork_a_id, fork_b_id)

        # A third, independent session finds exactly one fork_connections row for
        # the contested port pair across BOTH forks, and it belongs to the winner.
        async with session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(ForkConnection).where(
                            ForkConnection.fork_id.in_([fork_a_id, fork_b_id])
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, f"expected exactly one surviving fork_connections row, got {rows}"
        assert rows[0].fork_id == winner.fork_id
        assert {rows[0].device_a_id, rows[0].device_b_id} == {s1, s2}
        assert {rows[0].port_a, rows[0].port_b} == {"eth5", "eth6"}
    finally:
        await _delete_fork(session_factory, fork_a_id)
        await _delete_fork(session_factory, fork_b_id)
        await _delete_connection(session_factory, connection_id)
