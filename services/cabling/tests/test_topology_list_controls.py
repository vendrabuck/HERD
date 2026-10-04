"""GET /topologies search, owner filter, and sort (issue #958).

Unit level: rows are inserted straight into the model with fixed timestamps,
owners, and ids, then read back through the real route over ASGI, so every
assertion is about the route's filtering, counting, and ordering. The Postgres
side of the text ordering (collation, NULL placement) is pinned separately by
tests/test_topology_list_order_live_pg.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models.topology import Topology
from app.routes.topologies import _topology_order_by, list_topologies
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ME = uuid.uuid4()
OTHER = uuid.uuid4()
ADMIN = uuid.uuid4()

ME_PAYLOAD = {"sub": str(ME), "username": "me", "role": "user"}
ADMIN_PAYLOAD = {"sub": str(ADMIN), "username": "boss", "role": "admin"}

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


def _client(payload: dict):
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    app.dependency_overrides[get_db] = _override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def me_client():
    async with _client(ME_PAYLOAD) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client():
    async with _client(ADMIN_PAYLOAD) as ac:
        yield ac
    app.dependency_overrides.clear()


def _uuid(n: int) -> uuid.UUID:
    """A deterministic id whose string order equals n's order."""
    return uuid.UUID(int=n)


async def _seed(rows: list[dict]) -> None:
    async with SessionLocal() as session:
        for row in rows:
            session.add(Topology(canvas_data=None, **row))
        await session.commit()


def _row(n: int, name: str, owner: uuid.UUID, owner_name: str | None, created: int, updated: int):
    return {
        "id": _uuid(n),
        "name": name,
        "created_by": owner,
        "owner_name": owner_name,
        "created_at": T0 + timedelta(hours=created),
        "updated_at": T0 + timedelta(hours=updated),
    }


# Five rows with distinct values on every sortable field, so each sort has
# exactly one right answer. Mixed case on purpose: a case-sensitive order would
# put "Bravo" and "Delta" ahead of "alpha" and "charlie".
STANDARD = [
    _row(1, "alpha lab", ME, "me", created=1, updated=40),
    _row(2, "Bravo LAB", OTHER, "Zed", created=2, updated=10),
    _row(3, "charlie", ME, "me", created=3, updated=30),
    _row(4, "Delta lab", OTHER, "amy", created=4, updated=20),
    _row(5, "echo", ADMIN, "boss", created=5, updated=50),
]


def _names(body: dict) -> list[str]:
    return [t["name"] for t in body["items"]]


