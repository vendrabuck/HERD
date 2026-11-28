"""The users and groups lists end their order with the id (issue #1589).

Both lists sort by `created_at` ascending. Rows created in one transaction share
that value (Postgres `now()` is the transaction's start), and offset pages over a
tie have no defined order, so a full skip/limit walk could repeat or drop a row.
The id is the last sort key, ascending, so the order is total.

Each test makes the tie real (one explicit `created_at` for a block of rows) and
inserts the block in an order that is neither ascending nor descending by id, so
neither a forward nor a backward scan of SQLite's table or an index yields the
order under test: without the id key the exact-order assertion fails.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.group import UserGroup
from app.models.user import Role, User

from tests._harness import TestSessionLocal, mock_user

TIE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
PREFIX = 0x1589 << 64


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=PREFIX | n)


# (id number, created_at): one row before the tie, seven tied rows, one after.
# Listed in insertion order.
ROWS = [(100, TIE - timedelta(hours=1))]
ROWS += [(n, TIE) for n in (4, 1, 7, 3, 6, 2, 5)]
ROWS += [(50, TIE + timedelta(hours=1))]
# created_at ascending, ties by id ascending.
EXPECTED = [_id(100)] + [_id(n) for n in range(1, 8)] + [_id(50)]


async def _walk(client, path: str, limit: int) -> list[str]:
    seen: list[str] = []
    skip = 0
    while True:
        resp = await client.get(path, params={"skip": skip, "limit": limit})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == len(EXPECTED)
        seen += [row["id"] for row in body["items"]]
        skip += limit
        if skip >= body["total"]:
            return seen


@pytest.fixture
async def admin_client(make_client):
    async with make_client(mock_user(Role.ADMIN, username="tiebreak-admin")) as ac:
        yield ac


async def _seed_users() -> None:
    async with TestSessionLocal() as session:
        for n, created in ROWS:
            session.add(
                User(
                    id=_id(n),
                    email=f"tie{n}@test.com",
                    username=f"tie{n}",
                    full_name=f"tie{n}",
                    hashed_password="fake",
                    role=Role.USER,
                    created_at=created,
                )
            )
            # One flush per row, so the rows reach the table in this order.
            await session.flush()
        await session.commit()


async def _seed_groups() -> None:
    async with TestSessionLocal() as session:
        for n, created in ROWS:
            session.add(UserGroup(id=_id(n), name=f"tie-group-{n}", created_at=created))
            await session.flush()
        await session.commit()


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 500])
async def test_users_list_breaks_created_at_ties_by_id(admin_client, limit):
    await _seed_users()
    seen = await _walk(admin_client, "/users", limit)
    assert seen == [str(i) for i in EXPECTED]
    assert len(set(seen)) == len(seen)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 500])
async def test_groups_list_breaks_created_at_ties_by_id(admin_client, limit):
    await _seed_groups()
    seen = await _walk(admin_client, "/groups", limit)
    assert seen == [str(i) for i in EXPECTED]
    assert len(set(seen)) == len(seen)
