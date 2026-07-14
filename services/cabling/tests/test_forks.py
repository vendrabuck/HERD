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
    ForkStatus_ARCHIVED,
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


@pytest.mark.asyncio
async def test_create_fork_returns_winner_on_flush_integrity_error():
    """A flush-time reservation_id collision is caught and returns the winner (issue #304).

    Postgres checks the reservation_id unique constraint at INSERT (flush), not at
    commit, so on the real concurrent-activation race the loser's IntegrityError
    surfaces at db.flush(), before commit is ever reached. The guarded region must
    cover the flush: the handler rolls back and returns the winner, keeping the
    contract idempotent even when the error never reaches the commit line. Regression
    for the pre-#304 code that wrapped only db.commit() and let this flush error
    escape unhandled.
    """
    rid = uuid.uuid4()
    state: dict = {"attempts": 0, "winner_id": None}

    async with TestSessionLocal() as db:

        async def racing_flush():
            state["attempts"] += 1
            # The DB aborts the loser's transaction on the constraint violation.
            await db.rollback()
            # The concurrent winner commits its fork through an independent session,
            # after the loser's pre-check already found nothing.
            async with TestSessionLocal() as other:
                winner = ReservationFork(reservation_id=rid)
                other.add(winner)
                await other.flush()
                other.add(ForkVersion(fork_id=winner.id, version_number=1))
                await other.commit()
                state["winner_id"] = winner.id
            raise IntegrityError("INSERT", {}, Exception("uq reservation_fork.reservation_id"))

        # No parent topology: create_fork issues no queries before its explicit
        # db.flush(), so the patched flush intercepts only that INSERT, and internal
        # autoflush (which runs on the underlying sync session) is untouched.
        with patch.object(db, "flush", side_effect=racing_flush):
            fork = await create_fork(
                db,
                reservation_id=rid,
                parent_topology_id=None,
                parent_version_id=None,
            )

        # One flush attempt, then the handler re-queries and returns the winner; it
        # does not retry the insert.
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
        # No partial loser rows survived the rollback.
        versions = (await db.execute(select(ForkVersion))).scalars().all()
        assert [v.fork_id for v in versions] == [state["winner_id"]]


# --- GET /internal/forks/{reservation_id} (issue #25 P3a read) ---


@pytest.mark.asyncio
async def test_get_fork_requires_internal_token(client):
    resp = await client.get(
        f"/internal/forks/{uuid.uuid4()}", headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_fork_404_when_absent(client):
    resp = await client.get(f"/internal/forks/{uuid.uuid4()}", headers=_hdr())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Fork not found"


@pytest.mark.asyncio
async def test_get_fork_returns_metadata_canvas_connections_versions(client):
    """GET returns the fork row, its canvas, its wiring, and its version list."""
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
    topo_id, version_id = await _make_topology_with_version(canvas)
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )

    resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reservation_id"] == str(rid)
    assert body["parent_topology_id"] == str(topo_id)
    assert body["parent_version_id"] == str(version_id)
    assert body["status"] == ForkStatus_ACTIVE
    assert body["canvas_data"] == canvas
    assert len(body["connections"]) == 1
    conn = body["connections"][0]
    assert conn["layer"] == "L1"
    assert {conn["device_a_id"], conn["device_b_id"]} == {str(dev_a), str(dev_b)}
    assert conn["physical_connection_id"] == str(physical_id)
    assert len(body["versions"]) == 1
    assert body["versions"][0]["version_number"] == 1
    # The version list is a summary: no canvas payload leaks through it.
    assert "canvas_data" not in body["versions"][0]


@pytest.mark.asyncio
async def test_get_fork_empty_fork_has_no_connections(client):
    """A fork with no parent topology returns a null canvas and no wiring."""
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert resp.status_code == 200
    body = resp.json()
    assert body["canvas_data"] is None
    assert body["parent_topology_id"] is None
    assert body["connections"] == []
    assert len(body["versions"]) == 1


# --- PUT /internal/forks/{reservation_id}/canvas (issue #25 P3a loose edit) ---


