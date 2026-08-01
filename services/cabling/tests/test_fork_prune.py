"""POST /internal/forks/{reservation_id}/prune-devices (issues #459 and #462).

The device-set PATCH's REMOVE half, redesigned to release from the fork's saved
INTENDED set: the release is computed from fork_connections plus the last SAVED
canvas's edge incidence, never the draft canvas, so an unsaved draft edit can
neither be built nor released by a device removal. The draft is only scrubbed of the
removed devices (every other draft edit survives), and the appended fork_versions
row snapshots the pruned SAVED canvas, never the draft. A pure release builds
nothing, so no cross-reservation port claim can 409 it (issue #462's deterministic
trigger); the only 409 is an ARCHIVED fork.
"""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.fork import (
    ForkConnection,
    ForkStatus_ACTIVE,
    ForkStatus_ARCHIVED,
    ForkVersion,
    ReservationFork,
)
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


def _canvas(nodes: dict[str, str], edges: list[tuple[str, str, str]]) -> dict:
    """nodes: node_id to device_id; edges: (edge_id, source_node_id, target_node_id)."""
    return {
        "nodes": [
            {"id": node_id, "data": {"device": {"id": device_id}}}
            for node_id, device_id in nodes.items()
        ],
        "edges": [
            {"id": edge_id, "source": src, "target": dst, "data": {"layer": "L1"}}
            for edge_id, src, dst in edges
        ],
    }


async def _mk_fork(
    saved_canvas: dict,
    rows: list[dict],
    *,
    draft_canvas: dict | None = None,
    status: str = ForkStatus_ACTIVE,
) -> uuid.UUID:
    """Create a fork whose v1 snapshot is saved_canvas and whose draft may diverge."""
    reservation_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        fork = ReservationFork(
            reservation_id=reservation_id,
            canvas_data=draft_canvas if draft_canvas is not None else saved_canvas,
            status=status,
        )
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1, canvas_data=saved_canvas))
        for row in rows:
            db.add(
                ForkConnection(
                    fork_id=fork.id,
                    device_a_id=uuid.UUID(row["device_a_id"]),
                    port_a=row["port_a"],
                    device_b_id=uuid.UUID(row["device_b_id"]),
                    port_b=row["port_b"],
                    layer=row.get("layer", "L1"),
                    physical_connection_id=row.get("physical_connection_id"),
                    edge_key=row.get("edge_key"),
                    created_by="system",
                )
            )
        await db.commit()
    return reservation_id


async def _fork_state(reservation_id: uuid.UUID):
    """Return (fork, connections, versions ordered by number)."""
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(
                select(ReservationFork).where(ReservationFork.reservation_id == reservation_id)
            )
        ).scalar_one()
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
        versions = (
            (
                await db.execute(
                    select(ForkVersion)
                    .where(ForkVersion.fork_id == fork.id)
                    .order_by(ForkVersion.version_number)
                )
            )
            .scalars()
            .all()
        )
    return fork, conns, versions


def _row(a_id: str, port_a: str, b_id: str, port_b: str, edge_key: str | None) -> dict:
    return {
        "device_a_id": a_id,
        "port_a": port_a,
        "device_b_id": b_id,
        "port_b": port_b,
        "edge_key": edge_key,
    }


async def _prune(client, reservation_id: uuid.UUID, device_ids: list[str]):
    return await client.post(
        f"/internal/forks/{reservation_id}/prune-devices",
        json={"device_ids": device_ids},
        headers=_hdr(),
    )


