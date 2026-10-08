"""Unit tests for the rolled-back rehearsal session (issues #1017, #1064).

A dry run hands the real per-row create and update path a rehearsal session:
each commit there releases a savepoint, each rollback returns to it, later
work sees earlier commits, and nothing survives the context manager.
"""

import pytest
from herd_common.rehearsal import rehearsal_session
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Row(Base):
    __tablename__ = "rehearsal_rows"
    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)


@pytest.fixture
async def session_factory(tmp_path):
    # A file database, so a second connection can prove nothing was committed.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _names(factory) -> list[str]:
    async with factory() as other:
        return sorted((await other.execute(select(Row.name))).scalars().all())


async def test_commits_are_visible_inside_and_discarded_after(session_factory):
    async with session_factory() as db:
        db.add(Row(name="kept"))
        await db.commit()
        async with rehearsal_session(db) as rehearsal:
            rehearsal.add(Row(name="first"))
            await rehearsal.commit()
            # A later step sees the earlier step's committed write.
            seen = (await rehearsal.execute(select(Row.name))).scalars().all()
            assert sorted(seen) == ["first", "kept"]
            rehearsal.add(Row(name="second"))
            await rehearsal.commit()
        assert await _names(session_factory) == ["kept"]
        # The request session is still usable after the rehearsal.
        assert (await db.execute(select(Row.name))).scalars().all() == ["kept"]


async def test_rollback_returns_to_the_last_commit_only(session_factory):
    async with session_factory() as db:
        async with rehearsal_session(db) as rehearsal:
            rehearsal.add(Row(name="first"))
            await rehearsal.commit()
            rehearsal.add(Row(name="first"))  # a unique violation, as a bad row would raise
            with pytest.raises(IntegrityError):
                await rehearsal.commit()
            await rehearsal.rollback()
            seen = (await rehearsal.execute(select(Row.name))).scalars().all()
            assert seen == ["first"]
        assert await _names(session_factory) == []


async def test_an_exception_inside_still_rolls_everything_back(session_factory):
    async with session_factory() as db:
        with pytest.raises(RuntimeError, match="stop"):
            async with rehearsal_session(db) as rehearsal:
                rehearsal.add(Row(name="first"))
                await rehearsal.commit()
                raise RuntimeError("stop")
        assert await _names(session_factory) == []


async def test_yields_an_async_session(session_factory):
    async with session_factory() as db:
        async with rehearsal_session(db) as rehearsal:
            assert isinstance(rehearsal, AsyncSession)
            assert rehearsal is not db
