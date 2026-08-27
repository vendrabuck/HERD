"""Unit tests for herd_common.database.make_database (issue #595 item 1).

Covers the factory contract: a working sessionmaker against an in-memory SQLite
URL, and a Base usable for create_all. Each call must return its own Base class
(fresh DeclarativeBase subclass), since every service owns its own metadata.
"""

from herd_common.database import make_database
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
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