@pytest.mark.asyncio
async def test_update_fork_canvas_requires_internal_token(client):
    resp = await client.put(
        f"/internal/forks/{uuid.uuid4()}/canvas",
        json={"canvas_data": {"nodes": [], "edges": []}},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_fork_canvas_404_when_absent(client):
    resp = await client.put(
        f"/internal/forks/{uuid.uuid4()}/canvas",
        json={"canvas_data": {"nodes": [], "edges": []}},
        headers=_hdr(),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Fork not found"


@pytest.mark.asyncio
async def test_update_fork_canvas_stores_draft_without_reconcile_or_version(client):
    """A loose edit stores the canvas, validates it valid, but adds no wiring or version.

    The reachable-edge draft passes route validation (valid True), yet the loose PUT
    must NOT reconcile fork_connections (still zero) and must NOT append a fork_version
    (still just create's v1). That is the phase-1 contract: drafts are cheap.
    """
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

    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    draft = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(dev_a)}}},
            {"id": "n2", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    resp = await client.put(
        f"/internal/forks/{rid}/canvas", json={"canvas_data": draft}, headers=_hdr()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork.canvas_data == draft
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert conns == []  # loose edit does not reconcile wiring
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert len(versions) == 1  # loose edit appends no version
        assert versions[0].version_number == 1


@pytest.mark.asyncio
async def test_update_fork_canvas_reports_invalid_without_gating(client):
    """An unreachable edge is reported (valid False, no_path) but the draft still stores."""
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    draft = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(dev_a)}}},
            {"id": "n2", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    resp = await client.put(
        f"/internal/forks/{rid}/canvas", json={"canvas_data": draft}, headers=_hdr()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert len(body["invalid_edges"]) == 1
    assert body["invalid_edges"][0]["edge_id"] == "e1"
    assert body["invalid_edges"][0]["reason"] == "no_path"

    # Stored anyway: the loose edit does not gate on route validity.
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork.canvas_data == draft


@pytest.mark.asyncio
async def test_update_fork_canvas_refuses_archived(client):
    """A frozen (ARCHIVED) fork refuses the loose edit with 409 and stays unchanged."""
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        fork.status = ForkStatus_ARCHIVED
        await db.commit()

    draft = {"nodes": [{"id": "n1"}], "edges": []}
    resp = await client.put(
        f"/internal/forks/{rid}/canvas", json={"canvas_data": draft}, headers=_hdr()
    )
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork.canvas_data is None  # the draft was refused, not applied


# --- version_service generalization: commit_fork_with_new_version (issue #25 P3a) ---


async def _make_active_fork(reservation_id: uuid.UUID) -> uuid.UUID:
    """Persist a bare ACTIVE fork with a v1 snapshot; return its fork id."""
    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=reservation_id)
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        await db.commit()
        return fork.id


@pytest.mark.asyncio
async def test_commit_fork_with_new_version_allocates_next_number():
    """The fork helper numbers each save max+1 under uq_fork_versions_fork_version."""
    from app.services.version_service import commit_fork_with_new_version

    fork_id = await _make_active_fork(uuid.uuid4())

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        fork.canvas_data = {"nodes": [{"id": "n2"}], "edges": []}
        snapshot = ForkVersion(fork_id=fork.id, canvas_data=fork.canvas_data)
        await commit_fork_with_new_version(db, fork, snapshot)
        assert snapshot.version_number == 2

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        fork.canvas_data = {"nodes": [{"id": "n3"}], "edges": []}
        snapshot = ForkVersion(fork_id=fork.id, canvas_data=fork.canvas_data)
        await commit_fork_with_new_version(db, fork, snapshot)
        assert snapshot.version_number == 3

    async with TestSessionLocal() as db:
        numbers = sorted(
            v.version_number
            for v in (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
            .scalars()
            .all()
        )
        assert numbers == [1, 2, 3]


@pytest.mark.asyncio
async def test_commit_fork_with_new_version_retries_on_conflict():
    """A concurrent writer grabbing the next number forces a retry onto max+2, not a 500.

    Mirrors test_topology_versions' race pin against the fork version model: the same
    rollback-recompute-retry loop backs both, so the fork save recovers to a
    contiguous, duplicate-free sequence instead of surfacing a raw IntegrityError.
    """
    from app.services.version_service import commit_fork_with_new_version

    fork_id = await _make_active_fork(uuid.uuid4())

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        fork.canvas_data = {"nodes": [{"id": "n2"}], "edges": []}
        snapshot = ForkVersion(fork_id=fork.id, canvas_data=fork.canvas_data)

        real_commit = db.commit
        state = {"raced": False}

        async def racing_commit():
            if not state["raced"]:
                state["raced"] = True
                # A concurrent save grabs version 2 (the number this call allocated)
                # before our commit lands.
                async with TestSessionLocal() as other:
                    other.add(ForkVersion(fork_id=fork_id, version_number=2))
                    await other.commit()
            return await real_commit()

        with patch.object(db, "commit", side_effect=racing_commit):
            await commit_fork_with_new_version(db, fork, snapshot)

        # Retried onto 3 after the constraint rejected 2.
        assert snapshot.version_number == 3

    async with TestSessionLocal() as db:
        numbers = sorted(
            v.version_number
            for v in (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
            .scalars()
            .all()
        )
        assert numbers == [1, 2, 3]
        assert len(numbers) == len(set(numbers))


@pytest.mark.asyncio
async def test_commit_fork_with_new_version_exhausts_retries_and_raises():
    """Persistent contention past the cap re-raises IntegrityError rather than looping.

    The fork analogue of test_commit_with_new_version_exhausts_retries_and_raises: every
    commit is forced to conflict, so the bounded loop gives up after _MAX_ALLOCATE_RETRIES
    attempts and the error propagates.
    """
    import app.services.version_service as vs
    from app.services.version_service import commit_fork_with_new_version

    fork_id = await _make_active_fork(uuid.uuid4())

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        fork.canvas_data = {"nodes": [{"id": "n2"}], "edges": []}
        snapshot = ForkVersion(fork_id=fork.id, canvas_data=fork.canvas_data)

        attempts = {"n": 0}

        async def always_conflict():
            attempts["n"] += 1
            raise IntegrityError("INSERT", {}, Exception("uq conflict"))

        async def noop_rollback():
            return None

        with (
            patch.object(db, "commit", side_effect=always_conflict),
            patch.object(db, "rollback", side_effect=noop_rollback),
        ):
            with pytest.raises(IntegrityError):
                await commit_fork_with_new_version(db, fork, snapshot)

        assert attempts["n"] == vs._MAX_ALLOCATE_RETRIES


# --- Set-arithmetic pure functions (issue #25 P3a, ADR 0006 Decision 3) ---


def _spec(da, pa, db_dev, pb, layer="L1"):
    from app.services.fork_save_service import WireSpec

    return WireSpec(device_a_id=da, port_a=pa, device_b_id=db_dev, port_b=pb, layer=layer)


class _FakeRow:
    """A stand-in for a ForkConnection row for the pure set-arithmetic tests."""

    def __init__(self, da, pa, db_dev, pb, layer="L1"):
        self.device_a_id = da
        self.port_a = pa
        self.device_b_id = db_dev
        self.port_b = pb
        self.layer = layer
        self.physical_connection_id = None


def test_connection_identity_normalizes_orientation():
    """A wire and its reverse share one canonical identity; layer participates."""
    from app.services.fork_save_service import connection_identity

    a, b = uuid.uuid4(), uuid.uuid4()
    forward = connection_identity(a, "p0", b, "p1", "L1")
    reverse = connection_identity(b, "p1", a, "p0", "L1")
    assert forward == reverse
    # A different layer over the same ports is a different identity.
    assert connection_identity(a, "p0", b, "p1", "L2") != forward


def test_reconcile_sets_release_build_unchanged():
    """old MINUS new releases, new MINUS old builds, the intersection is untouched."""
    from app.services.fork_save_service import reconcile_connection_sets

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    kept = _FakeRow(a, "p0", b, "p1")  # in both
    gone = _FakeRow(a, "p2", c, "p3")  # old only
    old = [kept, gone]
    new = [
        _spec(b, "p1", a, "p0"),  # same identity as kept, reversed orientation
        _spec(a, "p4", c, "p5"),  # new only
    ]
    to_release, to_build, unchanged = reconcile_connection_sets(old, new)
    assert to_release == [gone]
    assert len(to_build) == 1
    assert (to_build[0].device_a_id, to_build[0].port_a) == (a, "p4")
    assert unchanged == 1


def test_reconcile_sets_move_wire_across_layers():
    """Moving a wire's layer over the same port pair is one release plus one build.

    The ADR's move-a-wire case: release and build touch the same physical port pair,
    differing only in layer, so it is never an in-place mutation.
    """
    from app.services.fork_save_service import reconcile_connection_sets

    a, b = uuid.uuid4(), uuid.uuid4()
    old = [_FakeRow(a, "p0", b, "p1", layer="L1")]
    new = [_spec(a, "p0", b, "p1", layer="L2")]
    to_release, to_build, unchanged = reconcile_connection_sets(old, new)
    assert len(to_release) == 1 and to_release[0].layer == "L1"
    assert len(to_build) == 1 and to_build[0].layer == "L2"
    assert unchanged == 0


# --- POST /internal/forks/{reservation_id}/save (issue #25 P3a reconcile) ---


async def _make_physical(da, pa, db_dev, pb) -> uuid.UUID:
    async with TestSessionLocal() as db:
        conn = Connection(
            device_a_id=da, port_a=pa, device_b_id=db_dev, port_b=pb, created_by="admin"
        )
        db.add(conn)
        await db.commit()
        return conn.id


def _canvas(nodes: list[uuid.UUID], edges: list[tuple[int, int]]) -> dict:
    return {
        "nodes": [{"id": f"n{i}", "data": {"device": {"id": str(d)}}} for i, d in enumerate(nodes)],
        "edges": [
            {"id": f"e{k}", "source": f"n{s}", "target": f"n{t}"} for k, (s, t) in enumerate(edges)
        ],
    }


def _endpoint_set(delta: dict) -> frozenset:
    return frozenset(
        {
            (delta["device_a_id"], delta["port_a"]),
            (delta["device_b_id"], delta["port_b"]),
        }
    )


async def _fork_connections(fork_id: uuid.UUID) -> list[ForkConnection]:
    async with TestSessionLocal() as db:
        return (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork_id)))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_save_fork_requires_internal_token(client):
    resp = await client.post(
        f"/internal/forks/{uuid.uuid4()}/save",
        json={"canvas_data": {"nodes": [], "edges": []}},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_save_fork_404_when_absent(client):
    resp = await client.post(
        f"/internal/forks/{uuid.uuid4()}/save",
        json={"canvas_data": {"nodes": [], "edges": []}},
        headers=_hdr(),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Fork not found"


@pytest.mark.asyncio
async def test_save_fork_refuses_archived(client):
    """An ARCHIVED fork refuses a save with 409, the same wording as the loose PUT."""
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        fork.status = ForkStatus_ARCHIVED
        await db.commit()

    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": {"nodes": [], "edges": []}},
        headers=_hdr(),
    )
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_save_fork_builds_new_wire(client):
    """Saving a canvas onto an empty fork builds its wiring and appends version 2."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": _canvas([a, b], [(0, 1)])},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_number"] == 2
    assert body["released"] == []
    assert body["unchanged_count"] == 0
    assert len(body["built"]) == 1
    assert _endpoint_set(body["built"][0]) == frozenset({(str(a), "a0"), (str(b), "b0")})

    conns = await _fork_connections(uuid.UUID(body["fork_id"]))
    assert len(conns) == 1


@pytest.mark.asyncio
async def test_save_fork_moves_wire(client):
    """A fork wired A-B, re-saved as A-C, releases A-B and builds A-C in one version."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    await _make_physical(a, "a1", c, "c0")
    # Parent topology wires A-B, so the created fork starts with that connection.
    parent_canvas = _canvas([a, b], [(0, 1)])
    topo_id, _ = await _make_topology_with_version(parent_canvas)
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )

    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": _canvas([a, c], [(0, 1)])},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_number"] == 2
    assert body["unchanged_count"] == 0
    assert len(body["released"]) == 1
    assert _endpoint_set(body["released"][0]) == frozenset({(str(a), "a0"), (str(b), "b0")})
    assert len(body["built"]) == 1
    assert _endpoint_set(body["built"][0]) == frozenset({(str(a), "a1"), (str(c), "c0")})

    conns = await _fork_connections(uuid.UUID(body["fork_id"]))
    assert len(conns) == 1
    assert {conns[0].device_a_id, conns[0].device_b_id} == {a, c}


