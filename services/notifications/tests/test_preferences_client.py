import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.services.preferences_client import PreferencesClient


def _make_upstream(stored_prefs: dict | None):
    return {
        "user_id": str(uuid.uuid4()),
        "saved_filters": {},
        "page_sizes": {},
        "extras": ({"notifications": stored_prefs} if stored_prefs is not None else {}),
        "updated_at": "2026-04-20T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_get_returns_defaults_when_no_stored_prefs():
    client = PreferencesClient(ttl_seconds=60)
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: _make_upstream(None)

    with patch("app.services.preferences_client.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = AsyncMock(return_value=resp)
        prefs = await client.get(uuid.uuid4())

    assert prefs.channel_enabled("in_app") is True
    assert prefs.event_enabled("reservation.created") is True


@pytest.mark.asyncio
async def test_get_returns_stored_prefs():
    user_id = uuid.uuid4()
    client = PreferencesClient(ttl_seconds=60)
    stored = {"channels": {"in_app": False}, "events": {"reservation.completed": False}}
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: _make_upstream(stored)

    with patch("app.services.preferences_client.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = AsyncMock(return_value=resp)
        prefs = await client.get(user_id)

    assert prefs.channel_enabled("in_app") is False
    assert prefs.event_enabled("reservation.completed") is False
    assert prefs.event_enabled("reservation.created") is True


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_http_call():
    user_id = uuid.uuid4()
    client = PreferencesClient(ttl_seconds=60)
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: _make_upstream(None)
    get_mock = AsyncMock(return_value=resp)

    with patch("app.services.preferences_client.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = get_mock
        await client.get(user_id)
        await client.get(user_id)

    assert get_mock.await_count == 1


@pytest.mark.asyncio
async def test_invalidate_forces_refetch():
    user_id = uuid.uuid4()
    client = PreferencesClient(ttl_seconds=60)
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: _make_upstream(None)
    get_mock = AsyncMock(return_value=resp)

    with patch("app.services.preferences_client.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = get_mock
        await client.get(user_id)
        client.invalidate(user_id)
        await client.get(user_id)

    assert get_mock.await_count == 2


@pytest.mark.asyncio
async def test_fetch_falls_back_on_http_error():
    client = PreferencesClient(ttl_seconds=60)
    import httpx

    with patch("app.services.preferences_client.httpx.AsyncClient") as MockClient:
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        prefs = await client.get(uuid.uuid4())

    assert prefs.channel_enabled("in_app") is True
    assert prefs.event_enabled("reservation.created") is True
