"""Postgres-live proof that a connections page walk is complete over a real tie (#1589).

`created_at` is `server_default=func.now()`, and Postgres's `now()` is the start
time of the transaction, so every connection inserted in one transaction (a bulk
cabling run commits once) carries the same value. Offset pages over that tie have
no defined order on Postgres: with a LIMIT the planner sorts with a bounded heap
whose order among equal keys depends on the bound, so two pages can repeat a row
and lose another. `list_connections` ends its order with the id (ascending), a
total order. This suite inserts one device's cables in ONE transaction, checks the
tie is real (one distinct `created_at`), and walks the REAL `list_connections`
with small page sizes, asserting every row exactly once and the id order.

Env contract identical to the sibling cabling live-PG files:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN of the database to use. Unset or
                            empty, the suite is skipped and connects to nothing; a
                            named database that does not answer skips it too.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns either skip
                            into an error at setup (herd_common.pg_live_suite).

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
cabling schema. Every row here names one of two random device ids, every list call
filters by one of them (so foreign rows cannot appear in a page or the total), no
table-wide count is asserted, every other connection row is snapshotted before and
after and must be unchanged, and the rows are deleted in a finally. The second
device's cables are inserted in the same transaction, so they tie with the first
device's at the same instant, and its own walk must not see the first device's.
"""

from __future__ import annotations

import uuid

import pytest
from app.models.connection import Connection
from app.services.connection_service import list_connections
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture(autouse=True)
def _use_cabling_schema():
    """Point the Connection table at the real "cabling" schema for this file only.

    services/cabling/conftest.py forces DB_SCHEMA="" for the SQLite unit suite, so
    the mapped Table carries schema=None; the live database's migrations created it
    under "cabling". Same fixture as the sibling live-PG files.
    """
    table = Connection.__table__
    original = table.schema
    table.schema = "cabling"
    try:
        yield
    finally:
        table.schema = original


@pytest.fixture
async def session_factory():
    engine = create_async_engine(PG_DSN)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# One device's cables, more than several pages of every size walked below, and a
# neighbor's cables in the same transaction.
OWN_COUNT = 60
NEIGHBOR_COUNT = 25
PAGE_SIZES = [1, 7, 13, 50]


def _names(*devices: uuid.UUID):
    return or_(Connection.device_a_id.in_(devices), Connection.device_b_id.in_(devices))


async def _foreign_snapshot(session_factory, own: tuple[uuid.UUID, ...]) -> dict:
    async with session_factory() as s:
        rows = await s.execute(
            select(
                Connection.id,
                Connection.device_a_id,
                Connection.port_a,
                Connection.device_b_id,
                Connection.port_b,
                Connection.created_at,
                Connection.updated_at,
            ).where(~_names(*own))
        )
        return {r.id: tuple(r[1:]) for r in rows}


@pytest.fixture
async def cabled(session_factory):
    """Yield (device, neighbor, own_ids, neighbor_ids) for cables inserted in ONE
    transaction, the server default stamping `created_at`.

    The neighbor's cables are interleaved with the device's, so the table's
    physical order is neither device's id order. Everything naming either random
    device is deleted in the finally, and every other row must be untouched.
    """
    device, neighbor = uuid.uuid4(), uuid.uuid4()
    own = (device, neighbor)
    before = await _foreign_snapshot(session_factory, own)
    own_ids: list[uuid.UUID] = []
    neighbor_ids: list[uuid.UUID] = []
    try:
        async with session_factory() as s:
            for i in range(max(OWN_COUNT, NEIGHBOR_COUNT)):
                if i < OWN_COUNT:
                    row = Connection(
                        device_a_id=device,
                        port_a=f"eth{i}",
                        device_b_id=uuid.uuid4(),
                        port_b="eth0",
                        created_by="live-pg-1589",
                    )
                    s.add(row)
                    await s.flush()
                    own_ids.append(row.id)
                if i < NEIGHBOR_COUNT:
                    row = Connection(
                        device_a_id=uuid.uuid4(),
                        port_a="eth0",
                        device_b_id=neighbor,
                        port_b=f"eth{i}",
                        created_by="live-pg-1589",
                    )
                    s.add(row)
                    await s.flush()
                    neighbor_ids.append(row.id)
            await s.commit()
        yield device, neighbor, own_ids, neighbor_ids
    finally:
        async with session_factory() as s:
            await s.execute(delete(Connection).where(_names(*own)))
            await s.commit()
        assert await _foreign_snapshot(session_factory, own) == before


async def _walk(session_factory, device: uuid.UUID, limit: int) -> tuple[list[uuid.UUID], int]:
    seen: list[uuid.UUID] = []
    totals: set[int] = set()
    skip = 0
    while True:
        async with session_factory() as db:
            assert db.get_bind().dialect.name == "postgresql"
            items, total = await list_connections(db, device_id=device, skip=skip, limit=limit)
        totals.add(total)
        seen += [c.id for c in items]
        skip += limit
        if skip >= total:
            break
    assert len(totals) == 1
    return seen, totals.pop()


async def test_one_transaction_really_ties_created_at(session_factory, cabled):
    """The precondition, measured: every row of the batch has one `created_at`."""
    device, neighbor, own_ids, neighbor_ids = cabled
    async with session_factory() as s:
        distinct = (
            await s.execute(
                select(func.count(func.distinct(Connection.created_at))).where(
                    _names(device, neighbor)
                )
            )
        ).scalar_one()
    assert distinct == 1
    assert len(own_ids) == OWN_COUNT and len(neighbor_ids) == NEIGHBOR_COUNT


@pytest.mark.parametrize("limit", PAGE_SIZES)
async def test_page_walk_over_the_tie_returns_every_cable_once_in_id_order(
    session_factory, cabled, limit
):
    device, neighbor, own_ids, neighbor_ids = cabled
    seen, total = await _walk(session_factory, device, limit)
    assert total == OWN_COUNT
    assert len(seen) == len(set(seen)) == OWN_COUNT
    assert set(seen) == set(own_ids)
    # Every row ties on created_at, so the id alone orders the walk.
    assert seen == sorted(own_ids)

    seen, total = await _walk(session_factory, neighbor, limit)
    assert total == NEIGHBOR_COUNT
    assert seen == sorted(neighbor_ids)
