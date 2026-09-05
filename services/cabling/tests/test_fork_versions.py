"""GET .../versions/{version_id} and POST .../versions/{version_id}/restore (issue #622).

Restore is restore-to-draft, never restore-and-reconcile (ADR 0006 addendum,
2026-08-28, revised after PR #623 review): it copies a version's canvas onto the
fork's draft canvas_data and sets a draft_restored_from_id marker on the fork row.
It deliberately appends NO fork_versions row of its own (a version means something
was reconciled, and the standing wiring-heal reconciler relies on that to tell a
missed save from a canvas-only change); the marker rides on the fork row until the
NEXT save, which carries it onto the version it appends as that version's own
restored_from_id and clears it. Restore must never touch fork_connections, the
wiring ledger, or the outbox; that is the load-bearing assertion the tests below
pin, alongside the "no version appended" and "marker survives a canvas PUT, only a
save consumes it" invariants.
"""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.connection import Connection
from app.models.fork import (
    ForkConnection,
    ForkStatus_ARCHIVED,
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


def _canvas(nodes: list[uuid.UUID], edges: list[tuple[int, int]]) -> dict:
    return {
        "nodes": [{"id": f"n{i}", "data": {"device": {"id": str(d)}}} for i, d in enumerate(nodes)],
        "edges": [
            {"id": f"e{k}", "source": f"n{s}", "target": f"n{t}"} for k, (s, t) in enumerate(edges)
        ],
    }


def _members(canvas: dict) -> list[str]:
    """Device ids referenced by a canvas's nodes (2026-09-04 fork endpoint-membership
    fix): the default ``member_device_ids`` for tests that are not exercising the
    membership check itself, so a create or save succeeds with no restriction."""
    ids = []
    for node in canvas.get("nodes") or []:
        device_id = ((node.get("data") or {}).get("device") or {}).get("id")
        if device_id:
            ids.append(device_id)
    return ids


async def _make_physical(da, pa, db_dev, pb) -> uuid.UUID:
    async with TestSessionLocal() as db:
        conn = Connection(
            device_a_id=da, port_a=pa, device_b_id=db_dev, port_b=pb, created_by="admin"
        )
        db.add(conn)
        await db.commit()
        return conn.id


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


async def _fork_connections(fork_id: uuid.UUID) -> list[ForkConnection]:
    async with TestSessionLocal() as db:
        return (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork_id)))
            .scalars()
            .all()
        )


def _connection_identities(rows: list[ForkConnection]) -> set[tuple]:
    return {(r.device_a_id, r.port_a, r.device_b_id, r.port_b, r.layer) for r in rows}


