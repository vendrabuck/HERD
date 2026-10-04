"""Postgres-live proof of the topologies list's text ordering and search (issue #958).

SQLite and Postgres disagree on text ordering in two ways the SQLite unit suite
(tests/test_topology_list_controls.py) cannot see:
- collation: Postgres orders text by the database's default collation, which varies
  per deployment (the gate stack's en_US.utf8 orders "B" before "a"), while SQLite
  uses byte order.
- NULL placement: Postgres treats NULL as the largest value, SQLite as the smallest,
  and ``owner_name`` is nullable.
The route sorts text by ``lower(coalesce(column, ''))`` with the "C" collation on
Postgres so both dialects agree (the #902 lesson). This suite runs the REAL
``list_topologies`` handler against a real Postgres and asserts the same orders the
SQLite twin asserts, plus the search behavior on Postgres.

Env contract identical to the sibling cabling live-PG files:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
cabling schema. Every row here is created by a random user id and named with a
per-run prefix, every list call passes ``owner="mine"`` as that user (so foreign rows
cannot appear in the result), no table-wide count is asserted, every other topology
row is snapshotted before and after and must be unchanged, and the rows are deleted
in a finally.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.topology import Topology
from app.routes.topologies import list_topologies
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# The rows and expected orders are shared with the SQLite twin
# (test_topology_list_controls.py::test_text_order_matches_the_live_pg_suite), so
# both dialects are held to one answer.
from tests.test_topology_list_controls import (
    NAME_ASC,
    NAME_DESC,
    NAME_ROWS,
    OWNER_ASC,
    OWNER_DESC,
    OWNER_ROWS,
)

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


_PG_REACHABLE = asyncio.run(_pg_reachable())

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
    """Point the Topology table at the real "cabling" schema for this file only.

    services/cabling/conftest.py forces DB_SCHEMA="" for the SQLite unit suite, so
    the mapped Table carries schema=None; the live database's migrations created it
    under "cabling". Same fixture as the sibling live-PG files.
    """
    table = Topology.__table__
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


async def _foreign_snapshot(session_factory, owner: uuid.UUID) -> dict:
    async with session_factory() as s:
        rows = await s.execute(
            select(Topology.id, Topology.name, Topology.owner_name, Topology.updated_at).where(
                Topology.created_by != owner
            )
        )
        return {r.id: (r.name, r.owner_name, r.updated_at) for r in rows}


@pytest.fixture
async def owned(session_factory):
    """Yield (owner, seed) where seed(rows) inserts rows owned by a random user.

    Rows are (order_key, name, owner_name). Ids share one random prefix and end in
    order_key, so the id tiebreak order is known. Everything owned by the random
    user is deleted in the finally, and foreign rows must be untouched.
    """
    owner = uuid.uuid4()
    prefix = uuid.uuid4().int & ~0xFFFF
    before = await _foreign_snapshot(session_factory, owner)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def seed(rows):
        ids = {}
        async with session_factory() as s:
            for key, name, owner_name in rows:
                tid = uuid.UUID(int=prefix | key)
                ids[name] = tid
                s.add(
                    Topology(
                        id=tid,
                        name=name,
                        created_by=owner,
                        owner_name=owner_name,
                        canvas_data=None,
                        created_at=t0 + timedelta(hours=key),
                        updated_at=t0 + timedelta(hours=key),
                    )
                )
            await s.commit()
        return ids

    try:
        yield owner, seed
    finally:
        async with session_factory() as s:
            await s.execute(delete(Topology).where(Topology.created_by == owner))
            await s.commit()
        assert await _foreign_snapshot(session_factory, owner) == before


async def _list(session_factory, owner, **kwargs):
    kwargs.setdefault("owner", "mine")
    async with session_factory() as db:
        dialect = db.get_bind().dialect.name
        assert dialect == "postgresql"
        page = await list_topologies(
            skip=0, limit=500, payload={"sub": str(owner), "role": "user"}, db=db, **kwargs
        )
    return page


async def test_name_order_is_case_folded_byte_order_on_postgres(session_factory, owned):
    owner, seed = owned
    await seed(NAME_ROWS)
    asc = await _list(session_factory, owner, sort_by="name", sort_dir="asc")
    assert [t.name for t in asc.items] == NAME_ASC
    desc = await _list(session_factory, owner, sort_by="name", sort_dir="desc")
    # Descending reverses the primary key only; the b/B tie keeps id ascending.
    assert [t.name for t in desc.items] == NAME_DESC
    assert asc.total == desc.total == len(NAME_ROWS)


async def test_owner_name_order_places_null_as_empty_on_postgres(session_factory, owned):
    owner, seed = owned
    await seed(OWNER_ROWS)
    asc = await _list(session_factory, owner, sort_by="owner_name", sort_dir="asc")
    assert [t.name for t in asc.items] == OWNER_ASC
    desc = await _list(session_factory, owner, sort_by="owner_name", sort_dir="desc")
    assert [t.name for t in desc.items] == OWNER_DESC


async def test_timestamp_order_and_total_on_postgres(session_factory, owned):
    owner, seed = owned
    await seed([(k, f"ts-{k}", "x") for k in (3, 1, 2)])
    page = await _list(session_factory, owner, sort_by="updated_at", sort_dir="desc")
    assert [t.name for t in page.items] == ["ts-3", "ts-2", "ts-1"]
    assert page.total == 3


async def test_search_is_case_insensitive_and_literal_on_postgres(session_factory, owned):
    owner, seed = owned
    await seed(
        [
            (1, "Core LAB", "x"),
            (2, "edge lab", "x"),
            (3, "100% uptime", "x"),
            (4, "1000 uptime", "x"),
            (5, "a_b", "x"),
            (6, "axb", "x"),
        ]
    )
    page = await _list(session_factory, owner, search="LaB", sort_by="name", sort_dir="asc")
    assert [t.name for t in page.items] == ["Core LAB", "edge lab"]
    assert page.total == 2
    page = await _list(session_factory, owner, search="0%", sort_by="name", sort_dir="asc")
    assert [t.name for t in page.items] == ["100% uptime"]
    page = await _list(session_factory, owner, search="a_b", sort_by="name", sort_dir="asc")
    assert [t.name for t in page.items] == ["a_b"]