@pytest.mark.asyncio
async def test_save_fork_unchanged_wire_is_not_rewritten(client):
    """Re-saving an identical canvas leaves the wire untouched but still versions."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    parent_canvas = _canvas([a, b], [(0, 1)])
    topo_id, _ = await _make_topology_with_version(parent_canvas)
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )

    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": parent_canvas},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["released"] == []
    assert body["built"] == []
    assert body["unchanged_count"] == 1
    assert body["version_number"] == 2


@pytest.mark.asyncio
async def test_save_fork_removes_all_wiring(client):
    """Saving an empty canvas releases every wire and builds nothing."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    parent_canvas = _canvas([a, b], [(0, 1)])
    topo_id, _ = await _make_topology_with_version(parent_canvas)
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "parent_topology_id": str(topo_id)},
        headers=_hdr(),
    )

    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": {"nodes": [], "edges": []}},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["released"]) == 1
    assert body["built"] == []
    assert body["unchanged_count"] == 0
    assert await _fork_connections(uuid.UUID(body["fork_id"])) == []


# --- Multi-hop physical path + shared-hop dedup at save (P3 audit gaps) ---


@pytest.mark.asyncio
async def test_save_fork_resolves_multi_hop_path(client):
    """A canvas edge across an off-canvas patch panel builds one wire per physical hop."""
    a, p, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", p, "p1")
    await _make_physical(p, "p2", b, "b0")
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    # Canvas edge A-B has no direct cable; it resolves over the two-hop path A-P-B.
    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": _canvas([a, b], [(0, 1)])},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    built = resp.json()["built"]
    assert len(built) == 2
    got = {_endpoint_set(d) for d in built}
    assert frozenset({(str(a), "a0"), (str(p), "p1")}) in got
    assert frozenset({(str(p), "p2"), (str(b), "b0")}) in got


