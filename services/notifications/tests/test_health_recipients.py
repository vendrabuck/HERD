"""Tests for app.services.health_recipients.

Covers:
- TTL cache hit/miss on the admin list
- HTTP failure / non-200 / malformed JSON return empty
- The deduped union of admins and active reservation holders
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services import health_recipients
from app.services.health_recipients import (
    AdminListClient,
    _fetch_active_reservation_holders,
    get_admin_client,
    resolve_health_recipients,
    set_admin_client,
)


def _mock_resp(*, status_code: int = 200, json_data=None, raise_value_error: bool = False):
    """Build a fake httpx Response-shaped mock."""
    resp = MagicMock()
    resp.status_code = status_code
    if raise_value_error:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_data if json_data is not None else []
    return resp


# --- AdminListClient ---


@pytest.mark.asyncio
async def test_admin_list_caches_within_ttl():
    a, b = uuid.uuid4(), uuid.uuid4()

    async def fake_get(*args, **kwargs):
        return _mock_resp(json_data=[str(a), str(b)])

    client = AdminListClient(
        base_url="http://auth-test:8000",
        internal_token="t",
        ttl_seconds=60,
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(side_effect=fake_get)
        first = await client.list_admins()
        second = await client.list_admins()
        assert set(first) == {a, b}
        assert second == first
        # Only one HTTP call thanks to the cache
        assert instance.request.call_count == 1


@pytest.mark.asyncio
async def test_admin_list_refetches_after_invalidate():
    a = uuid.uuid4()
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="t", ttl_seconds=60)

    async def fake_get(*args, **kwargs):
        return _mock_resp(json_data=[str(a)])

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(side_effect=fake_get)
        await client.list_admins()
        client.invalidate()
        await client.list_admins()
        assert instance.request.call_count == 2


@pytest.mark.asyncio
async def test_admin_list_returns_empty_when_token_missing():
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="", ttl_seconds=60)
    result = await client.list_admins()
    assert result == []


@pytest.mark.asyncio
async def test_admin_list_returns_empty_on_http_error():
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="t", ttl_seconds=60)
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(side_effect=httpx.ConnectError("nope"))
        result = await client.list_admins()
    assert result == []


@pytest.mark.asyncio
async def test_admin_list_returns_empty_on_non_200():
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="t", ttl_seconds=60)
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(return_value=_mock_resp(status_code=500))
        result = await client.list_admins()
    assert result == []


@pytest.mark.asyncio
async def test_admin_list_skips_unparseable_ids():
    a = uuid.uuid4()
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="t", ttl_seconds=60)
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(
            return_value=_mock_resp(json_data=[str(a), "not-a-uuid", None, 7])
        )
        result = await client.list_admins()
    assert result == [a]


@pytest.mark.asyncio
async def test_admin_list_concurrent_callers_fetch_once():
    """Two callers that both miss the cache serialize on the lock; the second
    sees the cache populated inside the lock and returns it without re-fetching."""
    a = uuid.uuid4()
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="t", ttl_seconds=60)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(*args, **kwargs):
        started.set()
        await release.wait()
        return _mock_resp(json_data=[str(a)])

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(side_effect=slow_get)

        first = asyncio.create_task(client.list_admins())
        # Wait until the first caller is inside the (gated) HTTP fetch, holding
        # the lock, so the second caller is forced to block on the lock and
        # subsequently take the in-lock cache-hit branch.
        await started.wait()
        second = asyncio.create_task(client.list_admins())
        await asyncio.sleep(0)  # let the second task block on the lock
        release.set()
        r1, r2 = await asyncio.gather(first, second)

    assert r1 == [a]
    assert r2 == [a]
    assert instance.request.call_count == 1


@pytest.mark.asyncio
async def test_admin_list_returns_empty_on_malformed_json():
    client = AdminListClient(base_url="http://auth-test:8000", internal_token="t", ttl_seconds=60)
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.request = AsyncMock(return_value=_mock_resp(raise_value_error=True))
        result = await client.list_admins()
    assert result == []


def test_get_admin_client_is_lazy_singleton():
    """First call constructs the client; subsequent calls reuse it."""
    set_admin_client(None)
    try:
        first = get_admin_client()
        second = get_admin_client()
        assert isinstance(first, AdminListClient)
        assert first is second
    finally:
        set_admin_client(None)


# --- _fetch_active_reservation_holders ---


@pytest.mark.asyncio
async def test_holders_returns_empty_when_token_missing():
    with patch.object(health_recipients.settings, "internal_api_token", ""):
        result = await _fetch_active_reservation_holders(uuid.uuid4())
    assert result == []


@pytest.mark.asyncio
async def test_holders_returns_parsed_user_ids_on_200():
    a, b = uuid.uuid4(), uuid.uuid4()
    device_id = uuid.uuid4()

    async def fake_get(url, **kwargs):
        # The device_id is forwarded as a query param and the internal token set.
        assert kwargs["params"] == {"device_id": str(device_id)}
        assert kwargs["headers"]["X-Internal-Token"] == "tok"
        return _mock_resp(json_data=[str(a), str(b)])

    with patch.object(health_recipients.settings, "internal_api_token", "tok"):
        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(side_effect=fake_get)
            result = await _fetch_active_reservation_holders(device_id)
    assert set(result) == {a, b}


@pytest.mark.asyncio
async def test_holders_returns_empty_on_http_error():
    with patch.object(health_recipients.settings, "internal_api_token", "tok"):
        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(side_effect=httpx.ConnectError("reservations down"))
            result = await _fetch_active_reservation_holders(uuid.uuid4())
    assert result == []


@pytest.mark.asyncio
async def test_holders_returns_empty_on_non_200():
    with patch.object(health_recipients.settings, "internal_api_token", "tok"):
        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(return_value=_mock_resp(status_code=503))
            result = await _fetch_active_reservation_holders(uuid.uuid4())
    assert result == []


@pytest.mark.asyncio
async def test_holders_returns_empty_on_malformed_json():
    with patch.object(health_recipients.settings, "internal_api_token", "tok"):
        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(return_value=_mock_resp(raise_value_error=True))
            result = await _fetch_active_reservation_holders(uuid.uuid4())
    assert result == []


@pytest.mark.asyncio
async def test_holders_skips_unparseable_ids():
    good = uuid.uuid4()
    with patch.object(health_recipients.settings, "internal_api_token", "tok"):
        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(
                return_value=_mock_resp(json_data=[str(good), "nope", None, 5])
            )
            result = await _fetch_active_reservation_holders(uuid.uuid4())
    assert result == [good]


# --- resolve_health_recipients (dedup union) ---


class _FakeAdminClient:
    def __init__(self, ids):
        self.ids = ids

    async def list_admins(self):
        return list(self.ids)


@pytest.mark.asyncio
async def test_resolver_returns_deduped_union():
    """Admin who also holds an active reservation appears exactly once."""
    admin = uuid.uuid4()
    holder_unique = uuid.uuid4()
    overlap = uuid.uuid4()  # both admin AND reservation holder

    set_admin_client(_FakeAdminClient([admin, overlap]))

    async def fake_holders(_did):
        return [holder_unique, overlap]

    with patch.object(health_recipients, "_fetch_active_reservation_holders", new=fake_holders):
        out = await resolve_health_recipients(uuid.uuid4())

    set_admin_client(None)

    assert set(out) == {admin, holder_unique, overlap}
    assert len(out) == 3  # overlap appears once, not twice


@pytest.mark.asyncio
async def test_resolver_returns_empty_when_both_sides_fail():
    set_admin_client(_FakeAdminClient([]))

    async def fake_holders(_did):
        return []

    with patch.object(health_recipients, "_fetch_active_reservation_holders", new=fake_holders):
        out = await resolve_health_recipients(uuid.uuid4())

    set_admin_client(None)
    assert out == []


@pytest.mark.asyncio
async def test_resolver_admin_only_when_no_holders():
    admin = uuid.uuid4()
    set_admin_client(_FakeAdminClient([admin]))

    async def fake_holders(_did):
        return []

    with patch.object(health_recipients, "_fetch_active_reservation_holders", new=fake_holders):
        out = await resolve_health_recipients(uuid.uuid4())

    set_admin_client(None)
    assert out == [admin]
