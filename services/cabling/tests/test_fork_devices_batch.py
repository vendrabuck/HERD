"""Unit tests for POST /internal/forks/devices/batch (issue #646 phase 3).

Reservations' utilization report calls this to fold transit gear (the switches
and routers on a reservation's resolved paths, per ADR 0013 phase 3) into its
device-level breakdowns. See docs/design/0013-lab-purpose-classification.md.
"""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkStatus_ARCHIVED, ReservationFork
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

INTERNAL_TOKEN = "test-internal-token"

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


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


def _hdr() -> dict:
    return {"X-Internal-Token": INTERNAL_TOKEN}


async def _mk_fork(rows: list[dict], *, status: str = ForkStatus_ACTIVE) -> uuid.UUID:
    """Create a fork with the given fork_connections rows; returns its reservation_id."""
    reservation_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=reservation_id, canvas_data={}, status=status)
        db.add(fork)
        await db.flush()
        for row in rows:
            db.add(
                ForkConnection(
                    fork_id=fork.id,
                    device_a_id=row["device_a_id"],
                    port_a=row.get("port_a", "eth0"),
                    device_b_id=row["device_b_id"],
                    port_b=row.get("port_b", "eth0"),
                    layer=row.get("layer", "L1"),
                    created_by="system",
                )
            )
        await db.commit()
    return reservation_id


async def _batch(client, reservation_ids: list[uuid.UUID], *, headers: dict | None = None):
    return await client.post(
        "/internal/forks/devices/batch",
        json={"reservation_ids": [str(rid) for rid in reservation_ids]},
        headers=_hdr() if headers is None else headers,
    )


# --- Guards --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_requires_internal_token(client):
    resp = await _batch(client, [uuid.uuid4()], headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_batch_missing_token(client):
    resp = await client.post(
        "/internal/forks/devices/batch", json={"reservation_ids": [str(uuid.uuid4())]}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_empty_list_is_422(client):
    resp = await client.post(
        "/internal/forks/devices/batch", json={"reservation_ids": []}, headers=_hdr()
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_over_cap_is_422(client):
    ids = [str(uuid.uuid4()) for _ in range(501)]
    resp = await client.post(
        "/internal/forks/devices/batch", json={"reservation_ids": ids}, headers=_hdr()
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_exactly_at_cap_is_accepted(client):
    ids = [str(uuid.uuid4()) for _ in range(500)]
    resp = await client.post(
        "/internal/forks/devices/batch", json={"reservation_ids": ids}, headers=_hdr()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["devices"] == {}


# --- The batch itself ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_returns_sorted_distinct_ids_active_and_archived(client):
    """Two forks, one ACTIVE one ARCHIVED, each holding connections through a
    shared switch: each reservation gets its own sorted distinct device id list."""
    switch = uuid.uuid4()
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    b1, b2 = uuid.uuid4(), uuid.uuid4()

    rid_active = await _mk_fork(
        [
            {"device_a_id": a1, "device_b_id": switch},
            {"device_a_id": switch, "device_b_id": a2},
        ],
        status=ForkStatus_ACTIVE,
    )
    rid_archived = await _mk_fork(
        [
            {"device_a_id": b1, "device_b_id": switch},
            {"device_a_id": switch, "device_b_id": b2},
        ],
        status=ForkStatus_ARCHIVED,
    )

    resp = await _batch(client, [rid_active, rid_archived])
    assert resp.status_code == 200, resp.text
    body = resp.json()["devices"]

    expected_active = sorted([str(a1), str(switch), str(a2)])
    expected_archived = sorted([str(b1), str(switch), str(b2)])
    assert body[str(rid_active)] == expected_active
    assert body[str(rid_archived)] == expected_archived


@pytest.mark.asyncio
async def test_batch_id_with_no_fork_is_absent(client):
    rid_with_fork = await _mk_fork([{"device_a_id": uuid.uuid4(), "device_b_id": uuid.uuid4()}])
    rid_without_fork = uuid.uuid4()

    resp = await _batch(client, [rid_with_fork, rid_without_fork])
    assert resp.status_code == 200, resp.text
    body = resp.json()["devices"]
    assert str(rid_with_fork) in body
    assert str(rid_without_fork) not in body


@pytest.mark.asyncio
async def test_batch_dedupes_a_device_touched_by_many_hops(client):
    switch = uuid.uuid4()
    d1, d2, d3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rid = await _mk_fork(
        [
            {"device_a_id": d1, "device_b_id": switch},
            {"device_a_id": switch, "device_b_id": d2},
            {"device_a_id": switch, "device_b_id": d3},
        ]
    )

    resp = await _batch(client, [rid])
    assert resp.status_code == 200, resp.text
    body = resp.json()["devices"]
    assert body[str(rid)] == sorted([str(d1), str(switch), str(d2), str(d3)])
