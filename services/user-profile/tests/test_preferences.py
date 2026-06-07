import uuid

import pytest
from app.database import Base, get_db
from app.main import app
from app.routers.preferences import get_current_user_payload
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


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _payload_for(_user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def other_user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _payload_for(_other_user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def no_auth_client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- GET ---


@pytest.mark.asyncio
async def test_get_preferences_auto_creates_empty(user_client):
    resp = await user_client.get("/preferences")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == str(_user_id)
    assert data["saved_filters"] == {}
    assert data["page_sizes"] == {}
    assert data["extras"] == {}
    assert "updated_at" in data


@pytest.mark.asyncio
async def test_get_preferences_idempotent(user_client):
    first = await user_client.get("/preferences")
    second = await user_client.get("/preferences")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]


@pytest.mark.asyncio
async def test_get_preferences_unauthenticated(no_auth_client):
    resp = await no_auth_client.get("/preferences")
    assert resp.status_code == 401


# --- PUT (replace) ---


@pytest.mark.asyncio
async def test_put_preferences_replaces_all(user_client):
    body = {
        "saved_filters": {"inventory": {"search": "router"}},
        "page_sizes": {"inventory": 25},
        "extras": {"theme": "dark"},
    }
    resp = await user_client.put("/preferences", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {"inventory": {"search": "router"}}
    assert data["page_sizes"] == {"inventory": 25}
    assert data["extras"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_put_preferences_clobbers_existing(user_client):
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {"inventory": {"search": "a"}},
            "page_sizes": {"inventory": 25},
            "extras": {"theme": "dark"},
        },
    )
    resp = await user_client.put(
        "/preferences",
        json={"saved_filters": {}, "page_sizes": {}, "extras": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {}
    assert data["page_sizes"] == {}
    assert data["extras"] == {}


@pytest.mark.asyncio
async def test_put_preferences_unauthenticated(no_auth_client):
    resp = await no_auth_client.put(
        "/preferences",
        json={"saved_filters": {}, "page_sizes": {}, "extras": {}},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_preferences_defaults(user_client):
    resp = await user_client.put("/preferences", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {}
    assert data["page_sizes"] == {}
    assert data["extras"] == {}


# --- Validation bounds ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_page_sizes",
    [
        {"inventory": 0},  # non-positive
        {"inventory": -1},  # negative
        {"inventory": 10_000},  # over the max
        {"inventory": "25"},  # not an int
        {"inventory": True},  # bool is not a page size
    ],
)
async def test_put_preferences_rejects_invalid_page_sizes(user_client, bad_page_sizes):
    resp = await user_client.put("/preferences", json={"page_sizes": bad_page_sizes})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_preferences_accepts_valid_page_size(user_client):
    resp = await user_client.put("/preferences", json={"page_sizes": {"inventory": 500}})
    assert resp.status_code == 200
    assert resp.json()["page_sizes"] == {"inventory": 500}


@pytest.mark.asyncio
async def test_put_preferences_rejects_oversized_blob(user_client):
    # A single saved_filters value larger than the 64 KB serialized cap is rejected.
    huge = {"inventory": {"search": "x" * 70_000}}
    resp = await user_client.put("/preferences", json={"saved_filters": huge})
    assert resp.status_code == 422


# --- PATCH (merge) ---


@pytest.mark.asyncio
async def test_patch_merges_page_sizes_without_clobbering_filters(user_client):
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {"inventory": {"search": "r"}},
            "page_sizes": {"inventory": 25},
            "extras": {},
        },
    )
    resp = await user_client.patch(
        "/preferences",
        json={"page_sizes": {"templates": 100}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {"inventory": {"search": "r"}}
    assert data["page_sizes"] == {"inventory": 25, "templates": 100}


@pytest.mark.asyncio
async def test_patch_shallow_merge_overwrites_same_key(user_client):
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {},
            "page_sizes": {"inventory": 25},
            "extras": {},
        },
    )
    resp = await user_client.patch(
        "/preferences",
        json={"page_sizes": {"inventory": 100}},
    )
    assert resp.status_code == 200
    assert resp.json()["page_sizes"] == {"inventory": 100}


@pytest.mark.asyncio
async def test_patch_empty_body_is_noop(user_client):
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {"a": 1},
            "page_sizes": {"b": 2},
            "extras": {"c": 3},
        },
    )
    resp = await user_client.patch("/preferences", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {"a": 1}
    assert data["page_sizes"] == {"b": 2}
    assert data["extras"] == {"c": 3}


@pytest.mark.asyncio
async def test_patch_auto_creates_row(user_client):
    resp = await user_client.patch(
        "/preferences",
        json={"extras": {"theme": "dark"}},
    )
    assert resp.status_code == 200
    assert resp.json()["extras"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_patch_unauthenticated(no_auth_client):
    resp = await no_auth_client.patch("/preferences", json={})
    assert resp.status_code == 401


# --- DELETE (reset) ---


@pytest.mark.asyncio
async def test_delete_resets_to_defaults(user_client):
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {"inventory": {"search": "x"}},
            "page_sizes": {"inventory": 25},
            "extras": {"theme": "dark"},
        },
    )
    resp = await user_client.delete("/preferences")
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {}
    assert data["page_sizes"] == {}
    assert data["extras"] == {}


@pytest.mark.asyncio
async def test_delete_without_existing_row(user_client):
    resp = await user_client.delete("/preferences")
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {}


@pytest.mark.asyncio
async def test_delete_unauthenticated(no_auth_client):
    resp = await no_auth_client.delete("/preferences")
    assert resp.status_code == 401


# --- Per-user isolation ---


@pytest.mark.asyncio
async def test_user_isolation():
    # user1 writes
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _payload_for(_user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c1:
        await c1.put(
            "/preferences",
            json={
                "saved_filters": {"inventory": {"search": "user1"}},
                "page_sizes": {},
                "extras": {},
            },
        )
    # user2 reads: should be empty, not user1's data
    app.dependency_overrides[get_current_user_payload] = _payload_for(_other_user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        resp = await c2.get("/preferences")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == str(_other_user_id)
        assert data["saved_filters"] == {}
    app.dependency_overrides.clear()


# --- Internal endpoint (service-to-service) ---


@pytest.mark.asyncio
async def test_internal_endpoint_requires_token(no_auth_client):
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": str(_user_id)},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_endpoint_rejects_wrong_token(no_auth_client):
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": str(_user_id)},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_endpoint_returns_prefs_and_auto_creates(no_auth_client):
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": str(_user_id)},
        headers={"X-Internal-Token": "test-internal-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == str(_user_id)
    assert data["saved_filters"] == {}
    assert data["extras"] == {}


@pytest.mark.asyncio
async def test_internal_endpoint_returns_stored_extras(user_client, no_auth_client):
    await user_client.patch(
        "/preferences",
        json={"extras": {"notifications": {"channels": {"in_app": False}}}},
    )
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": str(_user_id)},
        headers={"X-Internal-Token": "test-internal-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["extras"] == {"notifications": {"channels": {"in_app": False}}}


# --- Health ---


@pytest.mark.asyncio
async def test_health(user_client):
    resp = await user_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "user-profile"
