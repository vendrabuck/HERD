import uuid

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "viewer", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_connection(client, da_id, port_a, db_id, port_b):
    resp = await client.post(
        "/connections",
        json={
            "device_a_id": str(da_id),
            "port_a": port_a,
            "device_b_id": str(db_id),
            "port_b": port_b,
            "connection_type": "ethernet",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# ---- Algorithm tests ----


@pytest.mark.asyncio
async def test_direct_connection(admin_client, user_client):
    """Two devices directly connected: path has 2 hops."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(b)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 2
    assert len(data["paths"]) == 1
    assert len(data["paths"][0]) == 2
    assert data["paths"][0][0]["device_id"] == str(a)
    assert data["paths"][0][1]["device_id"] == str(b)


@pytest.mark.asyncio
async def test_linear_chain(admin_client, user_client):
    """A-B-C-D chain: path from A to D has 4 hops."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "0/0/1")
    await _seed_connection(admin_client, b, "0/0/2", c, "0/0/1")
    await _seed_connection(admin_client, c, "0/0/2", d, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(d)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 4
    assert len(data["paths"]) == 1
    path_ids = [h["device_id"] for h in data["paths"][0]]
    assert path_ids == [str(a), str(b), str(c), str(d)]


@pytest.mark.asyncio
async def test_no_path(admin_client, user_client):
    """Disconnected components: no path exists."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    await _seed_connection(admin_client, c, "eth1", d, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(d)},
    )
    data = resp.json()
    assert data["reachable"] is False
    assert data["hop_count"] == 0
    assert data["paths"] == []


@pytest.mark.asyncio
async def test_same_device(admin_client, user_client):
    """Source equals target: trivially reachable with 1 hop."""
    a = uuid.uuid4()
    b = uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(a)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 1
    assert data["paths"][0][0]["device_id"] == str(a)


@pytest.mark.asyncio
async def test_shortest_path_wins(admin_client, user_client):
    """With both A-B-C and A-C paths, the direct A-C path (2 hops) is chosen."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "0/0/1")
    await _seed_connection(admin_client, b, "0/0/2", c, "eth1")
    await _seed_connection(admin_client, a, "eth2", c, "eth2")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 2
    assert len(data["paths"]) == 1
    path_ids = [h["device_id"] for h in data["paths"][0]]
    assert path_ids == [str(a), str(c)]


@pytest.mark.asyncio
async def test_cycle_does_not_loop(admin_client, user_client):
    """Graph with a cycle A-B-C-A and branch A-D: finds A-D in 2 hops."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    await _seed_connection(admin_client, b, "eth2", c, "eth1")
    await _seed_connection(admin_client, c, "eth2", a, "eth2")
    await _seed_connection(admin_client, a, "eth3", d, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(d)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 2
    assert len(data["paths"]) == 1
    path_ids = [h["device_id"] for h in data["paths"][0]]
    assert path_ids == [str(a), str(d)]


@pytest.mark.asyncio
async def test_hub_topology(admin_client, user_client):
    """Mimics real infra: DUT1-Edge1-Hub-Edge2-DUT2 (5 hops)."""
    dut1, edge1, hub, edge2, dut2 = (uuid.uuid4() for _ in range(5))
    await _seed_connection(admin_client, dut1, "eth1", edge1, "0/0/1")
    await _seed_connection(admin_client, edge1, "0/7/1", hub, "0/0/1")
    await _seed_connection(admin_client, hub, "0/0/2", edge2, "0/7/1")
    await _seed_connection(admin_client, edge2, "0/0/1", dut2, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(dut1), "target_device_id": str(dut2)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 5
    assert len(data["paths"]) == 1
    path_ids = [h["device_id"] for h in data["paths"][0]]
    assert path_ids == [str(dut1), str(edge1), str(hub), str(edge2), str(dut2)]


@pytest.mark.asyncio
async def test_path_includes_ports(admin_client, user_client):
    """Verify port_in and port_out are populated in the path."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "0/0/1")
    await _seed_connection(admin_client, b, "0/7/1", c, "eth1")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    data = resp.json()
    assert len(data["paths"]) == 1
    path = data["paths"][0]
    assert len(path) == 3

    # Source: no port_in, port_out is eth1
    assert path[0]["port_in"] is None
    assert path[0]["port_out"] == "eth1"

    # Middle: port_in is 0/0/1, port_out is 0/7/1
    assert path[1]["port_in"] == "0/0/1"
    assert path[1]["port_out"] == "0/7/1"

    # Target: port_in is eth1, no port_out
    assert path[2]["port_in"] == "eth1"
    assert path[2]["port_out"] is None


@pytest.mark.asyncio
async def test_parallel_paths_via_distinct_intermediates(admin_client, user_client):
    """Two DUTs reachable via two distinct switches (e.g. L1 patch + L2 switch).

    Repro for issue #220: the Routes tab only showed one path because pathfind
    returned a single shortest. Both equal-cost routes must be enumerated.
    """
    dut1, dut2, sw_l1, sw_l2 = (uuid.uuid4() for _ in range(4))
    await _seed_connection(admin_client, dut1, "eth1", sw_l1, "0/0/1")
    await _seed_connection(admin_client, dut2, "eth1", sw_l1, "0/0/2")
    await _seed_connection(admin_client, dut1, "eth2", sw_l2, "eth1")
    await _seed_connection(admin_client, dut2, "eth2", sw_l2, "eth2")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(dut1), "target_device_id": str(dut2)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert data["hop_count"] == 3
    assert len(data["paths"]) == 2
    intermediates = {p[1]["device_id"] for p in data["paths"]}
    assert intermediates == {str(sw_l1), str(sw_l2)}


@pytest.mark.asyncio
async def test_parallel_cables_through_same_intermediate_collapse(admin_client, user_client):
    """Multiple cables between the same DUTs through the same switch return one route.

    Real labs run N cables through one patch panel; the operator wants to see
    "one route via switch X", not N near-identical entries.
    """
    dut1, dut2, sw = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for i in range(1, 5):
        await _seed_connection(admin_client, dut1, f"eth{i}", sw, f"p{i}")
    for i in range(1, 5):
        await _seed_connection(admin_client, dut2, f"eth{i}", sw, f"q{i}")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(dut1), "target_device_id": str(dut2)},
    )
    data = resp.json()
    assert data["reachable"] is True
    assert len(data["paths"]) == 1
    assert data["paths"][0][1]["device_id"] == str(sw)


@pytest.mark.asyncio
async def test_parallel_paths_preserve_ports(admin_client, user_client):
    """Each enumerated path carries the ports for the cables it traverses."""
    dut1, dut2, sw_a, sw_b = (uuid.uuid4() for _ in range(4))
    await _seed_connection(admin_client, dut1, "eth1", sw_a, "p1")
    await _seed_connection(admin_client, dut2, "eth1", sw_a, "p2")
    await _seed_connection(admin_client, dut1, "eth2", sw_b, "q1")
    await _seed_connection(admin_client, dut2, "eth2", sw_b, "q2")

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(dut1), "target_device_id": str(dut2)},
    )
    data = resp.json()
    paths_by_intermediate = {p[1]["device_id"]: p for p in data["paths"]}
    via_a = paths_by_intermediate[str(sw_a)]
    via_b = paths_by_intermediate[str(sw_b)]
    assert via_a[0]["port_out"] == "eth1"
    assert via_a[1]["port_in"] == "p1"
    assert via_a[1]["port_out"] == "p2"
    assert via_a[2]["port_in"] == "eth1"
    assert via_b[0]["port_out"] == "eth2"
    assert via_b[1]["port_in"] == "q1"
    assert via_b[1]["port_out"] == "q2"
    assert via_b[2]["port_in"] == "eth2"


