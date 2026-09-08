"""Unit tests for the Layer 3 routing-intent resolver and write paths (ADR 0014
phase 1, issue #34): the fork_l3_routes model, the set-arithmetic reconcile in
fork_save_service.save_fork and fork_service.create_fork, device-removal pruning,
the internal GET's l3_routes field, and the version-race retry reapply.

Mirrors test_forks.py's fixtures and helpers (its own in-memory engine, ASGI
client, `_canvas`/`_members`/`_make_physical` idioms). `gate_l3_intent` (the L3
validation-and-refuse gate) is patched to a no-op for tests that exercise the
resolver's own set arithmetic, since that pass is already covered end to end by
test_l3_validation.py; the refusal-propagation tests below patch it to raise
instead, to prove save_fork/create_fork honor the refusal without re-testing
validation reasons here.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.connection import Connection
from app.models.fork import ForkL3Route, ForkVersion, ReservationFork
from app.schemas.topology import TopologyValidationResponse
from app.services.fork_save_service import gate_l3_intent as _real_gate_l3_intent
from app.services.fork_save_service import reconcile_l3_route_sets, save_fork
from app.services.fork_service import create_fork
from app.services.l3_intent import RouteSpec
from fastapi import HTTPException
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


@pytest.fixture(autouse=True)
def _no_op_l3_gate():
    """Every test in this file exercises the resolver, not the validation gate
    (that is test_l3_validation.py's job); default the gate to a pass-through so
    a canvas with data.l3 does not need a real inventory double here. Individual
    refusal tests override this with their own patch."""
    with (
        patch("app.services.fork_save_service.gate_l3_intent", new=AsyncMock(return_value=None)),
        patch("app.services.fork_service.gate_l3_intent", new=AsyncMock(return_value=None)),
    ):
        yield


def _hdr() -> dict:
    return {"X-Internal-Token": INTERNAL_TOKEN}


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _make_physical(da, pa, db_dev, pb) -> uuid.UUID:
    async with TestSessionLocal() as db:
        conn = Connection(
            device_a_id=da, port_a=pa, device_b_id=db_dev, port_b=pb, created_by="admin"
        )
        db.add(conn)
        await db.commit()
        return conn.id


def _l3_canvas(entries: list[tuple[uuid.UUID, dict | None]]) -> dict:
    """Build a canvas of device nodes, each optionally carrying data.l3."""
    nodes = []
    for i, (device_id, l3) in enumerate(entries):
        data: dict = {"device": {"id": str(device_id)}}
        if l3 is not None:
            data["l3"] = l3
        nodes.append({"id": f"n{i}", "data": data})
    return {"nodes": nodes, "edges": []}


def _members(canvas: dict) -> set[uuid.UUID]:
    return {
        uuid.UUID(((n.get("data") or {}).get("device") or {}).get("id"))
        for n in canvas.get("nodes") or []
        if ((n.get("data") or {}).get("device") or {}).get("id")
    }


async def _l3_rows(fork_id: uuid.UUID) -> list[ForkL3Route]:
    async with TestSessionLocal() as db:
        return (
            (await db.execute(select(ForkL3Route).where(ForkL3Route.fork_id == fork_id)))
            .scalars()
            .all()
        )


async def _make_active_fork(reservation_id: uuid.UUID) -> uuid.UUID:
    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=reservation_id)
        db.add(fork)
        await db.flush()
        db.add(ForkVersion(fork_id=fork.id, version_number=1))
        await db.commit()
        return fork.id


ROUTE_A = {"destination": "10.0.0.0/24", "interface": "eth0"}
ROUTE_B = {"destination": "10.1.0.0/24", "interface": "eth1", "next_hop": "10.0.0.2"}


# --- Pure set arithmetic: reconcile_l3_route_sets ---


def test_reconcile_l3_all_build_from_empty():
    old_rows, intended = [], {uuid.uuid4(): [RouteSpec("10.0.0.0/24", None, "eth0", None)]}
    to_release, to_build, unchanged = reconcile_l3_route_sets(old_rows, intended)
    assert to_release == []
    assert len(to_build) == 1
    assert unchanged == 0


def test_reconcile_l3_unchanged_when_intent_repeats():
    device_id = uuid.uuid4()
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=device_id,
        destination="10.0.0.0/24",
        interface="eth0",
        route_key="10.0.0.0/24|eth0|",
        created_by="system",
    )
    intended = {device_id: [RouteSpec("10.0.0.0/24", None, "eth0", None)]}
    to_release, to_build, unchanged = reconcile_l3_route_sets([row], intended)
    assert to_release == []
    assert to_build == []
    assert unchanged == 1


def test_reconcile_l3_release_when_intent_empty():
    device_id = uuid.uuid4()
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=device_id,
        destination="10.0.0.0/24",
        interface="eth0",
        route_key="10.0.0.0/24|eth0|",
        created_by="system",
    )
    to_release, to_build, unchanged = reconcile_l3_route_sets([row], {})
    assert to_release == [row]
    assert to_build == []
    assert unchanged == 0


def test_reconcile_l3_move_across_devices_is_release_plus_build():
    """The same route_key moving from one device to another is a release on the
    old device and a build on the new one, never an in-place mutation, mirroring
    ADR 0006's connection-identity move case."""
    device_a, device_b = uuid.uuid4(), uuid.uuid4()
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=device_a,
        destination="10.0.0.0/24",
        interface="eth0",
        route_key="10.0.0.0/24|eth0|",
        created_by="system",
    )
    intended = {device_b: [RouteSpec("10.0.0.0/24", None, "eth0", None)]}
    to_release, to_build, unchanged = reconcile_l3_route_sets([row], intended)
    assert to_release == [row]
    assert len(to_build) == 1
    assert to_build[0][0] == device_b
    assert unchanged == 0


