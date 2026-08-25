import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.grant import ResourceGrant
from app.routers.grants import bearer_scheme, get_current_user_payload, require_admin
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


_admin_id = uuid.uuid4()
_user_id = uuid.uuid4()


def _admin_payload():
    return {"sub": str(_admin_id), "role": "admin"}


def _user_payload():
    return {"sub": str(_user_id), "role": "user"}


def _mock_credentials():
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _admin_payload
    app.dependency_overrides[require_admin] = _admin_payload
    app.dependency_overrides[bearer_scheme] = _mock_credentials
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _user_payload
    app.dependency_overrides[bearer_scheme] = _mock_credentials
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def no_auth_client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Helpers ---

_group_id_1 = uuid.uuid4()
_group_id_2 = uuid.uuid4()
_device_id_1 = uuid.uuid4()
_device_id_2 = uuid.uuid4()
_device_id_3 = uuid.uuid4()
_topology_id_1 = uuid.uuid4()


def _grant_payload(group_id=None, resource_type="device", resource_id=None, permission="view"):
    return {
        "group_id": str(group_id or _group_id_1),
        "resource_type": resource_type,
        "resource_id": str(resource_id or _device_id_1),
        "permission": permission,
    }


async def _create_grant(client, **kwargs):
    return await client.post("/grants", json=_grant_payload(**kwargs))


async def _seed_grant(
    group_id=None,
    resource_type="device",
    resource_id=None,
    permission="view",
    granted_by=None,
):
    async with TestSessionLocal() as session:
        grant = ResourceGrant(
            group_id=group_id or _group_id_1,
            resource_type=resource_type,
            resource_id=resource_id or _device_id_1,
            permission=permission,
            granted_by=granted_by,
        )
        session.add(grant)
        await session.commit()
        await session.refresh(grant)
        return grant


# --- Grant CRUD ---


