"""Unit tests for the shared count-then-page helper (issue #597).

Exercises the helper against an in-memory SQLite table, mirroring the shape
of the six call sites it replaces: page past the end, boundary limit values,
skip=0, ordering (with a tiebreaker) preserved through the page, and the
count staying independent of offset/limit.
"""

import uuid

import pytest
from herd_common.pagination import paginate
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Widget(Base):
    """A minimal row shape. `rank` is deliberately non-unique so tests can
    pin the id tiebreaker the same way ldap_sync.py's two endpoints do.
    """

    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    rank: Mapped[int]
    name: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(session_factory, count: int, *, same_rank: bool = False):
    async with session_factory() as session:
        for i in range(count):
            session.add(Widget(rank=0 if same_rank else i, name=f"widget-{i}"))
        await session.commit()


# ---------------------------------------------------------------------------
# Core shape: items + total, offset/limit applied to the page only.
# ---------------------------------------------------------------------------


async def test_skip_zero_returns_first_page_and_true_total(session_factory):
    await _seed(session_factory, 10)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=0, limit=4)
    assert total == 10
    assert [w.rank for w in items] == [0, 1, 2, 3]


async def test_page_past_the_end_returns_empty_list_with_true_total(session_factory):
    await _seed(session_factory, 5)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=100, limit=10)
    assert items == []
    assert total == 5


async def test_limit_of_one_returns_a_single_row(session_factory):
    await _seed(session_factory, 5)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=2, limit=1)
    assert total == 5
    assert [w.rank for w in items] == [2]


async def test_limit_larger_than_total_returns_every_row(session_factory):
    await _seed(session_factory, 5)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=0, limit=10_000)
    assert total == 5
    assert [w.rank for w in items] == [0, 1, 2, 3, 4]


async def test_count_ignores_offset_and_limit(session_factory):
    await _seed(session_factory, 7)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank)
        _, total_page_one = await paginate(session, stmt, skip=0, limit=2)
        _, total_page_two = await paginate(session, stmt, skip=5, limit=2)
    assert total_page_one == 7
    assert total_page_two == 7


async def test_empty_table_returns_empty_list_and_zero_total(session_factory):
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=0, limit=50)
    assert items == []
    assert total == 0


# ---------------------------------------------------------------------------
# Ordering preserved through the page, including a tiebreaker (the
# ldap_sync.py list_mappings/list_sync_runs shape: equal timestamps would
# otherwise make skip/limit pages nondeterministic).
# ---------------------------------------------------------------------------


async def test_ordering_is_preserved_across_pages(session_factory):
    await _seed(session_factory, 6)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank.desc())
        page_one, total = await paginate(session, stmt, skip=0, limit=3)
        page_two, _ = await paginate(session, stmt, skip=3, limit=3)
    assert total == 6
    assert [w.rank for w in page_one] == [5, 4, 3]
    assert [w.rank for w in page_two] == [2, 1, 0]


async def test_tiebreaker_ordering_stays_deterministic_with_equal_rank(session_factory):
    """All rows share the same `rank` (mirrors equal created_at/started_at
    timestamps at the two ldap_sync.py call sites); an `id` tiebreaker in
    the caller's ORDER BY must still yield stable, non-overlapping pages.
    """
    await _seed(session_factory, 10, same_rank=True)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank, Widget.id)
        page_one, total = await paginate(session, stmt, skip=0, limit=4)
        page_two, _ = await paginate(session, stmt, skip=4, limit=4)
    assert total == 10
    assert [w.id for w in page_one] == sorted(w.id for w in page_one)
    assert [w.id for w in page_two] == sorted(w.id for w in page_two)
    # No overlap and no gap between consecutive pages.
    assert page_one[-1].id + 1 == page_two[0].id


async def test_tiebreaker_ordering_repeats_identically_on_repeated_calls(session_factory):
    """A deterministic tiebreaker means calling paginate twice for the same
    page returns rows in the identical order, not just the same set.
    """
    await _seed(session_factory, 8, same_rank=True)
    async with session_factory() as session:
        stmt = select(Widget).order_by(Widget.rank.desc(), Widget.id.desc())
        first_call, _ = await paginate(session, stmt, skip=1, limit=3)
        second_call, _ = await paginate(session, stmt, skip=1, limit=3)
    assert [w.id for w in first_call] == [w.id for w in second_call]


# ---------------------------------------------------------------------------
# Filters on the incoming statement are respected (mirrors acl's
# list_grants, which conditionally adds WHERE clauses before calling in).
# ---------------------------------------------------------------------------


async def test_respects_a_filtered_statement(session_factory):
    await _seed(session_factory, 10)
    async with session_factory() as session:
        stmt = select(Widget).where(Widget.rank >= 5).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=0, limit=50)
    assert total == 5
    assert [w.rank for w in items] == [5, 6, 7, 8, 9]


async def test_works_with_a_random_uuid_backed_string_column_filter(session_factory):
    """Sanity check that the helper is agnostic to what the caller filters
    on; not every call site filters on an integer column.
    """
    marker = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(Widget(rank=0, name=marker))
        session.add(Widget(rank=1, name="other"))
        await session.commit()
        stmt = select(Widget).where(Widget.name == marker).order_by(Widget.rank)
        items, total = await paginate(session, stmt, skip=0, limit=50)
    assert total == 1
    assert items[0].name == marker