# --- create_fork inserts rows ---


@pytest.mark.asyncio
async def test_create_fork_inserts_l3_routes():
    from app.models.topology import Topology, TopologyVersion

    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A, ROUTE_B]})])

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
        topo_id = topo.id

    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        fork = await create_fork(
            db,
            reservation_id=rid,
            parent_topology_id=topo_id,
            parent_version_id=None,
            member_device_ids=_members(canvas),
        )
        fork_id = fork.id

    rows = await _l3_rows(fork_id)
    assert {r.route_key for r in rows} == {"10.0.0.0/24|eth0|", "10.1.0.0/24|eth1|10.0.0.2"}
    assert all(r.device_id == switch for r in rows)
    assert all(r.created_by == "system" for r in rows)


# --- save_fork: build / release / unchanged, counts, DB rows ---


@pytest.mark.asyncio
async def test_save_fork_builds_l3_routes_and_counts():
    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    fork_id = await _make_active_fork(uuid.uuid4())

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    assert result.l3_routes_built == 1
    assert result.l3_routes_released == 0
    rows = await _l3_rows(fork_id)
    assert len(rows) == 1
    assert rows[0].route_key == "10.0.0.0/24|eth0|"


@pytest.mark.asyncio
async def test_save_fork_second_save_releases_and_builds():
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())

    canvas_1 = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        await save_fork(db, fork, canvas_data=canvas_1, member_device_ids=_members(canvas_1))

    canvas_2 = _l3_canvas([(switch, {"routes": [ROUTE_B]})])
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await save_fork(
            db, fork, canvas_data=canvas_2, member_device_ids=_members(canvas_2)
        )

    assert result.l3_routes_built == 1
    assert result.l3_routes_released == 1
    rows = await _l3_rows(fork_id)
    assert [r.route_key for r in rows] == ["10.1.0.0/24|eth1|10.0.0.2"]


@pytest.mark.asyncio
async def test_save_fork_resaving_the_same_intent_is_unchanged():
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    assert result.l3_routes_built == 0
    assert result.l3_routes_released == 0
    assert len(await _l3_rows(fork_id)) == 1


@pytest.mark.asyncio
async def test_save_fork_duplicate_route_in_canvas_collapses_to_one_row():
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A, dict(ROUTE_A)]})])

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    assert result.l3_routes_built == 1
    assert len(await _l3_rows(fork_id)) == 1


@pytest.mark.asyncio
async def test_save_fork_move_across_devices_end_to_end():
    device_a, device_b = uuid.uuid4(), uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())

    canvas_1 = _l3_canvas([(device_a, {"routes": [ROUTE_A]}), (device_b, None)])
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        await save_fork(db, fork, canvas_data=canvas_1, member_device_ids=_members(canvas_1))

    canvas_2 = _l3_canvas([(device_a, None), (device_b, {"routes": [ROUTE_A]})])
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await save_fork(
            db, fork, canvas_data=canvas_2, member_device_ids=_members(canvas_2)
        )

    assert result.l3_routes_built == 1
    assert result.l3_routes_released == 1
    rows = await _l3_rows(fork_id)
    assert len(rows) == 1
    assert rows[0].device_id == device_b


# --- Save refuses invalid intent: 409 shape, no version appended ---


