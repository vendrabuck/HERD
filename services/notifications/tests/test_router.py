import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.routers.notifications import get_bearer_credentials, get_current_user_payload
from app.services import notification_service
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


_user_id = uuid.uuid4()
_other_user_id = uuid.uuid4()


def _payload_for(user_id: uuid.UUID):
    def _payload():
        return {"sub": str(user_id), "role": "user"}

    return _payload


def _credentials_for(user_id: uuid.UUID):
    def _creds():
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=f"token-{user_id}")

    return _creds


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _payload_for(_user_id)
    app.dependency_overrides[get_bearer_credentials] = _credentials_for(_user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def other_user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _payload_for(_other_user_id)
    app.dependency_overrides[get_bearer_credentials] = _credentials_for(_other_user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def no_auth_client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_notification(
    user_id: uuid.UUID,
    *,
    event_type: str = "reservation.created",
    title: str = "Test",
    body: str = "body",
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        n = await notification_service.create(
            session,
            user_id=user_id,
            event_type=event_type,
            title=title,
            body=body,
            data={"reservation_id": str(uuid.uuid4())},
        )
        return n.id


# --- List / count ---


@pytest.mark.asyncio
async def test_list_empty(user_client):
    resp = await user_client.get("/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"items": [], "total": 0, "unread": 0}


@pytest.mark.asyncio
async def test_list_returns_only_caller_notifications(user_client):
    mine = await _seed_notification(_user_id, title="mine")
    await _seed_notification(_other_user_id, title="theirs")
    resp = await user_client.get("/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["unread"] == 1
    assert data["items"][0]["id"] == str(mine)
    assert data["items"][0]["title"] == "mine"


@pytest.mark.asyncio
async def test_unread_count(user_client):
    await _seed_notification(_user_id)
    await _seed_notification(_user_id)
    resp = await user_client.get("/notifications/unread-count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


@pytest.mark.asyncio
async def test_unread_only_filter(user_client):
    n1 = await _seed_notification(_user_id, title="read")
    async with TestSessionLocal() as session:
        await notification_service.mark_read(session, _user_id, n1)
    await _seed_notification(_user_id, title="unread")

    resp = await user_client.get("/notifications?unread_only=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["unread"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "unread"


@pytest.mark.asyncio
async def test_list_unauthenticated(no_auth_client):
    resp = await no_auth_client.get("/notifications")
    assert resp.status_code == 401


# --- Mark read ---


@pytest.mark.asyncio
async def test_mark_read(user_client):
    nid = await _seed_notification(_user_id)
    resp = await user_client.patch(f"/notifications/{nid}/read")
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None


@pytest.mark.asyncio
async def test_mark_read_foreign_notification_returns_404(user_client):
    nid = await _seed_notification(_other_user_id)
    resp = await user_client.patch(f"/notifications/{nid}/read")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mark_all_read(user_client):
    await _seed_notification(_user_id)
    await _seed_notification(_user_id)
    resp = await user_client.post("/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2}
    resp = await user_client.get("/notifications/unread-count")
    assert resp.json() == {"count": 0}


# --- Delete ---


@pytest.mark.asyncio
async def test_delete_own(user_client):
    nid = await _seed_notification(_user_id)
    resp = await user_client.delete(f"/notifications/{nid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_foreign_returns_404(user_client):
    nid = await _seed_notification(_other_user_id)
    resp = await user_client.delete(f"/notifications/{nid}")
    assert resp.status_code == 404


# --- Preferences proxy ---


@pytest.mark.asyncio
async def test_get_preferences_proxies_user_profile(user_client):
    upstream = {
        "user_id": str(_user_id),
        "saved_filters": {},
        "page_sizes": {},
        "extras": {"notifications": {"channels": {"in_app": False}, "events": {}}},
        "updated_at": "2026-04-20T00:00:00+00:00",
    }
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: upstream

    async def _get(*args, **kwargs):
        return mock_resp

    with patch("app.routers.notifications.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = _get
        resp = await user_client.get("/notifications/preferences")

    assert resp.status_code == 200
    data = resp.json()
    assert data["channels"]["in_app"] is False
    assert data["events"]["reservation.created"] is True


@pytest.mark.asyncio
async def test_put_preferences_writes_merged_payload(user_client):
    upstream_get = {
        "user_id": str(_user_id),
        "saved_filters": {},
        "page_sizes": {},
        "extras": {"notifications": {"channels": {"in_app": True}, "events": {}}},
        "updated_at": "2026-04-20T00:00:00+00:00",
    }
    get_resp = AsyncMock()
    get_resp.status_code = 200
    get_resp.json = lambda: upstream_get
    patch_resp = AsyncMock()
    patch_resp.status_code = 200
    patch_resp.json = lambda: {}

    captured = {}

    async def _get(*args, **kwargs):
        return get_resp

    async def _patch(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return patch_resp

    with patch("app.routers.notifications.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = _get
        inst.patch = _patch
        resp = await user_client.put(
            "/notifications/preferences",
            json={"events": {"reservation.completed": False}},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["events"]["reservation.completed"] is False
    assert data["events"]["reservation.created"] is True
    assert "/preferences" in captured["url"]
    notif = captured["json"]["extras"]["notifications"]
    assert notif["events"]["reservation.completed"] is False


# --- Health ---


@pytest.mark.asyncio
async def test_health(user_client):
    resp = await user_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "notifications"
