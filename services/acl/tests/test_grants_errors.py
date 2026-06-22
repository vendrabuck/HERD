"""Error-wording and response-shape pins for the grants CRUD endpoints.

test_grants.py asserts the status codes for the duplicate (409) and not-found
(404) paths; this module additionally pins the exact `detail` strings and the
409 conflict body, so a reworded error or a swapped status is caught. It reuses
the same in-memory SQLite plus dependency-override harness as test_grants.py.
"""

import uuid

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.grant import ResourceGrant
from app.routers.grants import bearer_scheme, get_current_user_payload, require_admin
from fastapi.security import HTTPAuthorizationCredentials
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
_group_id = uuid.uuid4()
_device_id = uuid.uuid4()


def _admin_payload():
    return {"sub": str(_admin_id), "role": "admin"}


def _mock_credentials():
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


def _grant_payload(permission="view"):
    return {
        "group_id": str(_group_id),
        "resource_type": "device",
        "resource_id": str(_device_id),
        "permission": permission,
    }


async def _seed_grant(permission="view"):
    async with TestSessionLocal() as session:
        grant = ResourceGrant(
            group_id=_group_id,
            resource_type="device",
            resource_id=_device_id,
            permission=permission,
            granted_by=_admin_id,
        )
        session.add(grant)
        await session.commit()
        await session.refresh(grant)
        return grant


@pytest.mark.asyncio
async def test_create_duplicate_grant_returns_409_with_detail(admin_client):
    first = await admin_client.post("/grants", json=_grant_payload())
    assert first.status_code == 201

    dup = await admin_client.post("/grants", json=_grant_payload())
    assert dup.status_code == 409
    assert dup.json()["detail"] == "This grant already exists"


@pytest.mark.asyncio
async def test_create_grant_same_resource_different_permission_is_not_conflict(admin_client):
    # The uniqueness is on (group, resource, permission); a different permission
    # for the same resource must succeed rather than 409.
    view = await admin_client.post("/grants", json=_grant_payload(permission="view"))
    assert view.status_code == 201
    manage = await admin_client.post("/grants", json=_grant_payload(permission="manage"))
    assert manage.status_code == 201


@pytest.mark.asyncio
async def test_get_missing_grant_returns_404_with_detail(admin_client):
    resp = await admin_client.get(f"/grants/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Grant not found"


@pytest.mark.asyncio
async def test_delete_missing_grant_returns_404_with_detail(admin_client):
    resp = await admin_client.delete(f"/grants/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Grant not found"


@pytest.mark.asyncio
async def test_delete_then_get_is_404(admin_client):
    grant = await _seed_grant()
    deleted = await admin_client.delete(f"/grants/{grant.id}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    follow = await admin_client.get(f"/grants/{grant.id}")
    assert follow.status_code == 404
    assert follow.json()["detail"] == "Grant not found"


@pytest.mark.asyncio
async def test_list_grants_pagination_window_is_returned(admin_client):
    # Two grants on the same resource differing only by permission, paged one at
    # a time, to pin the skip/limit echo in the PaginatedGrantResponse body.
    await _seed_grant(permission="view")
    await _seed_grant(permission="manage")

    page = await admin_client.get("/grants?skip=1&limit=1")
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 2
    assert body["skip"] == 1
    assert body["limit"] == 1
    assert len(body["items"]) == 1
