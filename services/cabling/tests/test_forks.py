"""Unit tests for the fork-on-activation models and POST /internal/forks (issue #25)."""

import uuid
from unittest.mock import patch

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
from app.services.fork_service import create_fork
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
        # created_by omitted from the body falls back to the "system" sentinel.
        assert c.created_by == "system"


@pytest.mark.asyncio
async def test_create_fork_threads_created_by(client):
    """The booking user passed as created_by is stamped onto every snapshotted
    fork_connection, instead of the "system" default (issue #134)."""
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    booking_user = str(uuid.uuid4())
    async with TestSessionLocal() as db:
        db.add(
            Connection(
                device_a_id=dev_a,
                port_a="eth0",
                device_b_id=dev_b,
                port_b="eth1",
                created_by="admin",
            )
        )
        await db.commit()

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
        json={
            "reservation_id": str(rid),
            "parent_topology_id": str(topo_id),
            "created_by": booking_user,
        },
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
        assert conns[0].created_by == booking_user


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


# --- IntegrityError-on-commit race (concurrent activation, DB-level idempotency) ---


@pytest.mark.asyncio
async def test_create_fork_returns_winner_on_commit_integrity_error():
    """Two concurrent activations both pass the pre-check and race the insert.

    The unique constraint on reservation_id serializes them: the loser's commit
    raises IntegrityError, and the handler must roll back the loser's partial rows
    (fork, fork_connections, fork_versions) and return the winner's fork, so the
    contract stays idempotent. Simulated deterministically by wrapping the loser's
    commit: the winner lands through an independent session only after the loser's
    pre-check has already run, then the commit raises IntegrityError.
    """
    # Parent topology with one committed edge over a real physical connection, so
    # the loser flushes fork_connections and fork_versions rows that must vanish.
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(
            Connection(
                device_a_id=dev_a,
                port_a="eth0",
                device_b_id=dev_b,
                port_b="eth1",
                created_by="admin",
            )
        )
        await db.commit()

    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(dev_a)}}},
            {"id": "n2", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    topo_id, _ = await _make_topology_with_version(canvas)

    rid = uuid.uuid4()
    state: dict = {"attempts": 0, "winner_id": None}

    async with TestSessionLocal() as db:

        async def racing_commit():
            state["attempts"] += 1
            # The DB aborts the loser's transaction on the constraint violation:
            # its flushed fork/connection/version rows never persist.
            await db.rollback()
            # The concurrent winner commits its fork through an independent
            # session, after the loser's pre-check already found nothing.
            async with TestSessionLocal() as other:
                winner = ReservationFork(reservation_id=rid)
                other.add(winner)
                await other.flush()
                other.add(ForkVersion(fork_id=winner.id, version_number=1))
                await other.commit()
                state["winner_id"] = winner.id
            raise IntegrityError("INSERT", {}, Exception("uq reservation_fork.reservation_id"))

        with patch.object(db, "commit", side_effect=racing_commit):
            fork = await create_fork(
                db,
                reservation_id=rid,
                parent_topology_id=topo_id,
                parent_version_id=None,
            )

        # The loser returned the winner's row, after exactly one commit attempt
        # (the handler re-queries; it does not retry the insert).
        assert state["attempts"] == 1
        assert fork.id == state["winner_id"]
        assert fork.reservation_id == rid

    async with TestSessionLocal() as db:
        forks = (
            (await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid)))
            .scalars()
            .all()
        )
        assert len(forks) == 1
        assert forks[0].id == state["winner_id"]
        # No partial loser rows: every surviving version belongs to the winner,
        # and the loser's snapshotted fork_connections were rolled back.
        versions = (await db.execute(select(ForkVersion))).scalars().all()
        assert [v.fork_id for v in versions] == [state["winner_id"]]
        conns = (await db.execute(select(ForkConnection))).scalars().all()
        assert conns == []


@pytest.mark.asyncio
async def test_create_fork_reraises_integrity_error_when_no_winner_exists():
    """An IntegrityError with no winning fork row re-raises after rollback.

    The commit can fail on a constraint other than the reservation_id unique (the
    fork_connections or fork_versions uniques share the transaction); then the
    re-query finds nothing and the handler must propagate the original error
    rather than return None or loop.
    """
    rid = uuid.uuid4()
    err = IntegrityError("INSERT", {}, Exception("uq_fork_versions_fork_version"))

    async with TestSessionLocal() as db:

        async def failing_commit():
            raise err

        with patch.object(db, "commit", side_effect=failing_commit):
            with pytest.raises(IntegrityError) as excinfo:
                await create_fork(
                    db,
                    reservation_id=rid,
                    parent_topology_id=None,
                    parent_version_id=None,
                )
        # The bare raise propagates the original exception instance.
        assert excinfo.value is err

    async with TestSessionLocal() as db:
        forks = (
            (await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid)))
            .scalars()
            .all()
        )
        assert forks == []