@pytest.mark.asyncio
async def test_save_fork_dedups_shared_hop(client):
    """Two canvas edges sharing the A-P hop build that cable once, not twice."""
    a, p, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", p, "p1")  # shared first hop
    await _make_physical(p, "p2", b, "b0")
    await _make_physical(p, "p3", c, "c0")
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    # Edges A-B and A-C both route A-P-* and share the A-P cable.
    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": _canvas([a, b, c], [(0, 1), (0, 2)])},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    built = resp.json()["built"]
    assert len(built) == 3
    shared = frozenset({(str(a), "a0"), (str(p), "p1")})
    assert sum(1 for d in built if _endpoint_set(d) == shared) == 1


# --- Transactional rollback: no half-apply, no orphan version ---


@pytest.mark.asyncio
async def test_save_fork_rolls_back_between_release_and_build():
    """A failure after the release, before the build, leaves the fork byte-for-byte.

    The release-before-build delta and the version append share one transaction: an
    injected error mid-reconcile must persist nothing (no half-applied wiring, no
    orphan fork_versions row).
    """
    from unittest.mock import patch

    from app.services.fork_save_service import save_fork

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    await _make_physical(a, "a1", c, "c0")
    parent_canvas = _canvas([a, b], [(0, 1)])
    topo_id, _ = await _make_topology_with_version(parent_canvas)
    rid = uuid.uuid4()

    # Create the fork with its A-B wiring through an independent session.
    async with TestSessionLocal() as db:
        fork = await create_fork(
            db, reservation_id=rid, parent_topology_id=topo_id, parent_version_id=None
        )
        fork_id = fork.id

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        # The first db.add in a save is the first build insert; the releases are already
        # deleted-and-flushed by then, so raising here is squarely between the release
        # and the build.
        with patch.object(db, "add", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await save_fork(db, fork, canvas_data=_canvas([a, c], [(0, 1)]))
        await db.rollback()

    # The original A-B wiring survives and no version 2 was written.
    conns = await _fork_connections(fork_id)
    assert len(conns) == 1
    assert {conns[0].device_a_id, conns[0].device_b_id} == {a, b}
    async with TestSessionLocal() as db:
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
            .scalars()
            .all()
        )
        assert [v.version_number for v in versions] == [1]


