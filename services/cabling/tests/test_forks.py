"""Unit tests for the fork-on-activation models and POST /internal/forks (issue #25)."""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.connection import Connection
from app.models.fork import (
    ForkConnection,
    ForkStatus_ACTIVE,
    ForkVersion,
    ReservationFork,
)
from app.models.topology import Topology, TopologyVersion
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


# --- Model-level tests (Phase 1 schema) ---


@pytest.mark.asyncio
async def test_fork_models_persist():
    """A fork plus its connections and versions round-trip through the models.

    The fork_id FKs declare ondelete=CASCADE, but SQLite does not enforce foreign
    keys without PRAGMA foreign_keys=ON, so the cascade itself is a Postgres-level
    guarantee verified by the migration/integration tests rather than here.
    """
    async with TestSessionLocal() as db:
        reservation_id = uuid.uuid4()
        fork = ReservationFork(
            reservation_id=reservation_id,
            parent_topology_id=uuid.uuid4(),
            parent_version_id=uuid.uuid4(),
            canvas_data={"nodes": [], "edges": []},
            status=ForkStatus_ACTIVE,
        )
        db.add(fork)
        await db.flush()
        db.add(
            ForkConnection(
                fork_id=fork.id,
                device_a_id=uuid.uuid4(),
                port_a="eth0",
                device_b_id=uuid.uuid4(),
                port_b="eth1",
                layer="L1",
                physical_connection_id=uuid.uuid4(),
                created_by="system",
            )
        )
        db.add(ForkVersion(fork_id=fork.id, version_number=1, canvas_data={"nodes": []}))
        await db.commit()

        reloaded = await db.get(ReservationFork, fork.id)
        assert reloaded.reservation_id == reservation_id
        assert reloaded.status == ForkStatus_ACTIVE
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert len(conns) == 1
        assert conns[0].physical_connection_id is not None
        assert len(versions) == 1
        assert versions[0].version_number == 1


@pytest.mark.asyncio
async def test_fork_connections_unique_endpoints():
    """The (fork, endpoints, layer) unique constraint rejects a duplicate wire."""
    from sqlalchemy.exc import IntegrityError

    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=uuid.uuid4())
        db.add(fork)
        await db.flush()
        da, dbid = uuid.uuid4(), uuid.uuid4()
        for _ in range(2):
            db.add(
                ForkConnection(
                    fork_id=fork.id,
                    device_a_id=da,
                    port_a="p0",
                    device_b_id=dbid,
                    port_b="p1",
                    layer="L1",
                    created_by="system",
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_fork_versions_unique_number():
    """fork_versions enforces unique (fork_id, version_number)."""
    from sqlalchemy.exc import IntegrityError

    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=uuid.uuid4())
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_reservation_fork_unique_reservation():
    """Only one fork per reservation_id (the idempotency invariant at the DB level)."""
    from sqlalchemy.exc import IntegrityError

    async with TestSessionLocal() as db:
        rid = uuid.uuid4()
        db.add(ReservationFork(reservation_id=rid))
        db.add(ReservationFork(reservation_id=rid))
        with pytest.raises(IntegrityError):
            await db.commit()


# --- Endpoint tests (Phase 2 fork-on-activation) ---


async def _make_topology_with_version(canvas: dict) -> tuple[uuid.UUID, uuid.UUID]:
    async with TestSessionLocal() as db:
        topo = Topology(name="parent", created_by=uuid.uuid4(), canvas_data=canvas)
        db.add(topo)
        await db.flush()
        version = TopologyVersion(
            topology_id=topo.id,
            version_number=1,
            canvas_data=canvas,
            name="parent",
            created_by=uuid.uuid4(),
        )
        db.add(version)
        await db.commit()
        return topo.id, version.id


@pytest.mark.asyncio
async def test_create_fork_requires_internal_token(client):
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(uuid.uuid4())},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_fork_deep_copies_canvas_and_pins_version(client):
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    topo_id, version_id = await _make_topology_with_version(canvas)
    rid = uuid.uuid4()

    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version_number"] == 1

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork.canvas_data == canvas
        # Pinned to the parent's current max TopologyVersion (Decision 3 Case B).
        assert fork.parent_version_id == version_id
        assert fork.status == ForkStatus_ACTIVE
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert len(versions) == 1
        assert versions[0].version_number == 1
        assert versions[0].canvas_data == canvas


@pytest.mark.asyncio
async def test_create_fork_is_idempotent(client):
    canvas = {"nodes": [], "edges": []}
    topo_id, _ = await _make_topology_with_version(canvas)
    rid = uuid.uuid4()

    first = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )
    second = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["fork_id"] == second.json()["fork_id"]

    async with TestSessionLocal() as db:
        forks = (
            (await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid)))
            .scalars()
            .all()
        )
        assert len(forks) == 1


@pytest.mark.asyncio
async def test_create_fork_no_topology_creates_empty_fork(client):
    """A fork with no parent topology (explicit POST) gets a null canvas and v1."""
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": None},
        headers=_hdr(),
    )
    assert resp.status_code == 201
    assert resp.json()["version_number"] == 1
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork.canvas_data is None
        assert fork.parent_version_id is None


@pytest.mark.asyncio
async def test_create_fork_snapshots_physical_path(client):
    """A canvas edge between two devices is snapshotted as L1 fork_connections
    along the shortest physical path, each carrying its backing connection id."""
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as db:
        physical = Connection(
            device_a_id=dev_a,
            port_a="eth0",
            device_b_id=dev_b,
            port_b="eth1",
            created_by="admin",
        )
        db.add(physical)
        await db.commit()
        physical_id = physical.id

    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(dev_a)}}},
            {"id": "n2", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    async with TestSessionLocal() as db:
        topo = Topology(name="t", created_by=uuid.uuid4(), canvas_data=canvas)
        db.add(topo)
        await db.flush()
        db.add(
            TopologyVersion(
                topology_id=topo.id,
                version_number=1,
                canvas_data=canvas,
                name="t",
                created_by=uuid.uuid4(),
            )
        )
        await db.commit()
        topo_id = topo.id

    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert len(conns) == 1
        c = conns[0]
        assert c.layer == "L1"
        assert {c.device_a_id, c.device_b_id} == {dev_a, dev_b}
        assert c.physical_connection_id == physical_id


@pytest.mark.asyncio
async def test_create_fork_skips_unreachable_edge(client):
    """An edge whose endpoints have no physical path leaves no fork_connection."""
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(dev_a)}}},
            {"id": "n2", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    async with TestSessionLocal() as db:
        topo = Topology(name="t", created_by=uuid.uuid4(), canvas_data=canvas)
        db.add(topo)
        await db.flush()
        db.add(
            TopologyVersion(
                topology_id=topo.id,
                version_number=1,
                canvas_data=canvas,
                name="t",
                created_by=uuid.uuid4(),
            )
        )
        await db.commit()
        topo_id = topo.id

    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )
    assert resp.status_code == 201
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert conns == []
