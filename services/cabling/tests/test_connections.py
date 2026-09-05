import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from app.services.visible_devices import VisibleDevicesUnavailableError
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "viewer", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


# DB fixtures
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


@pytest.fixture
async def user_client_no_admin():
    """Client where require_admin is NOT overridden, so user role gets 403."""
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _connection_body():
    return {
        "device_a_id": str(uuid.uuid4()),
        "port_a": "eth0",
        "device_b_id": str(uuid.uuid4()),
        "port_b": "eth1",
        "connection_type": "ethernet",
        "notes": "test link",
    }


@pytest.mark.asyncio
async def test_create_connection(admin_client):
    resp = await admin_client.post("/connections", json=_connection_body())
    assert resp.status_code == 201
    data = resp.json()
    assert data["port_a"] == "eth0"
    assert data["created_by"] == "admin"


@pytest.mark.asyncio
async def test_list_connections(admin_client):
    await admin_client.post("/connections", json=_connection_body())
    await admin_client.post("/connections", json=_connection_body())
    resp = await admin_client.get("/connections")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_get_connection(admin_client):
    create_resp = await admin_client.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    resp = await admin_client.get(f"/connections/{conn_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conn_id


@pytest.mark.asyncio
async def test_get_connection_not_found(admin_client):
    fake_id = str(uuid.uuid4())
    resp = await admin_client.get(f"/connections/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_connection(admin_client):
    create_resp = await admin_client.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    resp = await admin_client.delete(f"/connections/{conn_id}")
    assert resp.status_code == 204
    # Verify it is gone
    resp = await admin_client.get(f"/connections/{conn_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_connection_not_found(admin_client):
    fake_id = str(uuid.uuid4())
    resp = await admin_client.delete(f"/connections/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_can_list(user_client):
    """A non-admin caller with an empty visible set still gets 200 + [] (issue #719)."""
    fetch = AsyncMock(return_value=set())
    with patch("app.routes.connections.fetch_visible_device_ids", fetch):
        resp = await user_client.get(
            "/connections", headers={"Authorization": "Bearer viewer-token"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_cannot_create(user_client_no_admin):
    resp = await user_client_no_admin.post("/connections", json=_connection_body())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete(user_client_no_admin):
    # Create via admin first, then try to delete as user
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        create_resp = await ac.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    # Switch to user (no admin override)
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides.pop(require_admin, None)
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/connections/{conn_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_can_get_connection(admin_client, user_client):
    create_resp = await admin_client.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    resp = await user_client.get(f"/connections/{conn_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conn_id


@pytest.mark.asyncio
async def test_create_connection_missing_fields(admin_client):
    resp = await admin_client.post("/connections", json={"device_a_id": str(uuid.uuid4())})
    assert resp.status_code == 422


# --- Edge cases ---


@pytest.mark.asyncio
async def test_list_connections_empty(admin_client):
    """GET /connections with none created returns 200 + empty list."""
    resp = await admin_client.get("/connections")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.fixture
async def unauthenticated_client():
    """Client with zero dependency overrides; real JWT validation runs."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_endpoint(admin_client):
    """GET /health returns 200."""
    resp = await admin_client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_connection_empty_port_name(admin_client):
    """An empty port name is rejected: a connection with no port is degenerate.

    Tightened in #130 (port_a/port_b are Field(min_length=1)). Previously an
    empty port was accepted and stored; it is now a 422.
    """
    body = _connection_body()
    body["port_a"] = ""
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_connection_empty_connection_type(admin_client):
    """connection_type="" edge case."""
    body = _connection_body()
    body["connection_type"] = ""
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 201
    assert resp.json()["connection_type"] == ""


@pytest.mark.asyncio
async def test_create_connection_null_notes(admin_client):
    """notes=null accepted."""
    body = _connection_body()
    body["notes"] = None
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 201
    assert resp.json()["notes"] is None


@pytest.mark.asyncio
async def test_create_connection_preserves_all_fields(admin_client):
    """Verify all response fields match input."""
    body = _connection_body()
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["device_a_id"] == body["device_a_id"]
    assert data["port_a"] == body["port_a"]
    assert data["device_b_id"] == body["device_b_id"]
    assert data["port_b"] == body["port_b"]
    assert data["connection_type"] == body["connection_type"]
    assert data["notes"] == body["notes"]
    assert "id" in data
    assert "created_by" in data


@pytest.mark.asyncio
async def test_unauthenticated_list_connections(unauthenticated_client):
    """No auth returns 401."""
    resp = await unauthenticated_client.get("/connections")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_create_connection(unauthenticated_client):
    """No auth returns 401."""
    resp = await unauthenticated_client.post("/connections", json=_connection_body())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_get_connection(unauthenticated_client):
    """No auth returns 401."""
    fake_id = str(uuid.uuid4())
    resp = await unauthenticated_client.get(f"/connections/{fake_id}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_connection_self_loop(admin_client):
    """device_a_id == device_b_id; documents current behavior (201 accepted)."""
    device_id = str(uuid.uuid4())
    body = {
        "device_a_id": device_id,
        "port_a": "eth0",
        "device_b_id": device_id,
        "port_b": "eth1",
        "connection_type": "ethernet",
        "notes": "self loop",
    }
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_reverse_duplicate_connection(admin_client):
    """A->B then B->A: both succeed (connections are directional)."""
    dev_a = str(uuid.uuid4())
    dev_b = str(uuid.uuid4())
    body1 = {
        "device_a_id": dev_a,
        "port_a": "eth0",
        "device_b_id": dev_b,
        "port_b": "eth1",
        "connection_type": "ethernet",
        "notes": "forward",
    }
    body2 = {
        "device_a_id": dev_b,
        "port_a": "eth1",
        "device_b_id": dev_a,
        "port_b": "eth0",
        "connection_type": "ethernet",
        "notes": "reverse",
    }
    resp1 = await admin_client.post("/connections", json=body1)
    assert resp1.status_code == 201
    resp2 = await admin_client.post("/connections", json=body2)
    assert resp2.status_code == 201
    assert resp1.json()["id"] != resp2.json()["id"]


@pytest.mark.asyncio
async def test_connection_full_lifecycle(admin_client):
    """Create, list (verify present), get by id, delete, get (verify 404)."""
    body = _connection_body()
    create_resp = await admin_client.post("/connections", json=body)
    assert create_resp.status_code == 201
    conn_id = create_resp.json()["id"]
    # List
    list_resp = await admin_client.get("/connections")
    assert list_resp.status_code == 200
    assert any(c["id"] == conn_id for c in list_resp.json()["items"])
    # Get by id
    get_resp = await admin_client.get(f"/connections/{conn_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == conn_id
    # Delete
    del_resp = await admin_client.delete(f"/connections/{conn_id}")
    assert del_resp.status_code == 204
    # Get to verify 404
    gone_resp = await admin_client.get(f"/connections/{conn_id}")
    assert gone_resp.status_code == 404


@pytest.mark.asyncio
async def test_filter_connections_by_device_id(admin_client):
    """GET /connections?device_id= filters to connections involving that device."""
    dev_a = str(uuid.uuid4())
    dev_b = str(uuid.uuid4())
    dev_c = str(uuid.uuid4())
    # Connection between A and B
    body1 = {
        "device_a_id": dev_a,
        "port_a": "eth0",
        "device_b_id": dev_b,
        "port_b": "eth1",
        "connection_type": "ethernet",
    }
    # Connection between B and C
    body2 = {
        "device_a_id": dev_b,
        "port_a": "eth2",
        "device_b_id": dev_c,
        "port_b": "eth3",
        "connection_type": "ethernet",
    }
    # Connection between A and C
    body3 = {
        "device_a_id": dev_a,
        "port_a": "eth4",
        "device_b_id": dev_c,
        "port_b": "eth5",
        "connection_type": "ethernet",
    }
    await admin_client.post("/connections", json=body1)
    await admin_client.post("/connections", json=body2)
    await admin_client.post("/connections", json=body3)

    # Filter by dev_a: should return connections 1 and 3
    resp = await admin_client.get(f"/connections?device_id={dev_a}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    ids = {c["device_a_id"] for c in data["items"]} | {c["device_b_id"] for c in data["items"]}
    assert dev_a in ids

    # Filter by dev_b: should return connections 1 and 2
    resp = await admin_client.get(f"/connections?device_id={dev_b}")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Filter by dev_c: should return connections 2 and 3
    resp = await admin_client.get(f"/connections?device_id={dev_c}")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # No filter: all 3
    resp = await admin_client.get("/connections")
    assert resp.json()["total"] == 3


@pytest.mark.asyncio
async def test_filter_connections_by_nonexistent_device(admin_client):
    """Filter by device_id that has no connections returns empty list."""
    await admin_client.post("/connections", json=_connection_body())
    fake_id = str(uuid.uuid4())
    resp = await admin_client.get(f"/connections?device_id={fake_id}")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


# --- Internal endpoint tests ---


@pytest.fixture
async def internal_client():
    """Client with only DB override; no auth overrides (for internal token testing)."""
    app.dependency_overrides[get_db] = _override_get_db
    with patch("app.routes.connections.settings") as mock_settings:
        mock_settings.internal_api_token = "test-token"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_internal_list_connections_valid_token(admin_client, internal_client):
    """GET /connections/internal with valid X-Internal-Token returns connections."""
    await admin_client.post("/connections", json=_connection_body())
    await admin_client.post("/connections", json=_connection_body())
    resp = await internal_client.get(
        "/connections/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_internal_list_connections_invalid_token_403(internal_client):
    """GET /connections/internal with wrong token returns 403."""
    resp = await internal_client.get(
        "/connections/internal",
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403
    assert "Invalid internal token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_internal_list_connections_missing_token(internal_client):
    """GET /connections/internal without X-Internal-Token header returns 422."""
    resp = await internal_client.get("/connections/internal")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_list_connections_device_filter(admin_client, internal_client):
    """GET /connections/internal?device_id= filters by device."""
    dev_a = str(uuid.uuid4())
    dev_b = str(uuid.uuid4())
    dev_c = str(uuid.uuid4())
    body1 = {
        "device_a_id": dev_a,
        "port_a": "eth0",
        "device_b_id": dev_b,
        "port_b": "eth1",
        "connection_type": "ethernet",
    }
    body2 = {
        "device_a_id": dev_b,
        "port_a": "eth2",
        "device_b_id": dev_c,
        "port_b": "eth3",
        "connection_type": "ethernet",
    }
    await admin_client.post("/connections", json=body1)
    await admin_client.post("/connections", json=body2)
    # Filter by dev_a: should return only body1
    resp = await internal_client.get(
        f"/connections/internal?device_id={dev_a}",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["device_a_id"] == dev_a


@pytest.mark.asyncio
async def test_internal_list_connections_pagination(admin_client, internal_client):
    """GET /connections/internal supports skip/limit pagination."""
    for _ in range(5):
        await admin_client.post("/connections", json=_connection_body())
    resp = await internal_client.get(
        "/connections/internal?skip=0&limit=2",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2

    resp2 = await internal_client.get(
        "/connections/internal?skip=3&limit=10",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total"] == 5
    assert len(data2["items"]) == 2


@pytest.mark.asyncio
async def test_internal_list_connections_empty(internal_client):
    """GET /connections/internal with no connections returns empty list."""
    resp = await internal_client.get(
        "/connections/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_create_connection_same_port_self_loop(admin_client):
    """Connecting a port to itself (same device, same port) returns 422."""
    device_id = str(uuid.uuid4())
    body = {
        "device_a_id": device_id,
        "port_a": "eth0",
        "device_b_id": device_id,
        "port_b": "eth0",
        "connection_type": "ethernet",
    }
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 422
    assert "Cannot connect a port to itself" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_connections_pagination(admin_client):
    """GET /connections supports skip/limit pagination."""
    for _ in range(5):
        await admin_client.post("/connections", json=_connection_body())
    resp = await admin_client.get("/connections?skip=0&limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["skip"] == 0
    assert data["limit"] == 2

    resp2 = await admin_client.get("/connections?skip=4&limit=10")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total"] == 5
    assert len(data2["items"]) == 1


@pytest.mark.asyncio
async def test_create_duplicate_connection(admin_client):
    """Creating same port pair twice succeeds (no unique constraint)."""
    dev_a = str(uuid.uuid4())
    dev_b = str(uuid.uuid4())
    body = {
        "device_a_id": dev_a,
        "port_a": "eth0",
        "device_b_id": dev_b,
        "port_b": "eth1",
        "connection_type": "ethernet",
    }
    resp1 = await admin_client.post("/connections", json=body)
    assert resp1.status_code == 201
    resp2 = await admin_client.post("/connections", json=body)
    assert resp2.status_code == 201
    # Different IDs
    assert resp1.json()["id"] != resp2.json()["id"]


# --- Non-admin visibility filter on GET /connections (issue #719) ---
#
# Fleet-wide GET /connections was flagged as the reconnaissance step behind
# the fork-membership exploit (#701): a non-admin could enumerate device ids
# and port names for gear they cannot otherwise see. These tests pin the fix:
# a non-admin sees only connections touching a device visible to them, an
# admin is never filtered (and never calls inventory), and an unreachable
# inventory fails closed rather than falling back to an unfiltered list.


@pytest.mark.asyncio
async def test_non_admin_sees_only_connections_touching_visible_device(admin_client, user_client):
    """A connection between two non-visible devices is absent from the non-admin's list."""
    visible_dev = str(uuid.uuid4())
    hidden_dev = str(uuid.uuid4())
    other_hidden_dev = str(uuid.uuid4())

    # Connection 1: touches the visible device.
    resp1 = await admin_client.post(
        "/connections",
        json={
            "device_a_id": visible_dev,
            "port_a": "eth0",
            "device_b_id": hidden_dev,
            "port_b": "eth1",
            "connection_type": "ethernet",
        },
    )
    assert resp1.status_code == 201
    visible_conn_id = resp1.json()["id"]

    # Connection 2: between two devices the caller cannot see at all.
    resp2 = await admin_client.post(
        "/connections",
        json={
            "device_a_id": hidden_dev,
            "port_a": "eth2",
            "device_b_id": other_hidden_dev,
            "port_b": "eth3",
            "connection_type": "ethernet",
        },
    )
    assert resp2.status_code == 201

    fetch = AsyncMock(return_value={uuid.UUID(visible_dev)})
    with patch("app.routes.connections.fetch_visible_device_ids", fetch):
        resp = await user_client.get(
            "/connections", headers={"Authorization": "Bearer viewer-token"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    ids = [c["id"] for c in data["items"]]
    assert ids == [visible_conn_id]
    fetch.assert_awaited_once()
    # The caller's own sub is what gets resolved, forwarding their own token.
    called_caller_id, called_auth = fetch.await_args.args
    assert str(called_caller_id) == USER_PAYLOAD["sub"]
    assert called_auth == "Bearer viewer-token"


@pytest.mark.asyncio
async def test_non_admin_visible_set_total_matches_filtered_count(admin_client, user_client):
    """total reflects the SQL-filtered set, not a post-filtered page (pagination-correct)."""
    visible_dev = str(uuid.uuid4())
    for i in range(3):
        resp = await admin_client.post(
            "/connections",
            json={
                "device_a_id": visible_dev,
                "port_a": f"eth{i}",
                "device_b_id": str(uuid.uuid4()),
                "port_b": "eth0",
                "connection_type": "ethernet",
            },
        )
        assert resp.status_code == 201
    # A connection with neither endpoint visible.
    resp = await admin_client.post(
        "/connections",
        json={
            "device_a_id": str(uuid.uuid4()),
            "port_a": "eth0",
            "device_b_id": str(uuid.uuid4()),
            "port_b": "eth1",
            "connection_type": "ethernet",
        },
    )
    assert resp.status_code == 201

    fetch = AsyncMock(return_value={uuid.UUID(visible_dev)})
    with patch("app.routes.connections.fetch_visible_device_ids", fetch):
        resp = await user_client.get(
            "/connections?limit=2", headers={"Authorization": "Bearer viewer-token"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_admin_list_connections_never_calls_inventory(admin_client):
    """Admins stay unfiltered and the visibility lookup is never invoked."""
    await admin_client.post("/connections", json=_connection_body())
    fetch = AsyncMock(return_value=set())
    with patch("app.routes.connections.fetch_visible_device_ids", fetch):
        resp = await admin_client.get("/connections")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_empty_visible_set_returns_empty_page(admin_client, user_client):
    """An empty visible-device set answers [] / total 0, no query against real data."""
    await admin_client.post("/connections", json=_connection_body())
    fetch = AsyncMock(return_value=set())
    with patch("app.routes.connections.fetch_visible_device_ids", fetch):
        resp = await user_client.get(
            "/connections", headers={"Authorization": "Bearer viewer-token"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_non_admin_list_connections_inventory_unreachable_503(admin_client, user_client):
    """Inventory unreachable answers 503 for a non-admin and returns nothing."""
    await admin_client.post("/connections", json=_connection_body())
    fetch = AsyncMock(side_effect=VisibleDevicesUnavailableError("boom"))
    with patch("app.routes.connections.fetch_visible_device_ids", fetch):
        resp = await user_client.get(
            "/connections", headers={"Authorization": "Bearer viewer-token"}
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_non_admin_list_connections_missing_authorization_header(user_client):
    """No Authorization header on the non-admin path is a contract violation (500).

    The user_client fixture overrides get_current_user_payload directly, so
    this can happen in practice only if the header is dropped somewhere
    upstream; mirrors inventory's _fetch_user_group_ids guard for the same
    case.
    """
    resp = await user_client.get("/connections")
    assert resp.status_code == 500
