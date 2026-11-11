"""Unit tests for herd_common.database.make_database (issue #595 item 1).

Covers the factory contract: a working sessionmaker against an in-memory SQLite
URL, and a Base usable for create_all. Each call must return its own Base class
(fresh DeclarativeBase subclass), since every service owns its own metadata.
"""

import pytest
from herd_common.database import (
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_POOL_PRE_PING,
    DEFAULT_POOL_SIZE,
    engine_options,
    make_database,
)
from sqlalchemy import Column, Integer, String, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


def test_make_database_returns_four_tuple():
    engine, session_local, Base, get_db = make_database("sqlite+aiosqlite:///:memory:")
    assert isinstance(engine, AsyncEngine)
    assert isinstance(session_local, async_sessionmaker)
    assert isinstance(Base, type) and issubclass(Base, DeclarativeBase)
    assert callable(get_db)


async def test_sessionmaker_works_against_in_memory_sqlite():
    engine, session_local, Base, get_db = make_database("sqlite+aiosqlite:///:memory:")

    class Widget(Base):
        __tablename__ = "widgets"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_local() as session:
        session.add(Widget(id=1, name="gear"))
        await session.commit()

    async with session_local() as session:
        result = await session.execute(select(Widget).where(Widget.id == 1))
        row = result.scalar_one()
        assert row.name == "gear"

    await engine.dispose()


async def test_get_db_yields_a_working_session():
    engine, session_local, Base, get_db = make_database("sqlite+aiosqlite:///:memory:")

    class Thing(Base):
        __tablename__ = "things"
        id = Column(Integer, primary_key=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    gen = get_db()
    session = await gen.__anext__()
    try:
        assert isinstance(session, AsyncSession)
        session.add(Thing(id=1))
        await session.commit()
    finally:
        # Drain the async generator to run its cleanup (session close).
        async for _ in gen:
            pass

    await engine.dispose()


def test_each_call_returns_a_distinct_base():
    _, _, base_a, _ = make_database("sqlite+aiosqlite:///:memory:")
    _, _, base_b, _ = make_database("sqlite+aiosqlite:///:memory:")
    assert base_a is not base_b
    assert base_a.metadata is not base_b.metadata


# --- Connection pool (issue #1352) -------------------------------------------------

PG_URL = "postgresql+asyncpg://user:pw@postgres:5432/herd"


def test_postgres_engine_gets_the_pool_defaults_and_pre_ping():
    """No connection is opened: building an engine only configures its pool."""
    engine, _, _, _ = make_database(PG_URL)
    pool = engine.sync_engine.pool
    assert (DEFAULT_POOL_SIZE, DEFAULT_MAX_OVERFLOW, DEFAULT_POOL_PRE_PING) == (5, 10, True)
    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool._pre_ping is True


def test_postgres_engine_takes_the_pool_arguments_it_is_given():
    engine, _, _, _ = make_database(PG_URL, pool_size=3, max_overflow=0, pool_pre_ping=False)
    pool = engine.sync_engine.pool
    assert (pool.size(), pool._max_overflow, pool._pre_ping) == (3, 0, False)


def test_engine_options_per_dialect():
    assert engine_options(PG_URL, pool_size=2, max_overflow=7, pool_pre_ping=True) == {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": 2,
        "max_overflow": 7,
    }
    for url in ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:////tmp/x.db"):
        assert engine_options(url, pool_size=2, max_overflow=7, pool_pre_ping=True) == {
            "echo": False,
            "pool_pre_ping": True,
        }


def test_sqlite_size_arguments_are_dropped_because_its_pool_refuses_them():
    """The branch exists because SQLAlchemy refuses size arguments for this pool."""
    with pytest.raises(TypeError, match="pool_size"):
        create_async_engine("sqlite+aiosqlite:///:memory:", pool_size=5, max_overflow=5)
    engine, _, _, _ = make_database("sqlite+aiosqlite:///:memory:", pool_size=5, max_overflow=5)
    assert engine.sync_engine.pool._pre_ping is True


async def test_sqlite_engine_with_pre_ping_still_runs_queries():
    engine, session_local, _, _ = make_database("sqlite+aiosqlite:///:memory:")
    async with session_local() as session:
        assert (await session.execute(text("select 1"))).scalar_one() == 1
    await engine.dispose()