# --- Cross-reservation port-claim enforcement (ADR 0006 Decision 4) ---


async def _make_active_fork_claiming(rid: uuid.UUID, da, pa, db_dev, pb, status_=None) -> uuid.UUID:
    """Persist an ACTIVE (or given-status) fork holding one fork_connection."""
    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=rid)
        if status_ is not None:
            fork.status = status_
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        db.add(
            ForkConnection(
                fork_id=fork.id,
                device_a_id=da,
                port_a=pa,
                device_b_id=db_dev,
                port_b=pb,
                layer="L1",
                created_by="system",
            )
        )
        await db.commit()
        return fork.id


@pytest.mark.asyncio
async def test_save_fork_409_on_cross_reservation_port_claim(client):
    """Building a wire on a port another ACTIVE fork holds fails 409, naming its reservation."""
    a, b, z = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    # Another ACTIVE reservation's fork already claims (a, a0).
    other_rid = uuid.uuid4()
    await _make_active_fork_claiming(other_rid, a, "a0", z, "z0")

    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": _canvas([a, b], [(0, 1)])},
        headers=_hdr(),
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == (
        "One or more ports are already claimed by another active reservation"
    )
    assert detail["conflicts"] == [
        {"reservation_id": str(other_rid), "device_id": str(a), "port": "a0"}
    ]
    # The save was refused wholesale: no wiring, still just version 1.
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert await _fork_connections(fork.id) == []
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert [v.version_number for v in versions] == [1]