# ---- Cache invalidation tests ----


@pytest.mark.asyncio
async def test_cache_invalidation_on_create(admin_client, user_client):
    """Creating a connection updates pathfinding results."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    # A to C: not reachable
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    assert resp.json()["reachable"] is False

    # Create B-C connection
    await _seed_connection(admin_client, b, "eth2", c, "eth1")

    # Now A to C is reachable
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    assert resp.json()["reachable"] is True


@pytest.mark.asyncio
async def test_cache_invalidation_on_delete(admin_client, user_client):
    """Deleting a connection updates pathfinding results."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    conn_id = await _seed_connection(admin_client, b, "eth2", c, "eth1")

    # A to C: reachable
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    assert resp.json()["reachable"] is True

    # Delete B-C connection
    del_resp = await admin_client.delete(f"/connections/{conn_id}")
    assert del_resp.status_code == 204

    # Now A to C is not reachable
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    assert resp.json()["reachable"] is False


# ---- Validation tests ----


@pytest.mark.asyncio
async def test_unknown_device_no_path(admin_client, user_client):
    """A device not in the graph returns unreachable."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    unknown = uuid.uuid4()

    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(unknown), "target_device_id": str(b)},
    )
    assert resp.json()["reachable"] is False


@pytest.mark.asyncio
async def test_unauthenticated(admin_client):
    """Pathfind without auth returns 401."""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/pathfind",
            json={
                "source_device_id": str(uuid.uuid4()),
                "target_device_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_missing_field(user_client):
    """Missing target_device_id returns 422."""
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bidirectional(admin_client, user_client):
    """Path works in both directions."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "0/0/1")
    await _seed_connection(admin_client, b, "0/0/2", c, "eth1")

    # Forward: A to C
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    assert resp.json()["reachable"] is True
    assert resp.json()["hop_count"] == 3

    # Reverse: C to A
    resp = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(c), "target_device_id": str(a)},
    )
    assert resp.json()["reachable"] is True
    assert resp.json()["hop_count"] == 3