async def _create_fork_from_parent(client, canvas: dict) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a fork pinned to a parent topology carrying ``canvas``; return (rid, fork_id)."""
    topo_id, _ = await _make_topology_with_version(canvas)
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={
            "reservation_id": str(rid),
            "parent_topology_id": str(topo_id),
            "member_device_ids": _members(canvas),
        },
        headers=_hdr(),
    )
    return rid, uuid.UUID(resp.json()["fork_id"])


async def _get_version_id(client, rid: uuid.UUID, version_number: int) -> uuid.UUID:
    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    versions = detail.json()["versions"]
    match = next(v for v in versions if v["version_number"] == version_number)
    return uuid.UUID(match["id"])


# --- GET /internal/forks/{reservation_id}/versions/{version_id} ---------------------


@pytest.mark.asyncio
async def test_get_fork_version_requires_internal_token(client):
    resp = await client.get(
        f"/internal/forks/{uuid.uuid4()}/versions/{uuid.uuid4()}",
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_fork_version_404_when_fork_absent(client):
    resp = await client.get(
        f"/internal/forks/{uuid.uuid4()}/versions/{uuid.uuid4()}", headers=_hdr()
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Fork not found"


@pytest.mark.asyncio
async def test_get_fork_version_404_when_version_id_unknown(client):
    a, b = uuid.uuid4(), uuid.uuid4()
    rid, _fork_id = await _create_fork_from_parent(client, _canvas([a, b], [(0, 1)]))

    resp = await client.get(f"/internal/forks/{rid}/versions/{uuid.uuid4()}", headers=_hdr())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Version not found"


@pytest.mark.asyncio
async def test_get_fork_version_404_for_a_foreign_forks_version(client):
    """A version id that exists, but belongs to a different fork, 404s (no leak)."""
    a, b = uuid.uuid4(), uuid.uuid4()
    _rid_1, _fork_1 = await _create_fork_from_parent(client, _canvas([a, b], [(0, 1)]))
    rid_2, _fork_2 = await _create_fork_from_parent(client, _canvas([a, b], [(0, 1)]))
    foreign_version_id = await _get_version_id(client, _rid_1, 1)

    resp = await client.get(
        f"/internal/forks/{rid_2}/versions/{foreign_version_id}", headers=_hdr()
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Version not found"


@pytest.mark.asyncio
async def test_get_fork_version_returns_canvas_data(client):
    a, b = uuid.uuid4(), uuid.uuid4()
    canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, canvas)
    version_id = await _get_version_id(client, rid, 1)

    resp = await client.get(f"/internal/forks/{rid}/versions/{version_id}", headers=_hdr())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(version_id)
    assert body["version_number"] == 1
    assert body["restored_from_id"] is None
    assert body["canvas_data"] == canvas


# --- POST /internal/forks/{reservation_id}/versions/{version_id}/restore ------------


@pytest.mark.asyncio
async def test_restore_requires_internal_token(client):
    resp = await client.post(
        f"/internal/forks/{uuid.uuid4()}/versions/{uuid.uuid4()}/restore",
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_restore_404_when_fork_absent(client):
    resp = await client.post(
        f"/internal/forks/{uuid.uuid4()}/versions/{uuid.uuid4()}/restore", headers=_hdr()
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Fork not found"


@pytest.mark.asyncio
async def test_restore_404_for_a_foreign_version(client):
    a, b = uuid.uuid4(), uuid.uuid4()
    _rid_1, _fork_1 = await _create_fork_from_parent(client, _canvas([a, b], [(0, 1)]))
    rid_2, _fork_2 = await _create_fork_from_parent(client, _canvas([a, b], [(0, 1)]))
    foreign_version_id = await _get_version_id(client, _rid_1, 1)

    resp = await client.post(
        f"/internal/forks/{rid_2}/versions/{foreign_version_id}/restore", headers=_hdr()
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Version not found"


@pytest.mark.asyncio
async def test_restore_409_when_fork_archived(client):
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": []},
        headers=_hdr(),
    )
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        fork.status = ForkStatus_ARCHIVED
        await db.commit()
    version_id = await _get_version_id(client, rid, 1)

    resp = await client.post(f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr())
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_restore_replaces_draft_canvas_byte_for_byte(client):
    """After a loose draft edit, restoring v1 puts v1's canvas back verbatim."""
    a, b = uuid.uuid4(), uuid.uuid4()
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    # Loose-edit the draft away from v1's canvas (no new version).
    other_canvas = _canvas([a, b], [])
    put_resp = await client.put(
        f"/internal/forks/{rid}/canvas", json={"canvas_data": other_canvas}, headers=_hdr()
    )
    assert put_resp.status_code == 200, put_resp.text

    restore_resp = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert restore_resp.status_code == 200, restore_resp.text

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert detail.json()["canvas_data"] == v1_canvas


@pytest.mark.asyncio
async def test_restore_appends_no_version_and_sets_marker(client):
    """Restore's response and the fork's version list both prove NO version landed.

    issue #622 (revised after PR #623 review): a version means something was
    reconciled, so restore-to-draft must never append one. It sets
    draft_restored_from_id on the fork row instead, echoed in the response.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    resp = await client.post(f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_restored_from_id"] == str(version_id)
    assert body["valid"] is True
    assert body["invalid_edges"] == []
    # ForkCanvasUpdateResponse's exact shape plus the marker: no "version" key.
    assert "version" not in body
    assert set(body) == {"id", "valid", "invalid_edges", "draft_restored_from_id"}

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    detail_body = detail.json()
    assert detail_body["draft_restored_from_id"] == str(version_id)
    versions = detail_body["versions"]
    assert len(versions) == 1, f"restore must append no fork_versions row, got {versions}"
    assert versions[0]["version_number"] == 1


@pytest.mark.asyncio
async def test_restore_twice_still_appends_no_version(client):
    """Restoring repeatedly just keeps overwriting the marker; still no version."""
    a, b = uuid.uuid4(), uuid.uuid4()
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    first = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert second.status_code == 200, second.text
    assert second.json()["draft_restored_from_id"] == str(version_id)

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert len(detail.json()["versions"]) == 1


@pytest.mark.asyncio
async def test_canvas_put_between_restore_and_save_keeps_marker(client):
    """A loose canvas PUT after a restore leaves draft_restored_from_id in place.

    The user is still editing the restored draft; only a save consumes the marker.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    restore_resp = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert restore_resp.status_code == 200, restore_resp.text

    put_resp = await client.put(
        f"/internal/forks/{rid}/canvas",
        json={"canvas_data": _canvas([a, b], [])},
        headers=_hdr(),
    )
    assert put_resp.status_code == 200, put_resp.text

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert detail.json()["draft_restored_from_id"] == str(version_id)
    assert len(detail.json()["versions"]) == 1