# --- Guards --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_requires_internal_token(client):
    resp = await client.post(
        f"/internal/forks/{uuid.uuid4()}/prune-devices",
        json={"device_ids": [str(uuid.uuid4())]},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_prune_404_when_absent(client):
    resp = await _prune(client, uuid.uuid4(), [str(uuid.uuid4())])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_prune_refuses_archived(client):
    dut = str(uuid.uuid4())
    rid = await _mk_fork(_canvas({}, []), [], status=ForkStatus_ARCHIVED)
    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 409


# --- The release itself --------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_releases_removed_device_rows_and_bumps_version(client):
    """The removed device's saved wiring releases: its rows delete (freeing their
    port claims), a fork_versions row is appended snapshotting the pruned SAVED
    canvas, and the released delta comes back with every per-wire field."""
    dut, other = str(uuid.uuid4()), str(uuid.uuid4())
    phys_id = uuid.uuid4()
    saved = _canvas({"nD": dut, "nO": other}, [("e1", "nD", "nO")])
    row = _row(dut, "eth0", other, "p1", "e1")
    row["physical_connection_id"] = phys_id
    rid = await _mk_fork(saved, [row])

    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert body["version_number"] == 2
    assert len(body["released"]) == 1
    wire = body["released"][0]
    assert wire["device_a_id"] == dut
    assert wire["port_a"] == "eth0"
    assert wire["device_b_id"] == other
    assert wire["port_b"] == "p1"
    assert wire["layer"] == "L1"
    assert wire["edge_key"] == "e1"
    assert wire["physical_connection_id"] == str(phys_id)

    fork, conns, versions = await _fork_state(rid)
    assert conns == [], "the released row must delete (its port claim frees with it)"
    assert [v.version_number for v in versions] == [1, 2]
    snapshot = versions[-1].canvas_data
    assert [n["id"] for n in snapshot["nodes"]] == ["nO"]
    assert snapshot["edges"] == []
    # The draft was the saved canvas here; it is scrubbed the same way.
    assert [n["id"] for n in fork.canvas_data["nodes"]] == ["nO"]
    assert fork.canvas_data["edges"] == []


@pytest.mark.asyncio
async def test_prune_ignores_draft_and_preserves_unsaved_edits(client):
    """The issue #459 regression pin. The draft canvas diverges from the last save in
    both directions: it ADDS a never-saved edge (eNew between remaining devices) and
    DELETES a saved edge (eKeep). The prune must treat neither as intent: eNew is not
    built and not released and survives in the draft; eKeep's saved wiring stays; no
    fork version ever snapshots the draft."""
    a, b, dut = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas(
        {"nA": a, "nB": b, "nD": dut},
        [("eD", "nA", "nD"), ("eKeep", "nA", "nB")],
    )
    # Draft: the user drew eNew (A-B, unsaved) and deleted eKeep (unsaved).
    draft = _canvas(
        {"nA": a, "nB": b, "nD": dut},
        [("eD", "nA", "nD"), ("eNew", "nA", "nB")],
    )
    rows = [
        _row(a, "eth0", dut, "eth0", "eD"),
        _row(a, "eth1", b, "eth1", "eKeep"),
    ]
    rid = await _mk_fork(saved, rows, draft_canvas=draft)

    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    # Only the removed device's saved wiring releases; the draft-deleted eKeep stays.
    assert [w["edge_key"] for w in body["released"]] == ["eD"]

    fork, conns, versions = await _fork_state(rid)
    assert [c.edge_key for c in conns] == ["eKeep"], (
        "an unsaved draft deletion must not release saved wiring"
    )
    assert {c.edge_key for c in conns} == {"eKeep"}
    assert not any(c.edge_key == "eNew" for c in conns), (
        "an unsaved draft edge must not be built by a device removal"
    )
    # The draft survives scrubbed of the removed device but keeps the user's edits.
    draft_edges = {e["id"] for e in fork.canvas_data["edges"]}
    assert draft_edges == {"eNew"}
    assert {n["id"] for n in fork.canvas_data["nodes"]} == {"nA", "nB"}
    # No version snapshots the draft: the new snapshot is the pruned SAVED canvas.
    snapshot = versions[-1].canvas_data
    assert {e["id"] for e in snapshot["edges"]} == {"eKeep"}
    assert all("eNew" not in {e["id"] for e in v.canvas_data.get("edges", [])} for v in versions), (
        "no fork version may snapshot the unsaved draft"
    )


@pytest.mark.asyncio
async def test_prune_keeps_through_hop_serving_remaining_edge(client):
    """A row touching the removed device whose edge_key is a REMAINING saved edge is
    a through-hop (the removed device sits mid-path between devices still held): it
    stays, no version is bumped, and only the node scrub touches the draft."""
    a, b, switch = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas({"nA": a, "nB": b, "nS": switch}, [("e1", "nA", "nB")])
    rows = [
        _row(a, "eth0", switch, "p1", "e1"),
        _row(switch, "p2", b, "eth0", "e1"),
    ]
    rid = await _mk_fork(saved, rows)

    resp = await _prune(client, rid, [switch])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["version_number"] == 1
    assert body["released"] == []

    fork, conns, versions = await _fork_state(rid)
    assert len(conns) == 2, "through-hops serving a remaining edge must survive"
    assert [v.version_number for v in versions] == [1], "a bare node scrub earns no version"
    assert {n["id"] for n in fork.canvas_data["nodes"]} == {"nA", "nB"}


@pytest.mark.asyncio
async def test_prune_releases_far_hops_of_pruned_edges(client):
    """An edge whose ENDPOINT device is removed releases every hop of its path,
    including far hops that do not touch the removed device (the multi-hop remote
    cable through an off-canvas patch panel)."""
    a, dut, panel = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas({"nA": a, "nD": dut}, [("e2", "nD", "nA")])
    rows = [
        _row(dut, "eth0", panel, "in1", "e2"),
        _row(panel, "out1", a, "eth0", "e2"),
    ]
    rid = await _mk_fork(saved, rows)

    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert len(body["released"]) == 2

    _, conns, _ = await _fork_state(rid)
    assert conns == [], "every hop of a pruned edge releases, far hops included"


@pytest.mark.asyncio
async def test_prune_releases_stale_and_null_edge_key_rows(client):
    """Rows touching the removed device whose edge_key is stale (absent from the
    saved canvas) or NULL (pre-#345 ungrouped) cannot prove a remaining edge is
    served: they release."""
    dut, other = str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas({"nO": other}, [])
    rows = [
        _row(dut, "eth0", other, "p1", "e-gone"),
        _row(dut, "eth1", other, "p2", None),
    ]
    rid = await _mk_fork(saved, rows)

    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 200
    assert len(resp.json()["released"]) == 2

    _, conns, _ = await _fork_state(rid)
    assert conns == []


@pytest.mark.asyncio
async def test_prune_is_idempotent_on_replay(client):
    """A replay (PATCH retry, or the sweep re-driving a marker whose clear failed)
    finds nothing to release: changed false, same version, no new snapshot."""
    dut, other = str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas({"nD": dut, "nO": other}, [("e1", "nD", "nO")])
    rid = await _mk_fork(saved, [_row(dut, "eth0", other, "p1", "e1")])

    first = await _prune(client, rid, [dut])
    assert first.status_code == 200
    assert first.json()["changed"] is True
    assert first.json()["version_number"] == 2

    second = await _prune(client, rid, [dut])
    assert second.status_code == 200
    body = second.json()
    assert body["changed"] is False
    assert body["version_number"] == 2
    assert body["released"] == []

    _, _, versions = await _fork_state(rid)
    assert [v.version_number for v in versions] == [1, 2]


@pytest.mark.asyncio
async def test_prune_never_409s_on_foreign_port_claims(client):
    """The issue #462 trigger kill: a pure release computes no to_build, so another
    ACTIVE fork claiming the same ports (which would 409 a save) cannot refuse the
    prune, and the release still lands."""
    dut, other = str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas({"nD": dut, "nO": other}, [("e1", "nD", "nO")])
    rid = await _mk_fork(saved, [_row(dut, "eth0", other, "p1", "e1")])
    # A second ACTIVE fork claims the exact same endpoints.
    await _mk_fork(
        _canvas({"nD": dut, "nO": other}, [("ex", "nD", "nO")]),
        [_row(dut, "eth0", other, "p1", "ex")],
    )

    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 200
    assert resp.json()["changed"] is True

    _, conns, _ = await _fork_state(rid)
    assert conns == []


@pytest.mark.asyncio
async def test_prune_scrubs_draft_only_content_without_a_version(client):
    """A removed device that exists ONLY in the draft (drawn after the last save,
    with a draft edge) releases nothing and earns no version, but the draft is
    scrubbed so a later user save cannot build wiring for it."""
    a, dut = str(uuid.uuid4()), str(uuid.uuid4())
    saved = _canvas({"nA": a}, [])
    draft = _canvas({"nA": a, "nD": dut}, [("eDraft", "nA", "nD")])
    rid = await _mk_fork(saved, [], draft_canvas=draft)

    resp = await _prune(client, rid, [dut])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["version_number"] == 1
    assert body["released"] == []

    fork, conns, versions = await _fork_state(rid)
    assert conns == []
    assert [v.version_number for v in versions] == [1]
    assert {n["id"] for n in fork.canvas_data["nodes"]} == {"nA"}
    assert fork.canvas_data["edges"] == []