# ---- Scoped graph load, offload, and cap (issue #139) ----


async def _insert_cable(db, a, port_a, b, port_b):
    """Insert one connection directly through a session (no HTTP)."""
    from app.models.connection import Connection

    db.add(
        Connection(device_a_id=a, port_a=port_a, device_b_id=b, port_b=port_b, created_by="seed")
    )
    await db.commit()


@pytest.mark.asyncio
async def test_scoped_graph_loads_only_in_scope_component():
    """A scoped build loads only the seed devices' component, ignoring other rows.

    Two disjoint fabrics are seeded: {a, b} and {c, d}. Scoping to {a} must load
    only the a-b edge and leave the c-d fabric out of the graph entirely.
    """
    from app.services.pathfind_service import build_adjacency_graph

    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as db:
        await _insert_cable(db, a, "eth1", b, "eth1")
        await _insert_cable(db, c, "eth1", d, "eth1")

        scoped = await build_adjacency_graph(db, device_ids={a})
        full = await build_adjacency_graph(db)

    # Out-of-scope fabric devices are absent from the scoped graph.
    assert set(scoped.keys()) == {a, b}
    assert c not in scoped and d not in scoped
    # The unscoped graph still sees everyone (backward-compatible default).
    assert set(full.keys()) == {a, b, c, d}


@pytest.mark.asyncio
async def test_scoped_graph_includes_off_scope_intermediates():
    """Scoping must not drop a valid path that routes through off-scope hops.

    a - hub - b where only {a, b} are in scope: component expansion must still
    load the hub and both hub edges, so the a to b path survives. This is the
    central correctness guard for #139's scoping.
    """
    from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths

    a, hub, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as db:
        await _insert_cable(db, a, "eth1", hub, "0/0/1")
        await _insert_cable(db, hub, "0/0/2", b, "eth1")

        scoped = await build_adjacency_graph(db, device_ids={a, b})

    # The hub is not in the scope set but must be loaded as an intermediate.
    assert hub in scoped
    paths = find_all_shortest_paths(scoped, a, b)
    assert len(paths) == 1
    assert [h.device_id for h in paths[0]] == [a, hub, b]


@pytest.mark.asyncio
async def test_scoped_results_match_unscoped():
    """For an in-scope query, scoped and unscoped graphs yield identical paths."""
    from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths

    # dut1 - sw_a - dut2 and dut1 - sw_b - dut2 (two equal-cost routes), plus an
    # unrelated isolated fabric that scoping should skip.
    dut1, dut2, sw_a, sw_b = (uuid.uuid4() for _ in range(4))
    other1, other2 = uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as db:
        await _insert_cable(db, dut1, "eth1", sw_a, "p1")
        await _insert_cable(db, dut2, "eth1", sw_a, "p2")
        await _insert_cable(db, dut1, "eth2", sw_b, "q1")
        await _insert_cable(db, dut2, "eth2", sw_b, "q2")
        await _insert_cable(db, other1, "eth1", other2, "eth1")

        scoped = await build_adjacency_graph(db, device_ids={dut1, dut2})
        full = await build_adjacency_graph(db)

    scoped_paths = find_all_shortest_paths(scoped, dut1, dut2)
    full_paths = find_all_shortest_paths(full, dut1, dut2)

    def _key(paths):
        return sorted(tuple(h.device_id for h in p) for p in paths)

    assert _key(scoped_paths) == _key(full_paths)
    assert len(scoped_paths) == 2


