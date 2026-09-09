"""Unit tests for the Layer 3 routing-intent resolver and write paths (ADR 0014
phase 1, issue #34; R1-R4, R8, R10, R12 review fixes on 2ade362c): the
fork_l3_routes model, the set-arithmetic reconcile in fork_save_service.save_fork
and fork_service.create_fork, device-removal pruning, the internal GET's
l3_routes field, and the version-race retry reapply.

Mirrors test_forks.py's fixtures and helpers (its own in-memory engine, ASGI
client, `_canvas`/`_members`/`_make_physical` idioms).

R3 moved the L3 gate (`gate_l3_intent`) OUT of `save_fork` entirely: the route
handler (`save_fork_internal`) now resolves the canvas and gates (S5: only when
the L3 intent actually changed) BEFORE taking the fork row lock, then calls
`save_fork` with the already-resolved wiring and already-gated intent. So most
tests here call `save_fork`/`create_fork` directly with a locally resolved
`wiring_resolution`/`intended_routes`, exercising the resolver's own set
arithmetic with no gate and no inventory double involved at all. The handful of
tests that need the REAL gate to run go through the ASGI client against
`/internal/forks/{id}/save`, patching `app.services.fork_save_service.validate_canvas_l3`
(S10: it now takes already-parsed candidates, not a canvas) to a stub, since this
file's concern is the resolver and the write paths, not validation reasons (that
is test_l3_validation.py's job).
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.connection import Connection
from app.models.fork import ForkL3Route, ForkVersion, ReservationFork
from app.schemas.topology import InvalidRoute
from app.services.fork_save_service import gate_l3_intent as _real_gate_l3_intent
from app.services.fork_save_service import (
    l3_intent_changed,
    reconcile_l3_route_sets,
    resolve_canvas_wiring,
)
from app.services.fork_save_service import save_fork as _real_save_fork
from app.services.fork_service import create_fork
from app.services.l3_intent import RouteSpec, parse_l3_intent, walk_l3_nodes
from app.services.l3_validation import L3ValidationResult
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


async def save_fork(db, fork, canvas_data, member_device_ids, **kwargs):
    """Test convenience wrapper (R3): save_fork no longer resolves the canvas or
    gates intent itself; the caller (the route, in production; this helper, in
    tests that do not care about gating) does both. Every test using this helper
    exercises the resolver's own set arithmetic; the gate's own behavior is
    covered separately (the `test_gate_l3_intent_*` section below and the
    ASGI-client tests against the real `/save` route)."""
    wiring_resolution = await resolve_canvas_wiring(db, canvas_data)
    intended_routes = parse_l3_intent(canvas_data)
    return await _real_save_fork(
        db,
        fork,
        canvas_data=canvas_data,
        member_device_ids=member_device_ids,
        wiring_resolution=wiring_resolution,
        intended_routes=intended_routes,
        **kwargs,
    )


ROUTE_A = {"destination": "10.0.0.0/24", "interface": "eth0"}
ROUTE_B = {"destination": "10.1.0.0/24", "interface": "eth1", "next_hop": "10.0.0.2"}


def _rk(destination: str, interface: str, next_hop: str = "", virtual_router: str = "") -> str:
    """The route_key JSON packing (S4 review fix, round 2), for test literals."""
    return json.dumps([destination, interface, next_hop, virtual_router])


# --- S5: l3_intent_changed pure-function tests ---


def test_l3_intent_changed_false_when_route_key_sets_match():
    device_id = uuid.uuid4()
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=device_id,
        destination="10.0.0.0/24",
        interface="eth0",
        route_key=_rk("10.0.0.0/24", "eth0"),
        created_by="system",
    )
    intended = {device_id: [RouteSpec("10.0.0.0/24", None, "eth0", None)]}
    assert l3_intent_changed([row], intended) is False


def test_l3_intent_changed_true_when_a_route_is_added():
    device_id = uuid.uuid4()
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=device_id,
        destination="10.0.0.0/24",
        interface="eth0",
        route_key=_rk("10.0.0.0/24", "eth0"),
        created_by="system",
    )
    intended = {
        device_id: [
            RouteSpec("10.0.0.0/24", None, "eth0", None),
            RouteSpec("10.1.0.0/24", None, "eth1", None),
        ]
    }
    assert l3_intent_changed([row], intended) is True


def test_l3_intent_changed_true_when_a_route_is_removed():
    device_id = uuid.uuid4()
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=device_id,
        destination="10.0.0.0/24",
        interface="eth0",
        route_key=_rk("10.0.0.0/24", "eth0"),
        created_by="system",
    )
    assert l3_intent_changed([row], {}) is True


def test_l3_intent_changed_true_when_device_differs():
    row = ForkL3Route(
        fork_id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        destination="10.0.0.0/24",
        interface="eth0",
        route_key=_rk("10.0.0.0/24", "eth0"),
        created_by="system",
    )
    other_device = uuid.uuid4()
    intended = {other_device: [RouteSpec("10.0.0.0/24", None, "eth0", None)]}
    assert l3_intent_changed([row], intended) is True


def test_l3_intent_changed_false_both_empty():
    assert l3_intent_changed([], {}) is False


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
        route_key=_rk("10.0.0.0/24", "eth0"),
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
        route_key=_rk("10.0.0.0/24", "eth0"),
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
        route_key=_rk("10.0.0.0/24", "eth0"),
        created_by="system",
    )
    intended = {device_b: [RouteSpec("10.0.0.0/24", None, "eth0", None)]}
    to_release, to_build, unchanged = reconcile_l3_route_sets([row], intended)
    assert to_release == [row]
    assert len(to_build) == 1
    assert to_build[0][0] == device_b
    assert unchanged == 0


def test_reconcile_l3_deterministic_ordering():
    """R8: reconcile_by_identity sorts both output lists by identity key, so two
    runs over the same (unordered) input produce the same order."""
    d1, d2 = sorted([uuid.uuid4(), uuid.uuid4()], key=str)
    intended = {
        d2: [RouteSpec("10.0.0.0/24", None, "eth0", None)],
        d1: [RouteSpec("10.0.0.0/24", None, "eth0", None)],
    }
    _, to_build_1, _ = reconcile_l3_route_sets([], intended)
    _, to_build_2, _ = reconcile_l3_route_sets([], intended)
    assert [d for d, _ in to_build_1] == [d for d, _ in to_build_2] == [d1, d2]


# --- create_fork inserts rows (R4: tolerant parse, no gate) ---


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
    assert {r.route_key for r in rows} == {
        _rk("10.0.0.0/24", "eth0"),
        _rk("10.1.0.0/24", "eth1", "10.0.0.2"),
    }
    assert all(r.device_id == switch for r in rows)
    assert all(r.created_by == "system" for r in rows)


@pytest.mark.asyncio
async def test_create_fork_drops_malformed_node_with_warning_writes_other_routes(caplog):
    """R4: create_fork never gates or refuses. A malformed node's data.l3 is
    dropped (logged at WARNING, naming the node) and every well-formed route on
    other nodes is still written as booked."""
    from app.models.topology import Topology, TopologyVersion

    good, bad = uuid.uuid4(), uuid.uuid4()
    canvas = {
        "nodes": [
            {"id": "n0", "data": {"device": {"id": str(good)}, "l3": {"routes": [ROUTE_A]}}},
            {"id": "n1", "data": {"device": {"id": str(bad)}, "l3": {"bad": "shape"}}},
        ],
        "edges": [],
    }
    async with TestSessionLocal() as db:
        topo = Topology(name="parent", created_by=uuid.uuid4(), canvas_data=canvas)
        db.add(topo)
        await db.flush()
        db.add(
            TopologyVersion(
                topology_id=topo.id,
                version_number=1,
                canvas_data=canvas,
                name="parent",
                created_by=uuid.uuid4(),
            )
        )
        await db.commit()
        topo_id = topo.id

    rid = uuid.uuid4()
    with caplog.at_level("WARNING", logger="app.services.l3_intent"):
        async with TestSessionLocal() as db:
            fork = await create_fork(
                db,
                reservation_id=rid,
                parent_topology_id=topo_id,
                parent_version_id=None,
                member_device_ids={good, bad},
            )
            fork_id = fork.id

    rows = await _l3_rows(fork_id)
    assert {r.device_id for r in rows} == {good}
    assert any("n1" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_create_fork_makes_no_inventory_call():
    """R4: create_fork's tolerant parse makes no HTTP call at all (no gate, no
    validate_canvas_l3), regardless of what data.l3 the parent canvas carries."""
    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    rid = uuid.uuid4()
    with patch(
        "app.services.fork_service.resolve_canvas_wiring",
        wraps=resolve_canvas_wiring,
    ):
        with patch("app.services.l3_validation.call_service") as call_service_mock:
            async with TestSessionLocal() as db:
                await create_fork(
                    db,
                    reservation_id=rid,
                    parent_topology_id=None,
                    parent_version_id=None,
                    member_device_ids=_members(canvas),
                )
    call_service_mock.assert_not_called()


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
    assert rows[0].route_key == _rk("10.0.0.0/24", "eth0")


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
    assert [r.route_key for r in rows] == [_rk("10.1.0.0/24", "eth1", "10.0.0.2")]


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


@pytest.mark.asyncio
async def test_save_fork_empty_routes_list_is_no_intent():
    """R10: a node whose data.l3 is {"routes": []} builds and releases nothing."""
    switch = uuid.uuid4()
    fork_id = await _make_active_fork(uuid.uuid4())
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        await save_fork(db, fork, canvas_data=canvas, member_device_ids=_members(canvas))

    empty_canvas = _l3_canvas([(switch, {"routes": []})])
    async with TestSessionLocal() as db:
        fork = await db.get(ReservationFork, fork_id)
        result = await save_fork(
            db, fork, canvas_data=empty_canvas, member_device_ids=_members(empty_canvas)
        )
    assert result.l3_routes_built == 0
    assert result.l3_routes_released == 1  # the previously-built ROUTE_A releases
    assert await _l3_rows(fork_id) == []


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
                route_key=_rk("10.0.0.0/24", "eth0"),
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


# --- gate_l3_intent: takes pre-parsed candidates, returns validated config
# versions (R3, R8, restructured by S5/S6/S10 review fixes, round 2) ---
#
# gate_l3_intent no longer parses the canvas or resolves wiring itself: the
# caller (save_fork_internal) parses once via walk_l3_nodes and passes the
# candidates plus touched_devices directly. On success it returns the per
# -device validated_config_version_id map (S6), not the parsed intent (the
# caller already has that from merge_candidates_by_device, S10).


def _no_invalid(validated_config_version_ids=None) -> L3ValidationResult:
    return L3ValidationResult(
        invalid_routes=[], validated_config_version_ids=validated_config_version_ids or {}
    )


@pytest.mark.asyncio
async def test_gate_l3_intent_returns_validated_config_version_ids():
    """S6: on success the gate returns the per-device config version id its
    caller (validate_canvas_l3, mocked here) says it judged each switch's
    routes against."""
    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    candidates, malformed = walk_l3_nodes(canvas)
    assert malformed == []
    version_id = uuid.uuid4()
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid({switch: version_id})),
    ):
        result = await _real_gate_l3_intent(candidates, {switch})
    assert result == {switch: version_id}


# --- R12: the gate's 409 branch, exact shape, direct test ---


@pytest.mark.asyncio
async def test_gate_l3_intent_409_exact_shape_on_invalid_routes():
    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    candidates, _malformed = walk_l3_nodes(canvas)
    invalid = InvalidRoute(
        node_id="n0", device_id=switch, index=0, reason="l3_bad_destination", detail=None
    )
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(
            return_value=L3ValidationResult(
                invalid_routes=[invalid], validated_config_version_ids={}
            )
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await _real_gate_l3_intent(candidates, {switch})
    assert exc.value.status_code == 409
    assert exc.value.detail == {
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
    }


@pytest.mark.asyncio
async def test_gate_l3_intent_ignores_duplicate_route_entries_for_refusal():
    """S12: an l3_duplicate_route entry alone (no other invalid_routes) does
    NOT make the gate refuse."""
    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    candidates, _malformed = walk_l3_nodes(canvas)
    duplicate = InvalidRoute(node_id="n0", device_id=switch, index=1, reason="l3_duplicate_route")
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(
            return_value=L3ValidationResult(
                invalid_routes=[duplicate], validated_config_version_ids={switch: uuid.uuid4()}
            )
        ),
    ):
        result = await _real_gate_l3_intent(candidates, {switch})
    assert result == {switch: result[switch]}  # returned normally, no raise


@pytest.mark.asyncio
async def test_save_route_409_on_invalid_intent_appends_no_version(client):
    """R12 end to end: the real /save route's gate refusal writes no
    fork_l3_routes row and appends no fork_versions row."""
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    invalid = InvalidRoute(
        node_id="n0", device_id=switch, index=0, reason="l3_bad_destination", detail=None
    )
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(
            return_value=L3ValidationResult(
                invalid_routes=[invalid], validated_config_version_ids={}
            )
        ),
    ):
        save_resp = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert save_resp.status_code == 409
    assert save_resp.json()["detail"]["error"] == "l3_intent_invalid"

    get_resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert get_resp.json()["l3_routes"] == []
    assert len(get_resp.json()["versions"]) == 1  # only the seeded v1


@pytest.mark.asyncio
async def test_save_route_422_on_malformed_intent(client):
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    canvas = _l3_canvas([(switch, {"bad": "shape"})])
    save_resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert save_resp.status_code == 422
    assert save_resp.json()["detail"]["error"] == "l3_intent_malformed"


# --- R3: lock ordering at the real /save route ---


@pytest.mark.asyncio
async def test_save_route_gate_runs_before_for_update_load(client):
    """R3: the gate's inventory-backed validation call is awaited BEFORE the
    fork row's FOR UPDATE load. Proven by patching both callables and recording
    call order."""
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    order: list[str] = []

    async def _spy_validate(*args, **kwargs):
        order.append("validate")
        return _no_invalid()

    import app.routes.forks as forks_module

    real_load_fork = forks_module._load_fork

    async def _spy_load_fork(db, reservation_id, *, for_update=False, refresh=False):
        if for_update:
            order.append("for_update_load")
        return await real_load_fork(db, reservation_id, for_update=for_update, refresh=refresh)

    with (
        patch("app.services.fork_save_service.validate_canvas_l3", new=_spy_validate),
        patch("app.routes.forks._load_fork", new=_spy_load_fork),
    ):
        save_resp = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert save_resp.status_code == 200, save_resp.text
    assert order == ["validate", "for_update_load"]


# --- R2: the save gate reuses save_fork's own wiring_resolution (no second BFS) ---


@pytest.mark.asyncio
async def test_save_route_membership_checked_before_gate_no_inventory_call(client):
    """S3 review fix (round 2): a canvas naming a device outside the reservation
    is refused with 409 fork_device_not_member BEFORE resolve_canvas_wiring or the
    L3 gate ever runs, so the inventory seam (l3_validation.call_service) is never
    awaited and no validation reason about the foreign device can leak."""
    switch = uuid.uuid4()
    member = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(member)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    # The switch carries data.l3 but is NOT in member_device_ids: a foreign device.
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])

    with patch("app.services.l3_validation.call_service") as call_service_mock:
        save_resp = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(member)]},
            headers=_hdr(),
        )
    assert save_resp.status_code == 409, save_resp.text
    assert save_resp.json()["detail"]["error"] == "fork_device_not_member"
    call_service_mock.assert_not_called()


@pytest.mark.asyncio
async def test_save_route_resolves_canvas_wiring_exactly_once(client):
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text

    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    call_count = {"n": 0}
    real_resolve = resolve_canvas_wiring

    async def _counting_resolve(db, canvas_data):
        call_count["n"] += 1
        return await real_resolve(db, canvas_data)

    with (
        patch("app.routes.forks.resolve_canvas_wiring", new=_counting_resolve),
        patch(
            "app.services.fork_save_service.validate_canvas_l3",
            new=AsyncMock(return_value=_no_invalid()),
        ),
    ):
        save_resp = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert save_resp.status_code == 200, save_resp.text
    assert call_count["n"] == 1


# --- S5: the L3 pass runs ONLY on an actual intent delta ---


@pytest.mark.asyncio
async def test_save_route_unchanged_l3_intent_never_calls_validation(client):
    """S5 review fix, round 2: a save whose L3 intent is byte-for-byte unchanged
    from the fork's existing ForkL3Route rows never calls validate_canvas_l3 (and
    so never makes an inventory call, and cannot 503)."""
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])

    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid({switch: uuid.uuid4()})),
    ):
        first = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert first.status_code == 200, first.text
    assert first.json()["l3_routes_built"] == 1

    # Second save, same canvas: the intent is unchanged, so validate_canvas_l3
    # must never be called this time.
    with patch(
        "app.services.fork_save_service.validate_canvas_l3", new=AsyncMock()
    ) as validate_mock:
        second = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert second.status_code == 200, second.text
    assert second.json()["l3_routes_built"] == 0
    assert second.json()["l3_routes_released"] == 0
    validate_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_route_adding_a_route_calls_validation(client):
    """S5: a save that ADDS a route to the existing intent DOES call
    validate_canvas_l3."""
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text
    canvas_1 = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid()),
    ):
        await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas_1, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )

    canvas_2 = _l3_canvas([(switch, {"routes": [ROUTE_A, ROUTE_B]})])
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid()),
    ) as validate_mock:
        second = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas_2, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert second.status_code == 200, second.text
    validate_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_route_removing_all_routes_never_calls_validation(client):
    """S5 (and R10's existing empty-intent short-circuit): a save that removes
    every route makes the new intent empty, so validate_canvas_l3 is never
    called, releasing the previously-built rows regardless."""
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text
    canvas_1 = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid()),
    ):
        await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas_1, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )

    canvas_2 = _l3_canvas([(switch, {"routes": []})])
    with patch(
        "app.services.fork_save_service.validate_canvas_l3", new=AsyncMock()
    ) as validate_mock:
        second = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas_2, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert second.status_code == 200, second.text
    assert second.json()["l3_routes_released"] == 1
    validate_mock.assert_not_awaited()


# --- S6: validated_config_version_id is stamped and carried on the GET ---


@pytest.mark.asyncio
async def test_save_route_stamps_validated_config_version_id(client):
    switch = uuid.uuid4()
    rid = uuid.uuid4()
    resp = await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [str(switch)]},
        headers=_hdr(),
    )
    assert resp.status_code == 201, resp.text
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    version_id = uuid.uuid4()

    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid({switch: version_id})),
    ):
        save_resp = await client.post(
            f"/internal/forks/{rid}/save",
            json={"canvas_data": canvas, "member_device_ids": [str(switch)]},
            headers=_hdr(),
        )
    assert save_resp.status_code == 200, save_resp.text

    rows = await _l3_rows(uuid.UUID(save_resp.json()["fork_id"]))
    assert rows[0].validated_config_version_id == version_id

    get_resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert get_resp.json()["l3_routes"][0]["validated_config_version_id"] == str(version_id)


@pytest.mark.asyncio
async def test_create_fork_leaves_validated_config_version_id_null():
    """S6: the tolerant activation path (fork_service.create_fork) never
    validates, so it always leaves validated_config_version_id NULL."""
    from app.models.topology import Topology, TopologyVersion

    switch = uuid.uuid4()
    canvas = _l3_canvas([(switch, {"routes": [ROUTE_A]})])
    async with TestSessionLocal() as db:
        topo = Topology(name="parent", created_by=uuid.uuid4(), canvas_data=canvas)
        db.add(topo)
        await db.flush()
        db.add(
            TopologyVersion(
                topology_id=topo.id,
                version_number=1,
                canvas_data=canvas,
                name="parent",
                created_by=uuid.uuid4(),
            )
        )
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
    assert rows[0].validated_config_version_id is None


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
    with patch(
        "app.services.fork_save_service.validate_canvas_l3",
        new=AsyncMock(return_value=_no_invalid()),
    ):
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
                    route_key=_rk("10.0.0.0/24", "eth0"),
                    created_by="system",
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()