@pytest.mark.asyncio
async def test_save_fork_refuses_invalid_intent_with_409_and_appends_no_version():
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])

    refusal = HTTPException(
        status_code=409,
        detail={
            "error": "l3_intent_invalid",
            "invalid_routes": [
                {
                    "node_id": "n0",
                    "device_id": str(switch),
                    "index": 0,
                    "reason": "l3_bad_destination",
                    "detail": None,
                }
            ],
        },
    )
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        with patch(
            "app.services.fork_save_service.gate_l3_intent",
            new=AsyncMock(side_effect=refusal),
        ):
            with pytest.raises(HTTPException) as exc:
                await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "l3_intent_invalid"
    assert await _l3_rows(fork_id) == []
    async with TestSessionLocal() as db:
        versions = (
            (await db.execute(select(ForkVersion).where(ForkVersion.fork_id == fork_id)))
            .scalars()
            .all()
        )
    assert [v.version_number for v in versions] == [1]  # only the seeded v1, no v2


@pytest.mark.asyncio
async def test_save_fork_malformed_intent_is_422():
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"bad": "shape"})])

    refusal = HTTPException(
        status_code=422,
        detail={"error": "l3_intent_malformed", "node_id": "n0", "message": "bad shape"},
    )
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        with patch(
            "app.services.fork_save_service.gate_l3_intent",
            new=AsyncMock(side_effect=refusal),
        ):
            with pytest.raises(HTTPException) as exc:
                await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "l3_intent_malformed"


@pytest.mark.asyncio
async def test_create_fork_refuses_invalid_intent_writes_no_row():
    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    rid = uuid.uuid4()

    refusal = HTTPException(
        status_code=409, detail={"error": "l3_intent_invalid", "invalid_routes": []}
    )
    async with TestSessionLocal() as db:
        with patch("app.services.fork_service.gate_l3_intent", new=AsyncMock(side_effect=refusal)):
            with pytest.raises(HTTPException) as exc:
                await create_fork(
                    db,
                    reservation_id=rid,
                    parent_topology_id=None,
                    parent_version_id=None,
                    member_device_ids=_members(canvas),
                )
    assert exc.value.status_code == 409
    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one_or_none()
    assert fork is None


# --- Prune deletes the removed device's rows ---


@pytest.mark.asyncio
async def test_prune_deletes_removed_devices_l3_routes():
    from app.services.fork_save_service import prune_fork_devices

    device_a, device_b = uuid.uuid4(), uuid.uuid4()
    await _make_physical(device_a, "a0", device_b, "b0")
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = {
        "nodes": [
            {"id": "na", "data": {"device": {"id": str(device_a)}, "l3": {"routes": [ROUTE_A]}}},
            {"id": "nb", "data": {"device": {"id": str(device_b)}, "l3": {"routes": [ROUTE_B]}}},
        ],
        "edges": [{"id": "e0", "source": "na", "target": "nb"}],
    }
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        await save_fork(db, fork, canvas_data=canvas, member_device_ids={device_a, device_b})

    rows_before = await _l3_rows(fork_id)
    assert {r.device_id for r in rows_before} == {device_a, device_b}

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await prune_fork_devices(db, fork, [device_a])

    assert result.changed is True
    rows_after = await _l3_rows(fork_id)
    assert {r.device_id for r in rows_after} == {device_b}


@pytest.mark.asyncio
async def test_prune_l3_only_device_with_no_wiring_still_releases():
    """A device carrying only L3 intent (no fork_connections at all) still has its
    routes released by a prune, and the prune still counts as a change."""
    from app.services.fork_save_service import prune_fork_devices

    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    async with TestSessionLocal() as db:
        db.add(
            ForkL3Route(
                fork_id=fork_id,
                device_id=switch,
                destination="10.0.0.0/24",
                interface="eth0",
                route_key="10.0.0.0/24|eth0|",
                created_by="system",
            )
        )
        await db.commit()

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await prune_fork_devices(db, fork, [switch])

    assert result.changed is True
    assert await _l3_rows(fork_id) == []


# --- Retry loop reapplies ---


@pytest.mark.asyncio
async def test_save_fork_l3_reconcile_reapplies_on_version_race_retry():
    """A version-race retry re-runs the L3 reconcile against fresh state, not the
    first pass's stale computation (mirrors the wiring reconcile's own retry
    coverage, e.g. test_save_fork_port_claim_query_reruns_on_retry)."""
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        state = {"raced": False}
        real_commit = db.commit

        async def racing_commit():
            if not state["raced"]:
                state["raced"] = True
                await db.rollback()
                async with TestSessionLocal() as other:
                    other.add(ForkVersion(fork_id=fork_id, version_number=2))
                    await other.commit()
                raise IntegrityError("INSERT", {}, Exception("uq_fork_versions_fork_version"))
            return await real_commit()

        with patch.object(db, "commit", side_effect=racing_commit):
            result = await save_fork(
                db, fork, canvas_data=canvas, member_device_ids=_members(canvas)
            )

    assert result.version_number == 3
    assert result.l3_routes_built == 1
    rows = await _l3_rows(fork_id)
    assert len(rows) == 1  # not double-inserted by the retry