@pytest.mark.asyncio
async def test_save_fork_archived_other_fork_does_not_block(client):
    """An ARCHIVED fork's wiring is history, not a claim, so it never blocks a save."""
    a, b, z = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    other_rid = uuid.uuid4()
    await _make_active_fork_claiming(other_rid, a, "a0", z, "z0", status_=ForkStatus_ARCHIVED)

    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())
    resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": _canvas([a, b], [(0, 1)])},
        headers=_hdr(),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["built"]) == 1


# --- Concurrent conflicting saves (ADR open risk 2, REQUIRED) ---


@pytest.mark.asyncio
async def test_save_fork_retries_on_version_conflict():
    """A competing save of the same fork grabs version 2; our save retries onto 3.

    Mirrors test_topology_versions' concurrent-writer pin against the fork save path:
    the version-allocation retry loop serializes the two saves onto a contiguous,
    duplicate-free sequence instead of a raw 500, and our wiring still lands.
    """
    from unittest.mock import patch

    from app.services.fork_save_service import save_fork

    a, b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        fork = await create_fork(
            db, reservation_id=rid, parent_topology_id=None, parent_version_id=None
        )
        fork_id = fork.id

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        real_commit = db.commit
        state = {"raced": False}

        async def racing_commit():
            if not state["raced"]:
                state["raced"] = True
                # Discard our in-flight save (mirrors the winner-race pattern used by
                # test_create_fork_returns_winner_on_commit_integrity_error), then let a
                # competing save of the same fork commit version 2 through an independent
                # session and force our version-allocation IntegrityError. The retry then
                # re-runs the reconcile and commits our wiring for real.
                await db.rollback()
                async with TestSessionLocal() as other:
                    other.add(ForkVersion(fork_id=fork_id, version_number=2))
                    await other.commit()
                raise IntegrityError("INSERT", {}, Exception("uq_fork_versions_fork_version"))
            return await real_commit()

        with patch.object(db, "commit", side_effect=racing_commit):
            result = await save_fork(db, fork, canvas_data=_canvas([a, b], [(0, 1)]))

        # Retried onto version 3 after the constraint rejected 2, and our wiring landed.
        assert result.version_number == 3
        assert len(result.built) == 1

    # Contiguous, duplicate-free sequence and the wiring persisted.
    async with TestSessionLocal() as db:
        numbers = sorted(
            v.version_number
            for v in (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
            .scalars()
            .all()
        )
        assert numbers == [1, 2, 3]
    conns = await _fork_connections(fork_id)
    assert len(conns) == 1


@pytest.mark.asyncio
async def test_save_fork_port_claim_query_reruns_on_retry():
    """The port-claim query re-runs inside the version-retry loop (ADR open risk 2).

    The conflict does not exist on the first pass; a competing writer both grabs the
    next version (forcing a retry) and plants a conflicting claim in another ACTIVE
    fork. The retry's re-executed port-claim query must catch it and fail the save 409,
    proving the check is inside the retry loop, not evaluated only once.
    """
    from unittest.mock import patch

    from app.services.fork_save_service import save_fork
    from fastapi import HTTPException

    a, b, z = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        fork = await create_fork(
            db, reservation_id=rid, parent_topology_id=None, parent_version_id=None
        )
        fork_id = fork.id

    competitor_rid = uuid.uuid4()

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        state = {"raced": False}

        async def racing_commit():
            if not state["raced"]:
                state["raced"] = True
                # Discard our in-flight save, then a competing writer takes version 2
                # (forcing our IntegrityError retry) AND plants a conflicting claim on
                # (a, a0) in a second ACTIVE fork. On the retry the reconcile re-runs and
                # its re-executed port-claim query must now catch that claim.
                await db.rollback()
                async with TestSessionLocal() as other:
                    other.add(ForkVersion(fork_id=fork_id, version_number=2))
                    competitor = ReservationFork(reservation_id=competitor_rid)
                    other.add(competitor)
                    await other.flush()
                    other.add(ForkVersion(fork_id=competitor.id, version_number=1))
                    other.add(
                        ForkConnection(
                            fork_id=competitor.id,
                            device_a_id=a,
                            port_a="a0",
                            device_b_id=z,
                            port_b="z0",
                            layer="L1",
                            created_by="system",
                        )
                    )
                    await other.commit()
                raise IntegrityError("INSERT", {}, Exception("uq_fork_versions_fork_version"))
            return None  # pragma: no cover - the retry raises 409 before committing

        with patch.object(db, "commit", side_effect=racing_commit):
            with pytest.raises(HTTPException) as excinfo:
                await save_fork(db, fork, canvas_data=_canvas([a, b], [(0, 1)]))
        await db.rollback()

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["conflicts"] == [
        {"reservation_id": str(competitor_rid), "device_id": str(a), "port": "a0"}
    ]
    # Our save lost: no wiring for our fork, and no version we authored.
    assert await _fork_connections(fork_id) == []


# --- POST /internal/forks/{reservation_id}/archive (ADR 0006 Decision 5) ---


@pytest.mark.asyncio
async def test_archive_fork_requires_internal_token(client):
    resp = await client.post(
        f"/internal/forks/{uuid.uuid4()}/archive", headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_archive_fork_absent_returns_204(client):
    """Archiving a nonexistent fork is a no-op 204 (nothing to freeze)."""
    resp = await client.post(f"/internal/forks/{uuid.uuid4()}/archive", headers=_hdr())
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_archive_fork_freezes_and_is_idempotent(client):
    """Archive flips status to ARCHIVED; a second call is a no-op 200, same state."""
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())

    first = await client.post(f"/internal/forks/{rid}/archive", headers=_hdr())
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == ForkStatus_ARCHIVED
    assert body["reservation_id"] == str(rid)

    second = await client.post(f"/internal/forks/{rid}/archive", headers=_hdr())
    assert second.status_code == 200
    assert second.json()["status"] == ForkStatus_ARCHIVED

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork.status == ForkStatus_ARCHIVED


@pytest.mark.asyncio
async def test_archive_fork_appends_no_version(client):
    """Archive retains versions read-only and appends no new one (Decision 5)."""
    rid = uuid.uuid4()
    await client.post("/internal/forks", json={"reservation_id": str(rid)}, headers=_hdr())
    await client.post(f"/internal/forks/{rid}/archive", headers=_hdr())

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork.id)))
            .scalars()
            .all()
        )
        assert [v.version_number for v in versions] == [1]
