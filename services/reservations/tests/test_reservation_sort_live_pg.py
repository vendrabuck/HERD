"""Postgres-live proof that sort_by=status orders alphabetically by status NAME.

Issue #902. `status` is a native Postgres enum, so ORDER BY on the raw column sorts in
the enum's storage order (declaration order on a create_all stack, a different order on
a stack migrated in place by 0006's ADD VALUE), while the SQLite unit test
(test_reservations.py::test_sort_status_both_directions) runs on a VARCHAR column and
asserts alphabetical order. The fix casts the column to String for the ORDER BY. SQLite
cannot see this divergence, so this suite runs the real production list function against
a real Postgres.

Env contract identical to test_reservation_status_cas_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN of the database to use. Unset or
                            empty, the suite is skipped and connects to nothing.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns that skip into
                            a hard failure. A named database that does not answer
                            fails every test either way (herd_common.pg_live_suite).

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database. Every row here belongs to a random user_id, the production list path is called
scoped to that user (so foreign rows cannot appear), the assertions are on the relative
order of this test's own ids only, no total count of the table is asserted, every other
reservation row is snapshotted before and after and must be unchanged, and the rows are
deleted in a finally. End times are an hour ahead so a live sweeper never touches them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.services.reservation_service import list_user_reservations
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn

_CONNECT_ARGS = {"server_settings": {"search_path": "reservations"}}


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture
async def pg_engine():
    engine = _make_engine()
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)


async def _foreign_snapshot(session_factory, user_id):
    async with session_factory() as s:
        rows = await s.execute(
            select(Reservation.id, Reservation.status).where(Reservation.user_id != user_id)
        )
        return {r.id: r.status for r in rows}


@pytest.fixture
async def six_rows(session_factory):
    """One reservation per status, owned by a random user; yields (user_id, id_by_status,
    foreign snapshot taken before insert). Deleted in the finally."""
    user_id = uuid.uuid4()
    before = await _foreign_snapshot(session_factory, user_id)
    now = datetime.now(timezone.utc)
    id_by_status = {st: uuid.uuid4() for st in ReservationStatus}
    try:
        async with session_factory() as s:
            for st, rid in id_by_status.items():
                s.add(
                    Reservation(
                        id=rid,
                        user_id=user_id,
                        device_ids=[],
                        topology_type=TopologyType.PHYSICAL,
                        purpose="live pg sort",
                        start_time=now + timedelta(hours=1),
                        end_time=now + timedelta(hours=2),
                        status=st,
                    )
                )
            await s.commit()
        yield user_id, id_by_status, before
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM reservations WHERE user_id = :u"), {"u": user_id})
            await s.commit()


async def _sorted_statuses(session_factory, user_id, id_by_status, sort_dir):
    by_id = {rid: st for st, rid in id_by_status.items()}
    async with session_factory() as db:
        rows, _total = await list_user_reservations(
            db, user_id, skip=0, limit=50, sort_by="status", sort_dir=sort_dir
        )
    return [by_id[r.id].name for r in rows if r.id in by_id]


async def test_sort_by_status_ascending_is_alphabetical_on_postgres(session_factory, six_rows):
    user_id, id_by_status, before = six_rows
    got = await _sorted_statuses(session_factory, user_id, id_by_status, "asc")
    assert got == sorted(st.name for st in ReservationStatus)
    assert await _foreign_snapshot(session_factory, user_id) == before


async def test_sort_by_status_descending_is_alphabetical_on_postgres(session_factory, six_rows):
    user_id, id_by_status, before = six_rows
    got = await _sorted_statuses(session_factory, user_id, id_by_status, "desc")
    assert got == sorted((st.name for st in ReservationStatus), reverse=True)
    assert await _foreign_snapshot(session_factory, user_id) == before