@pytest.mark.asyncio
async def test_async_offload_matches_sync():
    """The asyncio.to_thread wrapper returns identical results to the sync call."""
    from app.services.pathfind_service import (
        find_all_shortest_paths,
        find_all_shortest_paths_async,
    )

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = {
        a: [(b, "eth1", "0/0/1")],
        b: [(a, "0/0/1", "eth1"), (c, "0/0/2", "eth1")],
        c: [(b, "eth1", "0/0/2")],
    }
    sync_paths = find_all_shortest_paths(graph, a, c)
    async_paths = await find_all_shortest_paths_async(graph, a, c)

    assert [(h.device_id, h.port_in, h.port_out) for p in sync_paths for h in p] == [
        (h.device_id, h.port_in, h.port_out) for p in async_paths for h in p
    ]


def test_path_count_cap_truncates_on_dense_mesh():
    """A densely meshed graph with many equal-cost routes truncates at the cap.

    source and target are each connected to N distinct intermediates, giving N
    equal-cost 3-hop routes. With N above the cap, enumeration must return
    exactly MAX_ENUMERATED_PATHS rather than hang or return all N.
    """
    from app.services.pathfind_service import MAX_ENUMERATED_PATHS, find_all_shortest_paths

    source, target = uuid.uuid4(), uuid.uuid4()
    n = MAX_ENUMERATED_PATHS + 50
    intermediates = [uuid.uuid4() for _ in range(n)]
    graph: dict = {source: [], target: []}
    for mid in intermediates:
        graph[source].append((mid, "s_out", "m_in"))
        graph[target].append((mid, "t_out", "m_in"))
        graph[mid] = [(source, "m_in", "s_out"), (target, "m_out", "t_out")]

    paths = find_all_shortest_paths(graph, source, target)
    # All routes are distinct (distinct intermediates), so the cap bites exactly.
    assert len(paths) == MAX_ENUMERATED_PATHS
    # Every returned route is a valid 3-hop source to target path.
    for p in paths:
        assert p[0].device_id == source
        assert p[-1].device_id == target
        assert len(p) == 3


def test_path_count_cap_does_not_truncate_collapsed_parallel_cables():
    """Many parallel cables through ONE intermediate collapse to a single route.

    This guards that the cap counts distinct routes, not raw cable permutations:
    2N parallel cables through one switch must still return exactly one route,
    well under the cap, matching the existing collapse semantics.
    """
    from app.services.pathfind_service import find_all_shortest_paths

    dut1, dut2, sw = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph: dict = {dut1: [], dut2: [], sw: []}
    for i in range(40):
        graph[dut1].append((sw, f"d1_{i}", f"p{i}"))
        graph[sw].append((dut1, f"p{i}", f"d1_{i}"))
        graph[dut2].append((sw, f"d2_{i}", f"q{i}"))
        graph[sw].append((dut2, f"q{i}", f"d2_{i}"))

    paths = find_all_shortest_paths(graph, dut1, dut2)
    assert len(paths) == 1
    assert paths[0][1].device_id == sw


# ---- Batch endpoint tests (issue #249) ----


