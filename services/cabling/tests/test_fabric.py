import uuid
from unittest.mock import patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from app.services.fabric_service import compute_fabric_id, find_connected_component
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}

INTERNAL_TOKEN = "test-token"

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


def _override_admin():
    return ADMIN_PAYLOAD


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    with patch("app.routes.fabric.settings") as mock_settings:
        mock_settings.internal_api_token = INTERNAL_TOKEN
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def internal_client():
    """Client with only DB override; no auth overrides (for internal token testing)."""
    app.dependency_overrides[get_db] = _override_get_db
    with patch("app.routes.fabric.settings") as mock_settings:
        mock_settings.internal_api_token = INTERNAL_TOKEN
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


# ---- Unit tests for fabric_service functions ----


def test_connected_component_single_device():
    """A device not in the graph forms its own component."""
    graph = {}
    device = uuid.uuid4()
    component = find_connected_component(graph, device)
    assert component == {device}


def test_connected_component_two_connected():
    """Two directly connected devices are in the same component."""
    a, b = uuid.uuid4(), uuid.uuid4()
    graph = {
        a: [(b, "eth1", "eth1")],
        b: [(a, "eth1", "eth1")],
    }
    assert find_connected_component(graph, a) == {a, b}
    assert find_connected_component(graph, b) == {a, b}


def test_connected_component_two_groups():
    """Disconnected groups form separate components."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = {
        a: [(b, "eth1", "eth1")],
        b: [(a, "eth1", "eth1")],
        c: [(d, "eth1", "eth1")],
        d: [(c, "eth1", "eth1")],
    }
    comp_a = find_connected_component(graph, a)
    comp_c = find_connected_component(graph, c)
    assert comp_a == {a, b}
    assert comp_c == {c, d}
    assert comp_a != comp_c


def test_connected_component_chain():
    """A-B-C-D chain: all four in one component."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = {
        a: [(b, "eth1", "eth1")],
        b: [(a, "eth1", "eth1"), (c, "eth2", "eth1")],
        c: [(b, "eth1", "eth2"), (d, "eth2", "eth1")],
        d: [(c, "eth1", "eth2")],
    }
    assert find_connected_component(graph, a) == {a, b, c, d}


def test_fabric_id_deterministic():
    """Same component members always produce the same fabric ID."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    comp = {a, b, c}
    fid1 = compute_fabric_id(comp)
    fid2 = compute_fabric_id(comp)
    assert fid1 == fid2


def test_fabric_id_different_components():
    """Different component members produce different fabric IDs."""
    a, b = uuid.uuid4(), uuid.uuid4()
    c, d = uuid.uuid4(), uuid.uuid4()
    fid_ab = compute_fabric_id({a, b})
    fid_cd = compute_fabric_id({c, d})
    assert fid_ab != fid_cd


def test_fabric_id_same_from_any_member():
    """BFS from any member in the same component yields the same fabric ID."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = {
        a: [(b, "eth1", "eth1")],
        b: [(a, "eth1", "eth1"), (c, "eth2", "eth1")],
        c: [(b, "eth1", "eth2")],
    }
    comp_a = find_connected_component(graph, a)
    comp_b = find_connected_component(graph, b)
    comp_c = find_connected_component(graph, c)
    fid_a = compute_fabric_id(comp_a)
    fid_b = compute_fabric_id(comp_b)
    fid_c = compute_fabric_id(comp_c)
    assert fid_a == fid_b == fid_c


# ---- Endpoint tests ----


@pytest.mark.asyncio
async def test_fabric_endpoint_same_component(admin_client):
    """Two connected devices return the same fabric_id."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    resp_a = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(a)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["device_id"] == str(a)
    assert data_a["component_size"] == 2

    resp_b = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(b)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    data_b = resp_b.json()
    assert data_a["fabric_id"] == data_b["fabric_id"]


@pytest.mark.asyncio
async def test_fabric_endpoint_different_components(admin_client):
    """Disconnected devices return different fabric_ids."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    await _seed_connection(admin_client, c, "eth1", d, "eth1")

    resp_a = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(a)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    resp_c = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(c)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp_a.json()["fabric_id"] != resp_c.json()["fabric_id"]


@pytest.mark.asyncio
async def test_fabric_endpoint_unknown_device(admin_client):
    """A device not in the graph gets its own fabric (component_size=1)."""
    unknown = uuid.uuid4()
    resp = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(unknown)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["component_size"] == 1


@pytest.mark.asyncio
async def test_fabric_endpoint_invalid_token(internal_client):
    """Invalid internal token returns 403."""
    resp = await internal_client.get(
        "/fabric/internal",
        params={"device_id": str(uuid.uuid4())},
        headers={"X-Internal-Token": "bad-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_fabric_endpoint_missing_token(internal_client):
    """Missing internal token returns 422."""
    resp = await internal_client.get(
        "/fabric/internal",
        params={"device_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_fabric_updates_after_connection_added(admin_client):
    """Adding a connection between two groups merges them into one fabric."""
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")
    await _seed_connection(admin_client, c, "eth1", d, "eth1")

    # Initially different fabrics
    resp_a = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(a)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    resp_c = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(c)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp_a.json()["fabric_id"] != resp_c.json()["fabric_id"]

    # Connect B to C, merging the fabrics
    await _seed_connection(admin_client, b, "eth2", c, "eth2")

    resp_a2 = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(a)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    resp_c2 = await admin_client.get(
        "/fabric/internal",
        params={"device_id": str(c)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp_a2.json()["fabric_id"] == resp_c2.json()["fabric_id"]
    assert resp_a2.json()["component_size"] == 4
