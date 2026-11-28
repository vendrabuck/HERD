"""Execution's offset-paginated lists end their order with the key (issue #1589).

`GET /runs` sorts by `created_at` descending, and runs written in one
transaction share it. `GET /device-health` sorts by `last_polled_at` descending
with never-polled rows last, and every never-polled row ties at NULL. Offset
pages over a tie have no defined order, so the primary key, ascending, is the
last sort key: `id` for runs, `device_id` for health snapshots (that table's
primary key is the device id; it has no `id` column).

Each test makes the tie real (one explicit timestamp, or NULL, for a block of
rows) and inserts the block in an order that is neither ascending nor descending
by key, so neither a forward nor a backward scan of SQLite's table or an index
yields the order under test: without the key the exact-order assertion fails.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.device_health_status import DeviceHealthStatus
from app.models.execution_run import ExecutionRun
from app.routers.executions import _authorize_runs_list
from app.routers.health import get_current_user_payload, require_admin
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

TIE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
INSERT_ORDER = [4, 1, 7, 3, 6, 2, 5]
LIMITS = [1, 2, 3, 4, 500]
ADMIN = {"sub": str(uuid.uuid4()), "role": "admin"}


def _key(n: int) -> uuid.UUID:
    return uuid.UUID(int=(0x1589 << 64) | n)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN
    app.dependency_overrides[require_admin] = lambda: ADMIN
    # An admin's runs list has no device filter (CFG-RUN-1).
    app.dependency_overrides[_authorize_runs_list] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _walk(client, path: str, limit: int, total: int, key: str) -> list[str]:
    seen: list[str] = []
    for skip in range(0, total, limit):
        resp = await client.get(path, params={"skip": skip, "limit": limit})
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == total
        seen += [row[key] for row in resp.json()["items"]]
    return seen


@pytest.mark.parametrize("limit", LIMITS)
async def test_runs_list_breaks_created_at_ties_by_id(client, limit):
    rows = [(100, TIE + timedelta(hours=1))]
    rows += [(n, TIE) for n in INSERT_ORDER]
    rows += [(50, TIE - timedelta(hours=1))]
    async with TestSessionLocal() as session:
        for n, created in rows:
            session.add(
                ExecutionRun(
                    id=_key(n),
                    device_id=uuid.uuid4(),
                    driver_id=uuid.uuid4(),
                    driver_sha256="abc",
                    action="login",
                    user_id=uuid.uuid4(),
                    input_params={},
                    created_at=created,
                )
            )
            await session.flush()
        await session.commit()
    expected = [str(_key(n)) for n in (100, 1, 2, 3, 4, 5, 6, 7, 50)]
    assert await _walk(client, "/runs", limit, len(expected), "id") == expected


@pytest.mark.parametrize("limit", LIMITS)
async def test_device_health_list_breaks_ties_by_device_id(client, limit):
    """Both kinds of tie: one poll instant shared by a block of devices, and the
    never-polled block, which ties at NULL and always sorts last."""
    rows = [(100, TIE + timedelta(hours=1))]
    rows += [(n, TIE) for n in INSERT_ORDER]
    rows += [(50, TIE - timedelta(hours=1))]
    rows += [(10 + n, None) for n in INSERT_ORDER]
    async with TestSessionLocal() as session:
        for n, polled in rows:
            session.add(DeviceHealthStatus(device_id=_key(n), last_polled_at=polled))
            await session.flush()
        await session.commit()
    order = [100, 1, 2, 3, 4, 5, 6, 7, 50] + [11, 12, 13, 14, 15, 16, 17]
    expected = [str(_key(n)) for n in order]
    assert await _walk(client, "/device-health", limit, len(expected), "device_id") == expected


async def test_device_health_order_by_reads_nulls_last_on_postgres(client):
    """`never-polled rows sort last` holds on Postgres only through `.nulls_last()`.

    SQLite already sorts NULL last under DESC and Postgres sorts it first, so the
    walk above passes on SQLite whatever the route says. This captures the list
    statement from a real request and compiles it for the PostgreSQL dialect, so
    the ORDER BY it reads is the one Postgres would run.
    """
    captured = []

    def capture(orm_execute_state):
        captured.append(orm_execute_state.statement)

    event.listen(Session, "do_orm_execute", capture)
    try:
        resp = await client.get("/device-health", params={"skip": 0, "limit": 5})
    finally:
        event.remove(Session, "do_orm_execute", capture)
    assert resp.status_code == 200, resp.text
    ordered = [
        str(stmt.compile(dialect=postgresql.dialect()))
        for stmt in captured
        if stmt._order_by_clause.clauses
    ]
    assert len(ordered) == 1, ordered
    order_by = ordered[0].split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
    normalized = " ".join(order_by.split())
    assert normalized == (
        "device_health_status.last_polled_at DESC NULLS LAST, device_health_status.device_id"
    )