@pytest.mark.asyncio
async def test_batch_multiple_pairs_single_graph_build(admin_client, user_client, monkeypatch):
    """The batch endpoint builds the adjacency graph exactly once for N pairs."""
    from app.routes import pathfind as pathfind_route
    from app.services.pathfind_service import build_adjacency_graph

    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    await _seed_connection(admin_client, b, "eth2", c, "eth1")
    await _seed_connection(admin_client, c, "eth2", d, "eth1")

    calls = []

    async def counting_build(db, device_ids=None):
        calls.append(device_ids)
        return await build_adjacency_graph(db, device_ids=device_ids)

    monkeypatch.setattr(pathfind_route, "build_adjacency_graph", counting_build)

    resp = await user_client.post(
        "/pathfind/batch",
        json={
            "pairs": [
                {"source_device_id": str(a), "target_device_id": str(b)},
                {"source_device_id": str(a), "target_device_id": str(c)},
                {"source_device_id": str(a), "target_device_id": str(d)},
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 3
    assert all(r["reachable"] is True for r in data["results"])
    # One graph build for the whole batch, scoped to every requested endpoint.
    assert len(calls) == 1
    assert calls[0] == {a, b, c, d}


@pytest.mark.asyncio
async def test_batch_preserves_request_order_and_echoes_pairs(admin_client, user_client):
    """Results come back in request order, each echoing its requested pair."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "0/0/1")
    await _seed_connection(admin_client, b, "0/0/2", c, "eth1")

    pairs = [
        {"source_device_id": str(c), "target_device_id": str(a)},
        {"source_device_id": str(a), "target_device_id": str(b)},
        {"source_device_id": str(b), "target_device_id": str(c)},
    ]
    resp = await user_client.post("/pathfind/batch", json={"pairs": pairs})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [(r["source_device_id"], r["target_device_id"]) for r in results] == [
        (p["source_device_id"], p["target_device_id"]) for p in pairs
    ]


@pytest.mark.asyncio
async def test_batch_result_matches_single_endpoint_shape(admin_client, user_client):
    """Each batch entry is byte-for-byte the single response plus the pair echo."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "0/0/1")
    await _seed_connection(admin_client, b, "0/7/1", c, "eth1")

    single = await user_client.post(
        "/pathfind",
        json={"source_device_id": str(a), "target_device_id": str(c)},
    )
    batch = await user_client.post(
        "/pathfind/batch",
        json={"pairs": [{"source_device_id": str(a), "target_device_id": str(c)}]},
    )
    entry = batch.json()["results"][0]
    assert entry.pop("source_device_id") == str(a)
    assert entry.pop("target_device_id") == str(c)
    assert entry == single.json()


@pytest.mark.asyncio
async def test_batch_no_path_pair_matches_single_no_path(admin_client, user_client):
    """An unreachable pair in a batch uses the single endpoint's no-path shape."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    await _seed_connection(admin_client, c, "eth1", d, "eth1")

    resp = await user_client.post(
        "/pathfind/batch",
        json={
            "pairs": [
                {"source_device_id": str(a), "target_device_id": str(b)},
                {"source_device_id": str(a), "target_device_id": str(d)},
            ]
        },
    )
    assert resp.status_code == 200
    reachable, unreachable = resp.json()["results"]
    assert reachable["reachable"] is True
    assert unreachable["reachable"] is False
    assert unreachable["hop_count"] == 0
    assert unreachable["paths"] == []


@pytest.mark.asyncio
async def test_batch_same_device_pair(admin_client, user_client):
    """Source == target inside a batch: trivially reachable with 1 hop."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    resp = await user_client.post(
        "/pathfind/batch",
        json={"pairs": [{"source_device_id": str(a), "target_device_id": str(a)}]},
    )
    result = resp.json()["results"][0]
    assert result["reachable"] is True
    assert result["hop_count"] == 1


@pytest.mark.asyncio
async def test_batch_empty_pairs_returns_empty_results(user_client):
    """An empty pairs list is valid and returns an empty results list."""
    resp = await user_client.post("/pathfind/batch", json={"pairs": []})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


@pytest.mark.asyncio
async def test_batch_over_cap_returns_422(user_client):
    """More than MAX_BATCH_PAIRS pairs is rejected with 422 by the schema."""
    from app.schemas.pathfind import MAX_BATCH_PAIRS

    pair = {"source_device_id": str(uuid.uuid4()), "target_device_id": str(uuid.uuid4())}
    resp = await user_client.post(
        "/pathfind/batch",
        json={"pairs": [pair] * (MAX_BATCH_PAIRS + 1)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_at_cap_accepted(user_client):
    """Exactly MAX_BATCH_PAIRS pairs is accepted (boundary check)."""
    from app.schemas.pathfind import MAX_BATCH_PAIRS

    pair = {"source_device_id": str(uuid.uuid4()), "target_device_id": str(uuid.uuid4())}
    resp = await user_client.post(
        "/pathfind/batch",
        json={"pairs": [pair] * MAX_BATCH_PAIRS},
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == MAX_BATCH_PAIRS


@pytest.mark.asyncio
async def test_batch_missing_pair_field_returns_422(user_client):
    """A pair missing target_device_id is rejected with 422."""
    resp = await user_client.post(
        "/pathfind/batch",
        json={"pairs": [{"source_device_id": str(uuid.uuid4())}]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_unauthenticated(admin_client):
    """Batch pathfind without auth returns 401/403, same as the single endpoint."""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/pathfind/batch",
            json={
                "pairs": [
                    {
                        "source_device_id": str(uuid.uuid4()),
                        "target_device_id": str(uuid.uuid4()),
                    }
                ]
            },
        )
    assert resp.status_code in (401, 403)