@pytest.mark.asyncio
async def test_save_after_restore_carries_marker_and_clears_it(client):
    """The FIRST save after a restore is the one that carries restored_from_id.

    It appends a new fork_versions row (v2) whose restored_from_id equals the
    restored version's id, and the fork's draft_restored_from_id marker is cleared
    in that same save.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    restore_resp = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert restore_resp.status_code == 200, restore_resp.text

    save_resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": v1_canvas, "member_device_ids": _members(v1_canvas)},
        headers=_hdr(),
    )
    assert save_resp.status_code == 200, save_resp.text
    assert save_resp.json()["version_number"] == 2

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    detail_body = detail.json()
    assert detail_body["draft_restored_from_id"] is None
    versions = detail_body["versions"]
    assert len(versions) == 2
    v2 = next(v for v in versions if v["version_number"] == 2)
    assert v2["restored_from_id"] == str(version_id)


@pytest.mark.asyncio
async def test_second_save_after_restore_carries_no_marker(client):
    """Only the save that CONSUMES the marker carries restored_from_id; the next
    save, with no pending restore, appends a version with restored_from_id null."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    await _make_physical(a, "a1", c, "c0")
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    restore_resp = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert restore_resp.status_code == 200, restore_resp.text

    first_save = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": v1_canvas, "member_device_ids": _members(v1_canvas)},
        headers=_hdr(),
    )
    assert first_save.status_code == 200, first_save.text
    assert first_save.json()["version_number"] == 2

    second_save = await client.post(
        f"/internal/forks/{rid}/save",
        json={
            "canvas_data": _canvas([a, c], [(0, 1)]),
            "member_device_ids": _members(_canvas([a, c], [(0, 1)])),
        },
        headers=_hdr(),
    )
    assert second_save.status_code == 200, second_save.text
    assert second_save.json()["version_number"] == 3

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    versions = detail.json()["versions"]
    v3 = next(v for v in versions if v["version_number"] == 3)
    assert v3["restored_from_id"] is None
    assert detail.json()["draft_restored_from_id"] is None


@pytest.mark.asyncio
async def test_restore_does_not_touch_fork_connections(client):
    """Restore is a canvas-only write: fork_connections survive it untouched.

    Builds a fork wired A-B from v1, saves A-C over it (v2's wiring, released A-B
    and built A-C), then restores v1. The wiring must still be A-C: restore never
    re-runs the release-before-build reconcile, and appends no version.
    """
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _make_physical(a, "a0", b, "b0")
    await _make_physical(a, "a1", c, "c0")
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, fork_id = await _create_fork_from_parent(client, v1_canvas)
    version_id = await _get_version_id(client, rid, 1)

    save_resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={
            "canvas_data": _canvas([a, c], [(0, 1)]),
            "member_device_ids": _members(_canvas([a, c], [(0, 1)])),
        },
        headers=_hdr(),
    )
    assert save_resp.status_code == 200, save_resp.text

    conns_before = await _fork_connections(fork_id)
    identities_before = _connection_identities(conns_before)
    assert identities_before == {(a, "a1", c, "c0", "L1")}

    restore_resp = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert restore_resp.status_code == 200, restore_resp.text
    assert restore_resp.json()["draft_restored_from_id"] == str(version_id)

    conns_after = await _fork_connections(fork_id)
    assert _connection_identities(conns_after) == identities_before

    detail = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert detail.json()["canvas_data"] == v1_canvas
    # v1 and v2 (from the save above) only: restore appended nothing.
    assert len(detail.json()["versions"]) == 2
    assert {v["version_number"] for v in detail.json()["versions"]} == {1, 2}


@pytest.mark.asyncio
async def test_get_fork_detail_exposes_draft_restored_from_id(client):
    """GET /internal/forks/{reservation_id} carries the marker so the frontend can
    label an unsaved restored draft."""
    a, b = uuid.uuid4(), uuid.uuid4()
    v1_canvas = _canvas([a, b], [(0, 1)])
    rid, _fork_id = await _create_fork_from_parent(client, v1_canvas)

    fresh = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert fresh.json()["draft_restored_from_id"] is None

    version_id = await _get_version_id(client, rid, 1)
    restore_resp = await client.post(
        f"/internal/forks/{rid}/versions/{version_id}/restore", headers=_hdr()
    )
    assert restore_resp.status_code == 200, restore_resp.text

    after_restore = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert after_restore.json()["draft_restored_from_id"] == str(version_id)
