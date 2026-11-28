"""Cabling's offset-paginated lists end their order with the id (issue #1589).

`GET /connections` sorts by `created_at` descending, `GET /templates` by
`updated_at` descending, and `GET /internal/forks` by `created_at` ascending.
Rows written in one transaction share those values (Postgres `now()` is the
transaction's start; `create_connections_bulk` inserts up to 200 connections and
commits once), and offset pages over a tie have no defined order. The id,
ascending, is the last sort key, so a full skip/limit walk returns every row
exactly once. `tests/test_connection_list_order_live_pg.py` proves the
connections case on a real Postgres.

Each test makes the tie real (one explicit timestamp for a block of rows) with
one row on each side of it, and inserts the block in an order that is neither
ascending nor descending by id, so neither a forward nor a backward scan of
SQLite's table or an index yields the order under test: without the id key the
exact-order assertion fails.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from app.models.connection import Connection
from app.models.fork import ForkStatus_ACTIVE, ForkStatus_ARCHIVED, ReservationFork
from app.models.template import TopologyTemplate
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}
INTERNAL_TOKEN = "test-internal-token"

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

TIE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
INSERT_ORDER = [4, 1, 7, 3, 6, 2, 5]
LIMITS = [1, 2, 3, 4, 500]


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=(0x1589 << 64) | n)


def _rows() -> list[tuple[int, datetime]]:
    """(id number, timestamp) in insertion order: 100 is the newest row, 50 the oldest."""
    rows = [(100, TIE + timedelta(hours=1))]
    rows += [(n, TIE) for n in INSERT_ORDER]
    rows += [(50, TIE - timedelta(hours=1))]
    return rows


# Descending lists: the newest row, the tied block by id ascending, the oldest row.
NEWEST_FIRST = [100, 1, 2, 3, 4, 5, 6, 7, 50]
# Ascending lists: the oldest row, the tied block by id ascending, the newest row.
OLDEST_FIRST = [50, 1, 2, 3, 4, 5, 6, 7, 100]


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[require_admin] = lambda: ADMIN_PAYLOAD
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _walk(client, path: str, limit: int, total: int, key: str, **params) -> list[str]:
    seen: list[str] = []
    for skip in range(0, total, limit):
        resp = await client.get(
            path,
            params={"skip": skip, "limit": limit, **params},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == total
        seen += [row["id"] for row in body["items"]] if key == "items" else body[key]
    return seen


def _connection(n: int, created: datetime, device_a: uuid.UUID) -> Connection:
    return Connection(
        id=_id(n),
        device_a_id=device_a,
        # Descending against the id number (4-digit, so strings sort like numbers):
        # ordering by port_a instead of the id cannot pass.
        port_a=f"eth{5000 - n}",
        device_b_id=uuid.uuid4(),
        port_b="eth0",
        created_by="admin",
        created_at=created,
    )


@pytest.mark.parametrize("limit", LIMITS)
async def test_connections_list_breaks_created_at_ties_by_id(client, limit):
    device = uuid.uuid4()
    async with TestSessionLocal() as session:
        for n, created in _rows():
            session.add(_connection(n, created, device))
            await session.flush()
        await session.commit()
    seen = await _walk(client, "/connections", limit, len(NEWEST_FIRST), "items")
    assert seen == [str(_id(n)) for n in NEWEST_FIRST]


@pytest.mark.parametrize("limit", LIMITS)
async def test_connections_device_filter_breaks_created_at_ties_by_id(client, limit):
    """The `device_id` filter the frontend walks (`useDeviceConnections`) keeps the
    same total order, and another device's tied cables never reach its pages."""
    device = uuid.uuid4()
    async with TestSessionLocal() as session:
        for n, created in _rows():
            session.add(_connection(n, created, device))
            await session.flush()
        for n in INSERT_ORDER:
            session.add(_connection(1000 + n, TIE, uuid.uuid4()))
        await session.commit()
    seen = await _walk(
        client, "/connections", limit, len(NEWEST_FIRST), "items", device_id=str(device)
    )
    assert seen == [str(_id(n)) for n in NEWEST_FIRST]


@pytest.mark.parametrize("limit", LIMITS)
async def test_templates_list_breaks_updated_at_ties_by_id(client, limit):
    async with TestSessionLocal() as session:
        for n, stamp in _rows():
            session.add(
                TopologyTemplate(
                    id=_id(n),
                    name=f"tie-template-{n}",
                    created_by=uuid.uuid4(),
                    # created_at mirrors updated_at around TIE, so the two orders
                    # differ: sorting by created_at instead of updated_at fails.
                    created_at=TIE - (stamp - TIE),
                    updated_at=stamp,
                )
            )
            await session.flush()
        await session.commit()
    seen = await _walk(client, "/templates", limit, len(NEWEST_FIRST), "items")
    assert seen == [str(_id(n)) for n in NEWEST_FIRST]


@pytest.mark.parametrize("limit", LIMITS)
async def test_active_fork_listing_breaks_created_at_ties_by_id(client, limit):
    """The order comes from the ORDER BY, not from an access path.

    `ix_reservation_fork_active_created_at` is on `(created_at, id)`, so a scan of
    it already yields ties by id and an ORDER BY on `created_at` alone would pass
    here. Postgres may sort the heap instead of reading that index (a small table,
    or a plan that prefers a sequential scan), so the index guarantees nothing;
    the test drops it so SQLite must sort, and only the id key orders the tie.
    """

    def reservation(n: int) -> uuid.UUID:
        return uuid.UUID(int=(0x2589 << 64) | n)

    async with test_engine.begin() as conn:
        await conn.execute(text("DROP INDEX ix_reservation_fork_active_created_at"))
    async with TestSessionLocal() as session:
        for n, created in _rows():
            session.add(
                ReservationFork(
                    id=_id(n),
                    reservation_id=reservation(n),
                    status=ForkStatus_ACTIVE,
                    created_at=created,
                )
            )
            await session.flush()
        # An archived fork on the same instant is not listed.
        session.add(
            ReservationFork(
                id=_id(8), reservation_id=reservation(8), status=ForkStatus_ARCHIVED, created_at=TIE
            )
        )
        await session.commit()
    seen = await _walk(client, "/internal/forks", limit, len(OLDEST_FIRST), "reservation_ids")
    assert seen == [str(reservation(n)) for n in OLDEST_FIRST]
