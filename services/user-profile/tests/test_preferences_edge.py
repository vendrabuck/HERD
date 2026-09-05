"""Additional preferences-router and service tests covering edge shapes
(deeply nested extras, partial-PATCH semantics, malformed token subject,
internal endpoint header handling)."""

import uuid

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.preferences import UserPreferences
from app.routers.preferences import get_current_user_payload
from app.services import preferences_service
from app.services.preferences_service import get_or_create, replace, reset
from app.services.preferences_service import patch as svc_patch
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


def _payload():
    return {"sub": str(_user_id), "role": "user"}


def _bad_sub_payload():
    return {"sub": "not-a-uuid", "role": "user"}


def _missing_sub_payload():
    return {"role": "user"}


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _payload
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def bad_sub_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _bad_sub_payload
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def missing_sub_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = _missing_sub_payload
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def no_auth_client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Router: bad token subject ---


@pytest.mark.asyncio
async def test_get_preferences_invalid_uuid_in_sub_returns_401(bad_sub_client):
    resp = await bad_sub_client.get("/preferences")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_preferences_missing_sub_returns_401(missing_sub_client):
    resp = await missing_sub_client.put(
        "/preferences",
        json={"saved_filters": {}, "page_sizes": {}, "extras": {}},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_preferences_invalid_sub_returns_401(bad_sub_client):
    resp = await bad_sub_client.patch("/preferences", json={"extras": {"a": 1}})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_preferences_invalid_sub_returns_401(bad_sub_client):
    resp = await bad_sub_client.delete("/preferences")
    assert resp.status_code == 401


# --- Router: deep nesting and exotic JSON ---


@pytest.mark.asyncio
async def test_put_round_trip_with_deeply_nested_extras(user_client):
    body = {
        "saved_filters": {},
        "page_sizes": {},
        "extras": {
            "notifications": {
                "channels": {"in_app": True, "email": False},
                "events": {"reservation_started": {"channels": ["in_app"]}},
            },
            "theme": "dark",
        },
    }
    put_resp = await user_client.put("/preferences", json=body)
    assert put_resp.status_code == 200
    get_resp = await user_client.get("/preferences")
    assert get_resp.status_code == 200
    assert get_resp.json()["extras"] == body["extras"]


@pytest.mark.asyncio
async def test_patch_partial_updates_only_named_keys(user_client):
    """PATCH with extras only must not clear saved_filters or page_sizes that
    were set in a prior PUT."""
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {"inventory": {"q": "router"}},
            "page_sizes": {"inventory": 50},
            "extras": {"theme": "light"},
        },
    )
    resp = await user_client.patch("/preferences", json={"extras": {"theme": "dark"}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {"inventory": {"q": "router"}}
    assert data["page_sizes"] == {"inventory": 50}
    assert data["extras"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_patch_explicit_null_does_not_clear(user_client):
    """An explicit null in PATCH means 'no change'; the merge skips None."""
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {"inventory": {"q": "x"}},
            "page_sizes": {},
            "extras": {},
        },
    )
    resp = await user_client.patch(
        "/preferences",
        json={"saved_filters": None, "extras": {"theme": "dark"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved_filters"] == {"inventory": {"q": "x"}}
    assert data["extras"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_patch_empty_dict_for_key_replaces_with_empty(user_client):
    """A PATCH with extras={} updates extras with no keys; the merge is a
    shallow dict.update so it leaves existing extras intact."""
    await user_client.put(
        "/preferences",
        json={
            "saved_filters": {},
            "page_sizes": {},
            "extras": {"theme": "light"},
        },
    )
    resp = await user_client.patch("/preferences", json={"extras": {}})
    assert resp.status_code == 200
    # extras={} is a merge of nothing into existing dict, so existing keys persist.
    assert resp.json()["extras"] == {"theme": "light"}


@pytest.mark.asyncio
async def test_put_unknown_field_is_ignored(user_client):
    """Pydantic strips unknown fields by default; the request should succeed."""
    resp = await user_client.put(
        "/preferences",
        json={
            "saved_filters": {},
            "page_sizes": {},
            "extras": {},
            "unknown_field": "should be ignored",
        },
    )
    assert resp.status_code == 200


# --- Router: malformed payloads ---


@pytest.mark.asyncio
async def test_put_with_non_dict_saved_filters_returns_422(user_client):
    resp = await user_client.put(
        "/preferences",
        json={"saved_filters": "not-a-dict", "page_sizes": {}, "extras": {}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_with_non_dict_page_sizes_returns_422(user_client):
    resp = await user_client.patch("/preferences", json={"page_sizes": [1, 2, 3]})
    assert resp.status_code == 422


# --- Internal endpoint: header handling ---


@pytest.mark.asyncio
async def test_internal_endpoint_missing_user_id_returns_422(no_auth_client):
    resp = await no_auth_client.get(
        "/preferences/internal",
        headers={"X-Internal-Token": "test-internal-token"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_endpoint_invalid_user_id_returns_422(no_auth_client):
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": "not-a-uuid"},
        headers={"X-Internal-Token": "test-internal-token"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_endpoint_blank_token_value_rejected(no_auth_client):
    """Empty token header value is not the same as missing; both must 401."""
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": str(_user_id)},
        headers={"X-Internal-Token": ""},
    )
    assert resp.status_code == 401


# --- Service-layer tests (bypass router) ---


@pytest.mark.asyncio
async def test_get_or_create_returns_same_row_on_subsequent_calls():
    user_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        first = await get_or_create(session, user_id)
        first_updated_at = first.updated_at
        second = await get_or_create(session, user_id)
        assert second.user_id == first.user_id == user_id
        # A no-op re-read must not have replaced the row (updated_at unchanged).
        assert second.updated_at == first_updated_at
        # And a third read still resolves to the same row.
        third = await get_or_create(session, user_id)
        assert third.user_id == user_id


@pytest.mark.asyncio
async def test_service_patch_creates_row_when_missing():
    """patch() goes through get_or_create internally; a brand-new user gets
    a row created with their PATCHed extras."""
    user_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        prefs = await svc_patch(
            session,
            user_id,
            saved_filters=None,
            page_sizes=None,
            extras={"theme": "dark"},
        )
    assert prefs.extras == {"theme": "dark"}
    assert prefs.saved_filters == {}
    assert prefs.page_sizes == {}


@pytest.mark.asyncio
async def test_service_replace_overrides_all_fields():
    user_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        await svc_patch(
            session,
            user_id,
            saved_filters={"a": 1},
            page_sizes={"b": 2},
            extras={"c": 3},
        )
        new = await replace(
            session,
            user_id,
            saved_filters={"x": 1},
            page_sizes={},
            extras={},
        )
    assert new.saved_filters == {"x": 1}
    assert new.page_sizes == {}
    assert new.extras == {}


@pytest.mark.asyncio
async def test_service_reset_clears_existing_row():
    user_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        await replace(
            session,
            user_id,
            saved_filters={"q": "x"},
            page_sizes={"q": 25},
            extras={"theme": "dark"},
        )
        cleared = await reset(session, user_id)
    assert cleared.saved_filters == {}
    assert cleared.page_sizes == {}
    assert cleared.extras == {}


# --- Concurrent first-access: idempotent create (regression for #81) ---


@pytest.mark.asyncio
async def test_get_or_create_returns_existing_committed_row_without_raising():
    """When a row already exists (committed by another writer), get_or_create
    takes the read-existing path and returns it rather than inserting again."""
    user_id = uuid.uuid4()
    # Pre-insert and commit the row, simulating the winner of the race.
    async with TestSessionLocal() as writer:
        writer.add(
            UserPreferences(
                user_id=user_id,
                saved_filters={"who": "winner"},
                page_sizes={},
                extras={},
            )
        )
        await writer.commit()
    async with TestSessionLocal() as session:
        prefs = await get_or_create(session, user_id)
    assert prefs.user_id == user_id
    assert prefs.saved_filters == {"who": "winner"}


@pytest.mark.asyncio
async def test_get_or_create_recovers_from_lost_insert_race(monkeypatch):
    """Force the lost-race window: the initial SELECT returns None (so the
    caller tries to INSERT) but a committed row already exists, so the commit
    raises IntegrityError. get_or_create must roll back, re-read, and return the
    existing row instead of propagating a 500."""
    user_id = uuid.uuid4()
    # The winner commits its row first.
    async with TestSessionLocal() as writer:
        writer.add(
            UserPreferences(
                user_id=user_id,
                saved_filters={"who": "winner"},
                page_sizes={},
                extras={},
            )
        )
        await writer.commit()

    real_select = preferences_service._select_prefs
    calls = {"n": 0}

    async def fake_select(db, uid):
        # First lookup pretends the row is absent (the lost-race read), so the
        # caller proceeds to INSERT and hits the PK conflict. Later lookups
        # (the post-rollback re-read) use the real query.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_select(db, uid)

    monkeypatch.setattr(preferences_service, "_select_prefs", fake_select)

    async with TestSessionLocal() as session:
        prefs = await get_or_create(session, user_id)
    assert prefs.user_id == user_id
    assert prefs.saved_filters == {"who": "winner"}
    # The except path must have run: initial None read plus the re-read.
    assert calls["n"] >= 2


# --- Cross-user isolation in the internal endpoint ---


@pytest.mark.asyncio
async def test_internal_endpoint_isolates_users(user_client, no_auth_client):
    """The internal endpoint must return only the requested user's row, even
    when other users have differing data."""
    other_user = uuid.uuid4()
    await user_client.patch("/preferences", json={"extras": {"who": "me"}})
    # Internal lookup for the other user must auto-create with empty extras.
    resp = await no_auth_client.get(
        "/preferences/internal",
        params={"user_id": str(other_user)},
        headers={"X-Internal-Token": "test-internal-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == str(other_user)
    assert data["extras"] == {}


# --- PATCH validates the MERGED result, not just the request (issue #714) ---


@pytest.mark.asyncio
async def test_patch_past_blob_cap_returns_422_and_leaves_row_unchanged(user_client):
    """Two PATCHes each under the 64 KB cap must not combine into a row over
    it. The second PATCH is refused with 422 and the stored row is exactly
    what the first PATCH left."""
    big = "x" * 40_000  # 40 KB serialized, under the 64 KB per-field cap
    first = await user_client.patch("/preferences", json={"saved_filters": {"a": big}})
    assert first.status_code == 200
    second = await user_client.patch("/preferences", json={"saved_filters": {"b": big}})
    assert second.status_code == 422
    assert second.json()["detail"] == (
        "merged saved_filters is too large (max 65536 bytes serialized)"
    )
    stored = (await user_client.get("/preferences")).json()
    assert stored["saved_filters"] == {"a": big}


@pytest.mark.asyncio
async def test_patch_merge_past_key_cap_returns_422(user_client):
    """150 stored keys plus 100 fresh keys would be 250 against a cap of 200."""
    base = {f"k{i}": i for i in range(150)}
    first = await user_client.put(
        "/preferences", json={"saved_filters": base, "page_sizes": {}, "extras": {}}
    )
    assert first.status_code == 200
    fresh = {f"n{i}": i for i in range(100)}
    resp = await user_client.patch("/preferences", json={"saved_filters": fresh})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "merged saved_filters has too many keys (max 200)"
    stored = (await user_client.get("/preferences")).json()
    assert stored["saved_filters"] == base


@pytest.mark.asyncio
async def test_patch_merge_past_page_sizes_key_cap_returns_422(user_client):
    """page_sizes goes through its own validator; the merged result is capped too."""
    base = {f"p{i}": 25 for i in range(150)}
    await user_client.put(
        "/preferences", json={"saved_filters": {}, "page_sizes": base, "extras": {}}
    )
    fresh = {f"q{i}": 25 for i in range(100)}
    resp = await user_client.patch("/preferences", json={"page_sizes": fresh})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "merged page_sizes has too many keys (max 200)"
    stored = (await user_client.get("/preferences")).json()
    assert stored["page_sizes"] == base


@pytest.mark.asyncio
async def test_patch_rejection_leaves_other_fields_untouched(user_client):
    """A single PATCH carrying a valid extras merge AND an over-cap saved_filters
    merge is rejected as a whole: no field is partially applied."""
    big = "x" * 40_000
    await user_client.put(
        "/preferences",
        json={"saved_filters": {"a": big}, "page_sizes": {}, "extras": {"theme": "light"}},
    )
    resp = await user_client.patch(
        "/preferences",
        json={"saved_filters": {"b": big}, "extras": {"theme": "dark"}},
    )
    assert resp.status_code == 422
    stored = (await user_client.get("/preferences")).json()
    assert stored["saved_filters"] == {"a": big}
    assert stored["extras"] == {"theme": "light"}


@pytest.mark.asyncio
async def test_patch_overwriting_existing_keys_stays_within_cap(user_client):
    """Overwriting a key does not grow the key count, so a merge that lands
    exactly at the cap is accepted."""
    base = {f"k{i}": i for i in range(200)}
    await user_client.put(
        "/preferences", json={"saved_filters": base, "page_sizes": {}, "extras": {}}
    )
    resp = await user_client.patch("/preferences", json={"saved_filters": {"k0": "changed"}})
    assert resp.status_code == 200
    assert resp.json()["saved_filters"]["k0"] == "changed"
    assert len(resp.json()["saved_filters"]) == 200
