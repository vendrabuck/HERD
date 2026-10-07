"""Unit tests for GET /connections/internal/by-port (issue #1023).

Inventory's port DELETE and port rename guard calls this to learn whether any
cabling `Connection` row still names a device port. Cabling stores ports by
name, so the lookup is by (device_id, port_name) on either end of a row.
"""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.connection import Connection
from app.routes.forks import CONNECTION_ID_SAMPLE_LIMIT
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

INTERNAL_TOKEN = "test-internal-token"

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

DEV = uuid.UUID("00000000-0000-0000-0000-00000000000a")
OTHER = uuid.UUID("00000000-0000-0000-0000-00000000000b")


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    yield


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _mk_conn(a, port_a, b, port_b) -> uuid.UUID:
    async with TestSessionLocal() as db:
        conn = Connection(
            device_a_id=a, port_a=port_a, device_b_id=b, port_b=port_b, created_by="admin"
        )
        db.add(conn)
        await db.commit()
        return conn.id


async def _by_port(client, device_id, port_name, *, headers: dict | None = None):
    return await client.get(
        "/connections/internal/by-port",
        params={"device_id": str(device_id), "port_name": port_name},
        headers={"X-Internal-Token": INTERNAL_TOKEN} if headers is None else headers,
    )


@pytest.mark.asyncio
async def test_wrong_token_is_403(client):
    resp = await _by_port(client, DEV, "eth0", headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Invalid internal token"}


@pytest.mark.asyncio
async def test_missing_token_is_422(client):
    resp = await client.get(
        "/connections/internal/by-port", params={"device_id": str(DEV), "port_name": "eth0"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_port_name_is_422(client):
    resp = await _by_port(client, DEV, "")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_uncabled_port_is_zero_and_empty(client):
    await _mk_conn(DEV, "eth1", OTHER, "eth0")
    resp = await _by_port(client, DEV, "eth0")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"connection_count": 0, "connection_ids": []}


@pytest.mark.asyncio
async def test_a_end_and_b_end_both_count(client):
    a_side = await _mk_conn(DEV, "ge-0/0/1", OTHER, "eth0")
    b_side = await _mk_conn(OTHER, "eth1", DEV, "ge-0/0/1")
    resp = await _by_port(client, DEV, "ge-0/0/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connection_count"] == 2
    assert body["connection_ids"] == sorted([str(a_side), str(b_side)])


@pytest.mark.asyncio
async def test_same_port_name_on_another_device_does_not_count(client):
    """The port is (device, name): OTHER's eth0 is not DEV's eth0, even when
    the row also names DEV on its other end."""
    await _mk_conn(OTHER, "eth0", DEV, "eth9")
    resp = await _by_port(client, DEV, "eth0")
    assert resp.json()["connection_count"] == 0


@pytest.mark.asyncio
async def test_port_name_match_is_exact(client):
    await _mk_conn(DEV, "eth0", OTHER, "eth0")
    for probe in ("ETH0", "eth", "eth00", " eth0"):
        resp = await _by_port(client, DEV, probe)
        assert resp.json()["connection_count"] == 0, probe


@pytest.mark.asyncio
async def test_loopback_naming_the_port_on_both_ends_counts_once(client):
    cid = await _mk_conn(DEV, "eth0", DEV, "eth0")
    resp = await _by_port(client, DEV, "eth0")
    assert resp.json() == {"connection_count": 1, "connection_ids": [str(cid)]}


@pytest.mark.asyncio
async def test_sample_is_capped_while_count_is_the_true_total(client):
    total = CONNECTION_ID_SAMPLE_LIMIT + 3
    ids = [await _mk_conn(DEV, "eth0", uuid.uuid4(), "p") for _ in range(total)]
    resp = await _by_port(client, DEV, "eth0")
    body = resp.json()
    assert body["connection_count"] == total
    assert body["connection_ids"] == sorted(str(i) for i in ids)[:CONNECTION_ID_SAMPLE_LIMIT]
