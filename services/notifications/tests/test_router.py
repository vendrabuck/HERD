import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.routers.notifications import (
    _caller_id,
    delete_notification,
    get_bearer_credentials,
    get_current_user_payload,
    get_unread_count,
    list_notifications,
    mark_all_read,
    mark_read,
)
from app.services import notification_service
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from jose import jwt
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


# --- Direct handler calls ---
#
# The ASGI-transport tests above exercise the routes end-to-end, but coverage's
# trace function does not always attribute lines run inside the AnyIO worker that
# Starlette dispatches the handler on. Calling the handler coroutines directly
# (with a real SQLite session) pins the branch coverage for the 404/return arms
# deterministically.


def _payload(user_id: uuid.UUID) -> dict:
    return {"sub": str(user_id), "role": "user"}


@pytest.mark.asyncio
async def test_mark_read_handler_returns_notification_and_404():
    nid = await _seed_notification(_user_id)
    async with TestSessionLocal() as session:
        result = await mark_read(nid, db=session, payload=_payload(_user_id))
    assert result.id == nid

    foreign = await _seed_notification(_other_user_id)
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            await mark_read(foreign, db=session, payload=_payload(_user_id))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_mark_all_read_handler_returns_count():
    await _seed_notification(_user_id)
    await _seed_notification(_user_id)
    async with TestSessionLocal() as session:
        result = await mark_all_read(db=session, payload=_payload(_user_id))
    assert result.updated == 2


@pytest.mark.asyncio
async def test_delete_handler_404_on_foreign():
    foreign = await _seed_notification(_other_user_id)
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            await delete_notification(foreign, db=session, payload=_payload(_user_id))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_unread_count_handler_returns_count():
    await _seed_notification(_user_id)
    await _seed_notification(_user_id)
    async with TestSessionLocal() as session:
        result = await get_unread_count(db=session, payload=_payload(_user_id))
    assert result.count == 2


@pytest.mark.asyncio
async def test_list_handler_returns_items():
    await _seed_notification(_user_id, title="hello")
    async with TestSessionLocal() as session:
        result = await list_notifications(
            limit=50, offset=0, unread_only=False, db=session, payload=_payload(_user_id)
        )
    assert result.total == 1
    assert result.items[0].title == "hello"


# --- get_bearer_credentials: real JWT validation ---


def _make_creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_get_bearer_credentials_accepts_valid_token():
    token = jwt.encode(
        {"sub": str(uuid.uuid4())}, settings.secret_key, algorithm=settings.algorithm
    )
    creds = _make_creds(token)
    out = get_bearer_credentials(credentials=creds)
    assert out.credentials == token


def test_get_bearer_credentials_rejects_garbage_token():
    with pytest.raises(HTTPException) as exc:
        get_bearer_credentials(credentials=_make_creds("not-a-jwt"))
    assert exc.value.status_code == 401


def test_get_bearer_credentials_rejects_token_without_sub():
    token = jwt.encode({"role": "user"}, settings.secret_key, algorithm=settings.algorithm)
    with pytest.raises(HTTPException) as exc:
        get_bearer_credentials(credentials=_make_creds(token))
    assert exc.value.status_code == 401


# --- _caller_id: invalid subject ---


def test_caller_id_rejects_non_uuid_subject():
    with pytest.raises(HTTPException) as exc:
        _caller_id({"sub": "not-a-uuid"})
    assert exc.value.status_code == 401


def test_caller_id_rejects_missing_subject():
    with pytest.raises(HTTPException) as exc:
        _caller_id({})
    assert exc.value.status_code == 401


# --- Preferences proxy: upstream error branches ---


@pytest.mark.asyncio
async def test_get_preferences_returns_502_on_transport_error(user_client):
    async def _get(*args, **kwargs):
        raise httpx.ConnectError("user-profile down")

    with patch("app.routers.notifications.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = _get
        resp = await user_client.get("/notifications/preferences")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_get_preferences_returns_502_on_upstream_4xx(user_client):
    bad = AsyncMock()
    bad.status_code = 404
    bad.json = lambda: {}

    async def _get(*args, **kwargs):
        return bad

    with patch("app.routers.notifications.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = _get
        resp = await user_client.get("/notifications/preferences")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_put_preferences_returns_502_on_transport_error(user_client):
    get_resp = AsyncMock()
    get_resp.status_code = 200
    get_resp.json = lambda: {
        "extras": {"notifications": {"channels": {"in_app": True}, "events": {}}}
    }

    async def _get(*args, **kwargs):
        return get_resp

    async def _patch(*args, **kwargs):
        raise httpx.ConnectError("user-profile down")

    with patch("app.routers.notifications.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = _get
        inst.patch = _patch
        resp = await user_client.put(
            "/notifications/preferences", json={"events": {"reservation.completed": False}}
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_put_preferences_returns_502_on_upstream_4xx(user_client):
    get_resp = AsyncMock()
    get_resp.status_code = 200
    get_resp.json = lambda: {
        "extras": {"notifications": {"channels": {"in_app": True}, "events": {}}}
    }
    patch_resp = AsyncMock()
    patch_resp.status_code = 500
    patch_resp.json = lambda: {}

    async def _get(*args, **kwargs):
        return get_resp

    async def _patch(*args, **kwargs):
        return patch_resp

    with patch("app.routers.notifications.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = _get
        inst.patch = _patch
        resp = await user_client.put(
            "/notifications/preferences", json={"events": {"reservation.completed": False}}
        )
    assert resp.status_code == 502
