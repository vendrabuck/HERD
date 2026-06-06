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
