"""Postgres-live proof that the reservations list filters (issue #959) behave on
Postgres exactly as the SQLite unit tests in test_reservation_list_filters.py pin them.

Four filter shapes can differ between the dialects, so each runs here through the real
production list function against a real Postgres:
- search: icontains renders ILIKE on Postgres and lower() LIKE on SQLite, with an
  ESCAPE clause for % and _;
- the id prefix match: the id is a native uuid on Postgres (hyphenated text form) and a
  32-character hex string on SQLite, and the match strips hyphens on both;
- status: a native Postgres enum compared with IN;
- the time window: timestamptz compared by instant, including a bound given at a
  non-UTC offset.

Env contract identical to test_reservation_sort_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database. Every row here belongs to a random user_id, the production list path is called
scoped to that user (so foreign rows cannot appear), the purposes carry a per-run token,
no total count of the table is asserted, every other reservation row's purpose fields
are snapshotted before and after and must be unchanged, and the rows are deleted in a
finally. The rows sit around a fixed pivot instant decades ahead of now, so a live
sweeper never touches them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.services.reservation_service import (
    ReservationListFilters,
    list_all_reservations,
    list_user_reservations,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")

_CONNECT_ARGS = {"server_settings": {"search_path": "reservations"}}

# A fixed instant far from the present: the period views below are computed against it,
# not against the wall clock, so the sweeper's ACTIVE/PENDING handling never sees a row
# whose window is live.
PIVOT = datetime(2099, 6, 1, 12, 0, tzinfo=timezone.utc)


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)


async def _pg_reachable() -> bool:
    engine = _make_engine()
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
            select(Reservation.id, Reservation.purpose, Reservation.purpose_category).where(
                Reservation.user_id != user_id
            )
        )
        return {r.id: (r.purpose, r.purpose_category) for r in rows}


@pytest.fixture
async def rows(session_factory):
    """Seven rows owned by a random user; yields (user_id, token, ids by name, the
    foreign snapshot taken before insert). Deleted in the finally."""
    user_id = uuid.uuid4()
    token = uuid.uuid4().hex[:10]
    before = await _foreign_snapshot(session_factory, user_id)
    h = timedelta(hours=1)
    specs = {
        # name: (purpose, status, category, start, end, id)
        "mixed_case": (f"{token} Lab REGRESSION run", ReservationStatus.ACTIVE, "training",
                       PIVOT - h, PIVOT + h, None),
        "percent": (f"{token} load 100% now", ReservationStatus.PENDING, None,
                    PIVOT + h, PIVOT + 2 * h, None),
        "percent_decoy": (f"{token} load 1000 now", ReservationStatus.CANCELLED, None,
                          PIVOT + 2 * h, PIVOT + 3 * h, None),
        "underscore": (f"{token} a_b", ReservationStatus.COMPLETED, "qa_regression",
                       PIVOT - 3 * h, PIVOT - 2 * h, None),
        "underscore_decoy": (f"{token} axb", ReservationStatus.FAILED, "qa_regression",
                             PIVOT - 3 * h, PIVOT, None),
        "starts_at_pivot": (f"{token} edge", ReservationStatus.PENDING_PROVISION, None,
                            PIVOT, PIVOT + h, None),
        "by_id": (f"{token} id row", ReservationStatus.ACTIVE, None,
                  PIVOT - 2 * h, PIVOT - h, uuid.uuid4()),
    }  # fmt: skip
    ids: dict[str, uuid.UUID] = {}
    try:
        async with session_factory() as s:
            for name, (purpose, st, cat, start, end, rid) in specs.items():
                rid = rid or uuid.uuid4()
                ids[name] = rid
                s.add(
                    Reservation(
                        id=rid,
                        user_id=user_id,
                        device_ids=[],
                        topology_type=TopologyType.PHYSICAL,
                        purpose=purpose,
                        purpose_category=cat,
                        start_time=start,
                        end_time=end,
                        status=st,
                    )
                )
            await s.commit()
        yield user_id, token, ids, before
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM reservations WHERE user_id = :u"), {"u": user_id})
            await s.commit()
    # The suite only reads, so every foreign row that existed before is still there with
    # the same purpose fields. Status is left out on purpose: on a live stack the sweeper
    # legitimately moves foreign rows while this runs, and rows other sessions create
    # meanwhile are ignored.
    after = await _foreign_snapshot(session_factory, user_id)
    assert {k: after.get(k) for k in before} == before


async def _list(session_factory, user_id, **kw) -> tuple[set[uuid.UUID], int]:
    async with session_factory() as db:
        items, total = await list_user_reservations(
            db, user_id, skip=0, limit=50, filters=ReservationListFilters(**kw)
        )
    return {r.id for r in items}, total


async def test_search_is_case_insensitive_and_escapes_like_metacharacters(session_factory, rows):
    user_id, token, ids, _ = rows
    got, total = await _list(session_factory, user_id, search="lab regression")
    assert got == {ids["mixed_case"]} and total == 1
    got, _ = await _list(session_factory, user_id, search=f"{token.upper()} LAB")
    assert got == {ids["mixed_case"]}
    got, _ = await _list(session_factory, user_id, search="100%")
    assert got == {ids["percent"]}
    got, _ = await _list(session_factory, user_id, search=f"{token} a_b")
    assert got == {ids["underscore"]}


async def test_search_matches_id_prefix_on_native_uuid(session_factory, rows):
    user_id, _, ids, _ = rows
    rid = str(ids["by_id"])
    for term in (rid[:8], rid[:8].upper(), rid[:13], rid, rid.replace("-", "")):
        got, total = await _list(session_factory, user_id, search=term)
        assert got == {ids["by_id"]}, term
        assert total == 1


async def test_status_in_on_native_enum(session_factory, rows):
    user_id, _, ids, _ = rows
    got, total = await _list(
        session_factory,
        user_id,
        statuses=(ReservationStatus.PENDING, ReservationStatus.PENDING_PROVISION),
    )
    assert got == {ids["percent"], ids["starts_at_pivot"]}
    assert total == 2
    for name in ("percent_decoy", "underscore_decoy", "underscore"):
        st = {
            "percent_decoy": ReservationStatus.CANCELLED,
            "underscore_decoy": ReservationStatus.FAILED,
            "underscore": ReservationStatus.COMPLETED,
        }[name]
        got, _ = await _list(session_factory, user_id, statuses=(st,))
        assert got == {ids[name]}


async def test_purpose_category_value_and_none(session_factory, rows):
    user_id, _, ids, _ = rows
    got, _ = await _list(session_factory, user_id, purpose_category="qa_regression")
    assert got == {ids["underscore"], ids["underscore_decoy"]}
    got, _ = await _list(session_factory, user_id, purpose_category="none")
    assert got == {ids["percent"], ids["percent_decoy"], ids["starts_at_pivot"], ids["by_id"]}


async def test_period_views_partition_by_instant_on_timestamptz(session_factory, rows):
    user_id, _, ids, _ = rows
    every, _ = await _list(session_factory, user_id)
    assert every == set(ids.values())
    up, _ = await _list(session_factory, user_id, starts_after=PIVOT)
    cur, _ = await _list(session_factory, user_id, starts_before=PIVOT, ends_after=PIVOT)
    past, _ = await _list(session_factory, user_id, ends_before=PIVOT)
    assert up == {ids["percent"], ids["percent_decoy"], ids["starts_at_pivot"]}
    assert cur == {ids["mixed_case"], ids["underscore_decoy"]}
    assert past == {ids["underscore"], ids["by_id"]}
    assert up | cur | past == every
    assert not (up & cur) and not (up & past) and not (cur & past)


async def test_window_bound_at_non_utc_offset_compares_by_instant(session_factory, rows):
    """The route normalizes to UTC before the query; Postgres must agree with or
    without that step, so the raw offset bound is sent straight to the service here."""
    user_id, _, ids, _ = rows
    plus5 = timezone(timedelta(hours=5))
    later = (PIVOT + timedelta(minutes=1)).astimezone(plus5)
    earlier = (PIVOT - timedelta(minutes=1)).astimezone(plus5)
    got_later, _ = await _list(session_factory, user_id, starts_after=later)
    got_earlier, _ = await _list(session_factory, user_id, starts_after=earlier)
    assert ids["starts_at_pivot"] not in got_later
    assert ids["starts_at_pivot"] in got_earlier


async def test_filtered_total_and_paging_and_status_sort(session_factory, rows):
    user_id, token, ids, _ = rows
    filters = ReservationListFilters(search=token, purpose_category="none")
    seen: list[uuid.UUID] = []
    async with session_factory() as db:
        for skip in (0, 2):
            items, total = await list_user_reservations(
                db, user_id, skip=skip, limit=2, sort_by="status", sort_dir="asc", filters=filters
            )
            assert total == 4
            seen += [r.id for r in items]
    # Alphabetical by status name (issue #902): ACTIVE, CANCELLED, PENDING,
    # PENDING_PROVISION.
    assert seen == [ids["by_id"], ids["percent_decoy"], ids["percent"], ids["starts_at_pivot"]]


async def test_admin_all_view_filter_by_token_stays_on_own_rows(session_factory, rows):
    """list_all_reservations has no owner clause; a per-run token search keeps it to this
    suite's rows on a used database, and the filters still apply."""
    _, token, ids, _ = rows
    async with session_factory() as db:
        items, total = await list_all_reservations(
            db,
            skip=0,
            limit=50,
            filters=ReservationListFilters(search=token, statuses=(ReservationStatus.ACTIVE,)),
        )
    assert {r.id for r in items} == {ids["mixed_case"], ids["by_id"]}
    assert total == 2