async def _get(client: AsyncClient, **params) -> dict:
    resp = await client.get("/topologies", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# Defaults and each parameter alone


async def test_default_order_is_updated_at_desc(me_client):
    await _seed(STANDARD)
    body = await _get(me_client)
    assert _names(body) == ["echo", "alpha lab", "charlie", "Delta lab", "Bravo LAB"]
    assert body["total"] == 5


async def test_search_is_case_insensitive_substring_and_total_is_filtered(me_client):
    await _seed(STANDARD)
    for term in ("LAB", "lab", "Lab"):
        body = await _get(me_client, search=term)
        assert sorted(_names(body)) == ["Bravo LAB", "Delta lab", "alpha lab"], term
        assert body["total"] == 3, term


async def test_search_matches_inside_the_name(me_client):
    await _seed(STANDARD)
    body = await _get(me_client, search="harl")
    assert _names(body) == ["charlie"]
    assert body["total"] == 1


async def test_search_with_no_match_returns_empty_and_zero_total(me_client):
    await _seed(STANDARD)
    body = await _get(me_client, search="nothing-like-this")
    assert body["items"] == []
    assert body["total"] == 0


async def test_blank_search_is_no_filter(me_client):
    await _seed(STANDARD)
    for term in ("", "   "):
        body = await _get(me_client, search=term)
        assert body["total"] == 5, repr(term)


async def test_search_ignores_surrounding_whitespace(me_client):
    await _seed(STANDARD)
    body = await _get(me_client, search="  charlie ")
    assert _names(body) == ["charlie"]


async def test_search_treats_like_wildcards_literally(me_client):
    await _seed(
        [
            _row(1, "100% uptime", ME, "me", 1, 1),
            _row(2, "1000 uptime", ME, "me", 2, 2),
            _row(3, "a_b", ME, "me", 3, 3),
            _row(4, "axb", ME, "me", 4, 4),
        ]
    )
    assert _names(await _get(me_client, search="0%")) == ["100% uptime"]
    assert _names(await _get(me_client, search="a_b")) == ["a_b"]


async def test_owner_mine_returns_only_the_callers_topologies(me_client):
    await _seed(STANDARD)
    body = await _get(me_client, owner="mine")
    assert _names(body) == ["alpha lab", "charlie"]
    assert body["total"] == 2


async def test_owner_mine_for_an_admin_is_the_admins_own(admin_client):
    await _seed(STANDARD)
    body = await _get(admin_client, owner="mine")
    assert _names(body) == ["echo"]
    assert body["total"] == 1


async def test_owner_all_equals_the_default(me_client):
    await _seed(STANDARD)
    assert await _get(me_client, owner="all") == await _get(me_client)


async def test_owner_mine_with_nothing_owned_is_empty(me_client):
    await _seed([_row(1, "theirs", OTHER, "x", 1, 1)])
    body = await _get(me_client, owner="mine")
    assert body == {"items": [], "total": 0, "skip": 0, "limit": 50}


EXPECTED_ASC = {
    "name": ["alpha lab", "Bravo LAB", "charlie", "Delta lab", "echo"],
    # amy, boss, me, me, Zed: the two "me" rows tie and fall back to id order.
    "owner_name": ["Delta lab", "echo", "alpha lab", "charlie", "Bravo LAB"],
    "created_at": ["alpha lab", "Bravo LAB", "charlie", "Delta lab", "echo"],
    "updated_at": ["Bravo LAB", "Delta lab", "charlie", "alpha lab", "echo"],
}


@pytest.mark.parametrize("field", sorted(EXPECTED_ASC))
async def test_each_sort_field_ascending(me_client, field):
    await _seed(STANDARD)
    body = await _get(me_client, sort_by=field, sort_dir="asc")
    assert _names(body) == EXPECTED_ASC[field]
    assert body["total"] == 5


EXPECTED_DESC = {
    "name": ["echo", "Delta lab", "charlie", "Bravo LAB", "alpha lab"],
    # Zed, me, me, boss, amy: the tied "me" rows keep id ASCENDING even though
    # the primary direction is descending.
    "owner_name": ["Bravo LAB", "alpha lab", "charlie", "echo", "Delta lab"],
    "created_at": ["echo", "Delta lab", "charlie", "Bravo LAB", "alpha lab"],
    "updated_at": ["echo", "alpha lab", "charlie", "Delta lab", "Bravo LAB"],
}


@pytest.mark.parametrize("field", sorted(EXPECTED_DESC))
async def test_each_sort_field_descending(me_client, field):
    await _seed(STANDARD)
    body = await _get(me_client, sort_by=field, sort_dir="desc")
    assert _names(body) == EXPECTED_DESC[field]


async def test_sort_dir_alone_applies_to_the_default_field(me_client):
    await _seed(STANDARD)
    body = await _get(me_client, sort_dir="asc")
    assert _names(body) == EXPECTED_ASC["updated_at"]


@pytest.mark.parametrize("field", ["name", "owner_name", "created_at", "updated_at"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_full_tie_falls_back_to_id_ascending(me_client, field, direction):
    # Every sortable field equal: only the id tiebreak orders these rows, and it
    # is ascending in both directions.
    rows = [_row(n, "same", ME, "same", created=1, updated=1) for n in (3, 1, 2)]
    await _seed(rows)
    body = await _get(me_client, sort_by=field, sort_dir=direction)
    assert [t["id"] for t in body["items"]] == [str(_uuid(n)) for n in (1, 2, 3)]


async def test_tiebreak_keeps_pages_disjoint_and_complete(me_client):
    rows = [_row(n, f"tie-{n}", ME, "same", created=1, updated=1) for n in range(1, 8)]
    await _seed(rows)
    seen: list[str] = []
    for skip in range(0, 7, 2):
        body = await _get(me_client, sort_by="updated_at", skip=skip, limit=2)
        assert body["total"] == 7
        seen += [t["id"] for t in body["items"]]
    assert seen == [str(_uuid(n)) for n in range(1, 8)]


async def test_null_owner_name_sorts_as_empty(me_client):
    await _seed(
        [
            _row(1, "named", ME, "bob", 1, 1),
            _row(2, "null owner", ME, None, 2, 2),
            _row(3, "empty owner", ME, "", 3, 3),
        ]
    )
    asc = await _get(me_client, sort_by="owner_name", sort_dir="asc")
    assert _names(asc) == ["null owner", "empty owner", "named"]
    desc = await _get(me_client, sort_by="owner_name", sort_dir="desc")
    assert _names(desc) == ["named", "null owner", "empty owner"]


async def test_name_sort_folds_case_then_breaks_ties_by_id(me_client):
    await _seed(
        [
            _row(1, "b", ME, "me", 1, 1),
            _row(2, "B", ME, "me", 2, 2),
            _row(3, "a", ME, "me", 3, 3),
            _row(4, "A", ME, "me", 4, 4),
        ]
    )
    body = await _get(me_client, sort_by="name", sort_dir="asc")
    assert _names(body) == ["a", "A", "b", "B"]


# Shared with tests/test_topology_list_order_live_pg.py, which asserts the same
# orders on a real Postgres.
# Under the gate's default collation a raw ORDER BY name gives
# A, B, _z, a-c, ab, b: uppercase first, which is what the route must NOT do.
NAME_ROWS = [(1, "b", "x"), (2, "B", "x"), (3, "a-c", "x"), (4, "ab", "x"), (5, "A", "x")]
NAME_ROWS += [(6, "_z", "x")]
NAME_ASC = ["_z", "A", "a-c", "ab", "b", "B"]
NAME_DESC = ["b", "B", "ab", "a-c", "A", "_z"]


OWNER_ROWS = [(1, "t1", "carl"), (2, "t2", None), (3, "t3", "Bob"), (4, "t4", "amy")]
OWNER_ROWS += [(5, "t5", "")]
# NULL sorts as an empty owner: first ascending, last descending, on both dialects.
OWNER_ASC = ["t2", "t5", "t4", "t3", "t1"]
OWNER_DESC = ["t1", "t3", "t4", "t2", "t5"]


async def test_text_order_matches_the_live_pg_suite(me_client):
    # The SQLite twin of tests/test_topology_list_order_live_pg.py: the same rows
    # must produce the same orders on both dialects.
    await _seed([_row(k, name, ME, owner_name, k, k) for k, name, owner_name in NAME_ROWS])
    assert _names(await _get(me_client, sort_by="name", sort_dir="asc")) == NAME_ASC
    assert _names(await _get(me_client, sort_by="name", sort_dir="desc")) == NAME_DESC
    await _seed([_row(10 + k, name, OTHER, owner_name, k, k) for k, name, owner_name in OWNER_ROWS])
    mine_excluded = {"owner": "all", "search": "t"}
    asc = await _get(me_client, sort_by="owner_name", sort_dir="asc", **mine_excluded)
    assert _names(asc) == OWNER_ASC
    desc = await _get(me_client, sort_by="owner_name", sort_dir="desc", **mine_excluded)
    assert _names(desc) == OWNER_DESC


# Combined


async def test_search_owner_sort_and_page_combined(me_client):
    await _seed(STANDARD + [_row(6, "zulu LAB", ME, "me", 6, 60)])
    body = await _get(
        me_client, search="lab", owner="mine", sort_by="name", sort_dir="desc", skip=1, limit=1
    )
    # Mine and matching "lab": alpha lab, zulu LAB. Descending by name, page 2.
    assert _names(body) == ["alpha lab"]
    assert body["total"] == 2
    assert body["skip"] == 1
    assert body["limit"] == 1


async def test_total_is_filtered_not_the_page_size(me_client):
    await _seed([_row(n, f"lab-{n}", ME, "me", n, n) for n in range(10, 15)] + STANDARD[1:2])
    body = await _get(me_client, search="lab-", limit=2)
    assert len(body["items"]) == 2
    assert body["total"] == 5


async def test_search_combined_with_owner_all(admin_client):
    await _seed(STANDARD)
    body = await _get(admin_client, search="lab", owner="all", sort_by="owner_name", sort_dir="asc")
    assert _names(body) == ["Delta lab", "alpha lab", "Bravo LAB"]
    assert body["total"] == 3


# Validation


@pytest.mark.parametrize(
    "params",
    [
        {"sort_by": "id"},
        {"sort_by": "created_by"},
        {"sort_by": "NAME"},
        {"sort_by": ""},
        {"sort_dir": "up"},
        {"sort_dir": "ASC"},
        {"owner": "theirs"},
        {"owner": ""},
        {"search": "x" * 256},
        {"skip": -1},
        {"limit": 0},
        {"limit": 501},
    ],
)
async def test_bad_values_are_422(me_client, params):
    resp = await me_client.get("/topologies", params=params)
    assert resp.status_code == 422, (params, resp.text)


async def test_search_at_the_length_cap_is_accepted(me_client):
    resp = await me_client.get("/topologies", params={"search": "x" * 255})
    assert resp.status_code == 200


# Order clause and direct-call defaults


def test_order_by_uses_byte_order_collation_on_postgres_only():
    for field in ("name", "owner_name"):
        stmt = select(Topology.id).order_by(*_topology_order_by(field, "asc", "postgresql"))
        pg_sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert 'COLLATE "C"' in pg_sql, pg_sql
        assert "lower(coalesce(" in pg_sql, pg_sql
        stmt = select(Topology.id).order_by(*_topology_order_by(field, "asc", "sqlite"))
        sqlite_sql = str(stmt.compile(dialect=sqlite.dialect()))
        assert "COLLATE" not in sqlite_sql, sqlite_sql
        assert "lower(coalesce(" in sqlite_sql, sqlite_sql


def test_order_by_leaves_timestamp_fields_bare():
    for field in ("created_at", "updated_at"):
        stmt = select(Topology.id).order_by(*_topology_order_by(field, "desc", "postgresql"))
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "lower(" not in sql and "COLLATE" not in sql, sql
        assert sql.rstrip().endswith("DESC, cabling.topologies.id") or sql.rstrip().endswith(
            "DESC, topologies.id"
        ), sql


async def test_direct_call_with_only_skip_and_limit_keeps_defaults():
    # The parameters are Annotated with real defaults, so a direct handler call
    # that names only the old arguments gets the default filter and order
    # rather than FastAPI Query objects.
    await _seed(STANDARD)
    async with SessionLocal() as db:
        result = await list_topologies(skip=0, limit=50, payload=ME_PAYLOAD, db=db)
    assert [t.name for t in result.items] == [
        "echo",
        "alpha lab",
        "charlie",
        "Delta lab",
        "Bravo LAB",
    ]
    assert result.total == 5