# --- gate_l3_intent placement and short-circuit (coordinator review fix on
# 2ade362c): the gate must run OUTSIDE the fork row lock / port-claim locks and
# must not repeat inventory calls on a version-race retry, and it must not call
# _run_topology_validation at all when the canvas carries no L3 intent. These
# tests use the REAL gate_l3_intent (the module fixture above no-ops it for
# every other test in this file), restoring it for just their own scope.


@pytest.mark.asyncio
async def test_save_fork_no_l3_data_makes_no_validation_or_inventory_call():
    """A canvas with no data.l3 anywhere never calls _run_topology_validation:
    a fork save has never validated physical edge paths and must not start."""
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, None)])

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        with (
            patch("app.services.fork_save_service.gate_l3_intent", new=_real_gate_l3_intent),
            patch(
                "app.routes.topologies._run_topology_validation", new=AsyncMock()
            ) as validate_mock,
        ):
            await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    validate_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_fork_malformed_intent_is_422_with_no_validation_call():
    """A malformed data.l3 shape is refused from the parse alone, with no
    _run_topology_validation call (and therefore no inventory call)."""
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"bad": "shape"})])

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        with (
            patch("app.services.fork_save_service.gate_l3_intent", new=_real_gate_l3_intent),
            patch(
                "app.routes.topologies._run_topology_validation", new=AsyncMock()
            ) as validate_mock,
        ):
            with pytest.raises(HTTPException) as exc:
                await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "l3_intent_malformed"
    validate_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_fork_validation_gate_runs_exactly_once_across_version_race_retry():
    """Valid intent runs _run_topology_validation once per save, even when the
    version-allocation retry loop reapplies reconcile(): proves the gate sits
    OUTSIDE reconcile() and is not repeated by the retry."""
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])

    validate_mock = AsyncMock(
        return_value=TopologyValidationResponse(valid=True, invalid_edges=[], invalid_routes=[])
    )

    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        state = {"raced": False}
        real_commit = db.commit

        async def racing_commit():
            if not state["raced"]:
                state["raced"] = True
                await db.rollback()
                async with TestSessionLocal() as other:
                    other.add(ForkVersion(fork_id=fork_id, version_number=2))
                    await other.commit()
                raise IntegrityError("INSERT", {}, Exception("uq_fork_versions_fork_version"))
            return await real_commit()

        with (
            patch("app.services.fork_save_service.gate_l3_intent", new=_real_gate_l3_intent),
            patch("app.routes.topologies._run_topology_validation", new=validate_mock),
            patch.object(db, "commit", side_effect=racing_commit),
        ):
            result = await save_fork(
                db, fork, canvas_data=canvas, member_device_ids=_members(canvas)
            )

    assert result.version_number == 3
    assert result.l3_routes_built == 1
    validate_mock.assert_awaited_once()


# --- Internal GET carries l3_routes ---


@pytest.mark.asyncio
async def test_internal_get_carries_l3_routes(client):
    switch_1, switch_2 = sorted([uuid.uuid4(), uuid.uuid4()])
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch_1), str(switch_2)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    canvas = _l3_canvas([(switch_1, {"routes": [ROUTE_B]}), (switch_2, {"routes": [ROUTE_A]})])
    save_resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={
            "canvas_data": canvas,
            "member_device_ids": [str(switch_1), str(switch_2)],
        },
        headers=_hdr(),
    )
    assert save_resp.status_code == 200, save_resp.text
    assert save_resp.json()["l3_routes_built"] == 2

    get_resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert [r["device_id"] for r in body["l3_routes"]] == [str(switch_1), str(switch_2)]
    assert body["l3_routes"][0]["destination"] == "10.1.0.0/24"
    assert body["l3_routes"][1]["destination"] == "10.0.0.0/24"
    assert body["l3_routes"][0]["next_hop"] == "10.0.0.2"
    assert body["l3_routes"][1]["next_hop"] is None


# --- Model index present ---


def test_fork_l3_routes_fork_id_index_present():
    found = any(set(index.columns.keys()) == {"fork_id"} for index in ForkL3Route.__table__.indexes)
    assert found, "expected an index on fork_l3_routes.fork_id"


def test_fork_l3_routes_unique_constraint():
    names = {c.name for c in ForkL3Route.__table__.constraints}
    assert "uq_fork_l3_routes_device_route" in names


@pytest.mark.asyncio
async def test_fork_l3_routes_unique_constraint_enforced():
    fork_id = await _make_active_fork(uuid.uuid4())
    device_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        for _ in range(2):
            db.add(
                ForkL3Route(
                    fork_id=fork_id,
                    device_id=device_id,
                    destination="10.0.0.0/24",
                    interface="eth0",
                    route_key="10.0.0.0/24|eth0|",
                    created_by="system",
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()