@pytest.mark.asyncio
async def test_create_grant_admin(admin_client):
    resp = await _create_grant(admin_client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["group_id"] == str(_group_id_1)
    assert data["resource_type"] == "device"
    assert data["resource_id"] == str(_device_id_1)
    assert data["permission"] == "view"
    assert data["granted_by"] == str(_admin_id)


@pytest.mark.asyncio
async def test_create_grant_logs_distinct_fields(admin_client, caplog):
    """The grant-created log line renders each placeholder from a distinct
    field: group_id, permission, resource_type, resource_id, not
    resource_type twice (regression check for a copy-paste log bug)."""
    with caplog.at_level("INFO", logger="app.routers.grants"):
        resp = await _create_grant(admin_client)
    assert resp.status_code == 201

    [record] = [r for r in caplog.records if r.message.startswith("Grant created:")]
    assert record.message == (f"Grant created: group {_group_id_1}, view on device {_device_id_1}")
    assert record.message.count(str(_group_id_1)) == 1
    assert record.message.count(str(_device_id_1)) == 1
    assert record.message.count("device") == 1


@pytest.mark.asyncio
async def test_create_grant_manage(admin_client):
    resp = await _create_grant(admin_client, permission="manage")
    assert resp.status_code == 201
    assert resp.json()["permission"] == "manage"


@pytest.mark.asyncio
async def test_create_grant_user_forbidden(user_client):
    resp = await _create_grant(user_client)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_grant_unauthenticated(no_auth_client):
    resp = await no_auth_client.post("/grants", json=_grant_payload())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_grant_duplicate(admin_client):
    await _create_grant(admin_client)
    resp = await _create_grant(admin_client)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_grant_invalid_resource_type(admin_client):
    resp = await admin_client.post(
        "/grants",
        json=_grant_payload(resource_type="invalid"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_grant_invalid_permission(admin_client):
    resp = await admin_client.post(
        "/grants",
        json=_grant_payload(permission="delete"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_grants(admin_client):
    await _seed_grant(resource_id=_device_id_1)
    await _seed_grant(resource_id=_device_id_2)
    resp = await admin_client.get("/grants")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_list_grants_empty(admin_client):
    resp = await admin_client.get("/grants")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_grants_filter_group_id(admin_client):
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1)
    await _seed_grant(group_id=_group_id_2, resource_id=_device_id_2)
    resp = await admin_client.get(f"/grants?group_id={_group_id_1}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["group_id"] == str(_group_id_1)


@pytest.mark.asyncio
async def test_list_grants_filter_resource_type(admin_client):
    await _seed_grant(resource_type="device", resource_id=_device_id_1)
    await _seed_grant(resource_type="topology", resource_id=_topology_id_1)
    resp = await admin_client.get("/grants?resource_type=topology")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["resource_type"] == "topology"


@pytest.mark.asyncio
async def test_list_grants_filter_resource_id(admin_client):
    await _seed_grant(resource_id=_device_id_1)
    await _seed_grant(resource_id=_device_id_2)
    resp = await admin_client.get(f"/grants?resource_id={_device_id_2}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["resource_id"] == str(_device_id_2)


@pytest.mark.asyncio
async def test_list_grants_user_forbidden(user_client):
    resp = await user_client.get("/grants")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_grant(admin_client):
    grant = await _seed_grant()
    resp = await admin_client.get(f"/grants/{grant.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(grant.id)


@pytest.mark.asyncio
async def test_get_grant_not_found(admin_client):
    resp = await admin_client.get(f"/grants/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_grant(admin_client):
    grant = await _seed_grant()
    resp = await admin_client.delete(f"/grants/{grant.id}")
    assert resp.status_code == 204
    # Verify gone
    resp = await admin_client.get(f"/grants/{grant.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_grant_not_found(admin_client):
    resp = await admin_client.delete(f"/grants/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_grant_user_forbidden(user_client):
    resp = await user_client.delete(f"/grants/{uuid.uuid4()}")
    assert resp.status_code == 403


# --- Permission Check ---


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_allowed(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, permission="view")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["allowed"] is True
    assert len(data["grants"]) == 1


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_denied_no_grant(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_denied_wrong_permission(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, permission="view")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "manage",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_manage_implies_view(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, permission="manage")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is True


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_denied_no_groups(mock_fetch, user_client):
    mock_fetch.return_value = []
    await _seed_grant(group_id=_group_id_1, permission="view")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_denied_wrong_group(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_2]
    await _seed_grant(group_id=_group_id_1, permission="view")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
async def test_check_unauthenticated(no_auth_client):
    resp = await no_auth_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 401


# --- Self-permission guard (#128, IDOR) ---

_other_user_id = uuid.uuid4()


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_rejects_other_user(mock_fetch, user_client):
    """A user querying another user's permission is rejected before any lookup."""
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_other_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot query permissions for another user"
    # The guard must short-circuit before the cross-user group lookup runs.
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_batch_rejects_other_user(mock_fetch, user_client):
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_other_user_id),
            "resource_type": "device",
            "resource_ids": [str(_device_id_1), str(_device_id_2)],
            "permission": "view",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot query permissions for another user"
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_resources_rejects_other_user(mock_fetch, user_client):
    resp = await user_client.get(
        "/resources",
        params={
            "user_id": str(_other_user_id),
            "resource_type": "device",
            "permission": "view",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot query permissions for another user"
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_allows_self(mock_fetch, user_client):
    """The self-case (user_id == sub) still resolves normally; guard is not over-broad."""
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, permission="view")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
    mock_fetch.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_admin_may_query_other_user(mock_fetch, admin_client):
    """Admins and superadmins may introspect any user's permissions."""
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, permission="view")
    resp = await admin_client.post(
        "/check",
        json={
            "user_id": str(_other_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
    mock_fetch.assert_awaited_once()


# --- Batch Check ---


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="view")
    # device_id_2 has no grant
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [str(_device_id_1), str(_device_id_2)],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[str(_device_id_1)] is True
    assert results[str(_device_id_2)] is False


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check_manage_implies_view(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="manage")
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [str(_device_id_1)],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["results"][str(_device_id_1)] is True


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check_empty_groups(mock_fetch, user_client):
    mock_fetch.return_value = []
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="view")
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [str(_device_id_1)],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["results"][str(_device_id_1)] is False


# --- Resources ---


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_get_resources(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="view")
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_2, permission="view")
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=device&permission=view"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    ids = {r["resource_id"] for r in data}
    assert str(_device_id_1) in ids
    assert str(_device_id_2) in ids


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_get_resources_empty(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=device&permission=view"
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_get_resources_manage_implies_view(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="manage")
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=device&permission=view"
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# --- Edge Cases ---


@pytest.mark.asyncio
async def test_grant_for_nonexistent_group(admin_client):
    """Grants for nonexistent groups are allowed (no FK constraint)."""
    fake_group = uuid.uuid4()
    resp = await _create_grant(admin_client, group_id=fake_group)
    assert resp.status_code == 201
    assert resp.json()["group_id"] == str(fake_group)


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_delete_grant_then_check_denied(mock_fetch, admin_client):
    mock_fetch.return_value = [_group_id_1]
    grant = await _seed_grant(group_id=_group_id_1, permission="view")
    # Delete the grant
    resp = await admin_client.delete(f"/grants/{grant.id}")
    assert resp.status_code == 204
    # Now override to allow check endpoint (admin_client already has admin payload)
    resp = await admin_client.post(
        "/check",
        json={
            "user_id": str(_admin_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
async def test_create_grant_topology(admin_client):
    resp = await _create_grant(
        admin_client,
        resource_type="topology",
        resource_id=_topology_id_1,
        permission="manage",
    )
    assert resp.status_code == 201
    assert resp.json()["resource_type"] == "topology"


@pytest.mark.asyncio
async def test_create_grant_reservation(admin_client):
    res_id = uuid.uuid4()
    resp = await _create_grant(
        admin_client,
        resource_type="reservation",
        resource_id=res_id,
        permission="view",
    )
    assert resp.status_code == 201
    assert resp.json()["resource_type"] == "reservation"


@pytest.mark.asyncio
async def test_same_group_different_permissions(admin_client):
    """Same group can have view and manage on the same resource."""
    resp1 = await _create_grant(admin_client, permission="view")
    resp2 = await _create_grant(admin_client, permission="manage")
    assert resp1.status_code == 201
    assert resp2.status_code == 201


@pytest.mark.asyncio
async def test_health(admin_client):
    resp = await admin_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "acl"


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_auth_service_returns_empty_groups(mock_fetch, user_client):
    """When fetch_user_groups returns empty (e.g., auth service unreachable),
    check returns allowed: false."""
    mock_fetch.return_value = []
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check_empty_groups_returns_all_false(mock_fetch, user_client):
    """When fetch_user_groups returns empty for batch check, all results are false."""
    mock_fetch.return_value = []
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [str(_device_id_1), str(_device_id_2)],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[str(_device_id_1)] is False
    assert results[str(_device_id_2)] is False


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_resources_manage_implies_view(mock_fetch, admin_client):
    """Grant 'manage'; querying resources with permission='view' should include it."""
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, permission="manage")
    resp = await admin_client.get(
        "/resources",
        params={
            "user_id": str(_admin_id),
            "resource_type": "device",
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    resource_ids = [item["resource_id"] for item in resp.json()]
    assert str(_device_id_1) in resource_ids


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check_empty_resource_ids(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == {}


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check_duplicate_resource_ids(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="view")
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [str(_device_id_1), str(_device_id_1)],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[str(_device_id_1)] is True


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_fetch_user_groups_returns_empty_allows_false(mock_fetch, user_client):
    """When fetch_user_groups returns empty (auth error), check returns allowed:false."""
    mock_fetch.return_value = []
    await _seed_grant(group_id=_group_id_1, permission="manage")
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
async def test_list_grants_pagination(admin_client):
    await _seed_grant(resource_id=_device_id_1)
    await _seed_grant(resource_id=_device_id_2)
    await _seed_grant(resource_id=_device_id_3)
    resp = await admin_client.get("/grants?skip=0&limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2

    resp2 = await admin_client.get("/grants?skip=2&limit=2")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total"] == 3
    assert len(data2["items"]) == 1


@pytest.mark.asyncio
async def test_create_grant_missing_group_id(admin_client):
    resp = await admin_client.post(
        "/grants",
        json={
            "resource_type": "device",
            "resource_id": str(_device_id_1),
            "permission": "view",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_resources_with_multiple_groups(mock_fetch, user_client):
    mock_fetch.return_value = [_group_id_1, _group_id_2]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="view")
    await _seed_grant(group_id=_group_id_2, resource_id=_device_id_2, permission="view")
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=device&permission=view"
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {r["resource_id"] for r in data}
    assert str(_device_id_1) in ids
    assert str(_device_id_2) in ids


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_reservation_resource_type(mock_fetch, user_client):
    """Check permission works with resource_type='reservation'."""
    mock_fetch.return_value = [_group_id_1]
    res_id = uuid.uuid4()
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="reservation",
        resource_id=res_id,
        permission="view",
    )
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "reservation",
            "resource_id": str(res_id),
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is True


# --- Additional coverage tests ---


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_batch_check_multiple_resources_mixed(mock_fetch, user_client):
    """Batch check with 3 resources: 2 granted, 1 not."""
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_1, permission="view")
    await _seed_grant(group_id=_group_id_1, resource_id=_device_id_3, permission="manage")
    # device_id_2 has no grant
    resp = await user_client.post(
        "/check/batch",
        json={
            "user_id": str(_user_id),
            "resource_type": "device",
            "resource_ids": [
                str(_device_id_1),
                str(_device_id_2),
                str(_device_id_3),
            ],
            "permission": "view",
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[str(_device_id_1)] is True
    assert results[str(_device_id_2)] is False
    # manage implies view
    assert results[str(_device_id_3)] is True


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_list_resources_by_type_topology(mock_fetch, user_client):
    """GET /resources with resource_type=topology returns only topology grants."""
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="device",
        resource_id=_device_id_1,
        permission="view",
    )
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="topology",
        resource_id=_topology_id_1,
        permission="view",
    )
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=topology&permission=view"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["resource_type"] == "topology"
    assert data[0]["resource_id"] == str(_topology_id_1)


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_list_resources_by_type_reservation(mock_fetch, user_client):
    """GET /resources with resource_type=reservation returns only reservation grants."""
    mock_fetch.return_value = [_group_id_1]
    res_id = uuid.uuid4()
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="reservation",
        resource_id=res_id,
        permission="manage",
    )
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="device",
        resource_id=_device_id_1,
        permission="view",
    )
    # Query for reservations with view (manage implies view)
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=reservation&permission=view"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["resource_id"] == str(res_id)


@pytest.mark.asyncio
async def test_health_returns_service_name(admin_client):
    """GET /health returns service=acl."""
    resp = await admin_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "acl"


@pytest.mark.asyncio
async def test_list_grants_filter_combined(admin_client):
    """Filter grants by group_id and resource_type together."""
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="device",
        resource_id=_device_id_1,
    )
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="topology",
        resource_id=_topology_id_1,
    )
    await _seed_grant(
        group_id=_group_id_2,
        resource_type="device",
        resource_id=_device_id_2,
    )
    resp = await admin_client.get(f"/grants?group_id={_group_id_1}&resource_type=device")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["group_id"] == str(_group_id_1)
    assert data["items"][0]["resource_type"] == "device"


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_resources_no_groups_returns_empty(mock_fetch, user_client):
    """GET /resources with no user groups returns empty list."""
    mock_fetch.return_value = []
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="device",
        resource_id=_device_id_1,
        permission="view",
    )
    resp = await user_client.get(
        f"/resources?user_id={_user_id}&resource_type=device&permission=view"
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
@patch("app.routers.grants.fetch_user_groups", new_callable=AsyncMock)
async def test_check_topology_manage(mock_fetch, user_client):
    """Check manage permission on topology resource type."""
    mock_fetch.return_value = [_group_id_1]
    await _seed_grant(
        group_id=_group_id_1,
        resource_type="topology",
        resource_id=_topology_id_1,
        permission="manage",
    )
    resp = await user_client.post(
        "/check",
        json={
            "user_id": str(_user_id),
            "resource_type": "topology",
            "resource_id": str(_topology_id_1),
            "permission": "manage",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
