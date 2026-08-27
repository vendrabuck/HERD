"""Unit tests for the ContactClient (auth /internal/users/{id}/contact)."""

import asyncio
import uuid

import httpx
import pytest
from app.services.contact_client import (
    ContactClient,
    get_contact_client,
    set_contact_client,
)


def _client(handler):
    transport = httpx.MockTransport(handler)
    c = ContactClient(base_url="http://auth", internal_token="tok", ttl_seconds=60)

    # Patch httpx.AsyncClient used inside herd_common.internal_client.call_service
    # (the shared transport _fetch now delegates to) to route through MockTransport.
    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    mod.httpx.AsyncClient = _factory
    c._restore = lambda: setattr(mod.httpx, "AsyncClient", orig)
    return c


@pytest.mark.asyncio
async def test_returns_contact_on_200():
    uid = uuid.uuid4()

    def handler(request):
        assert request.headers["X-Internal-Token"] == "tok"
        return httpx.Response(
            200, json={"user_id": str(uid), "email": "a@b.com", "username": "alice"}
        )

    c = _client(handler)
    contact = await c.get(uid)
    c._restore()
    assert contact is not None
    assert contact.email == "a@b.com"
    assert contact.username == "alice"


@pytest.mark.asyncio
async def test_returns_none_on_404():
    uid = uuid.uuid4()
    c = _client(lambda req: httpx.Response(404, json={"detail": "User not found"}))
    contact = await c.get(uid)
    c._restore()
    assert contact is None


@pytest.mark.asyncio
async def test_returns_none_on_transport_error():
    uid = uuid.uuid4()

    def handler(request):
        raise httpx.ConnectError("auth down")

    c = _client(handler)
    contact = await c.get(uid)
    c._restore()
    assert contact is None


@pytest.mark.asyncio
async def test_caches_within_ttl():
    uid = uuid.uuid4()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200, json={"user_id": str(uid), "email": "a@b.com", "username": "alice"}
        )

    c = _client(handler)
    await c.get(uid)
    await c.get(uid)
    c._restore()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_no_token_returns_none():
    c = ContactClient(base_url="http://auth", internal_token="", ttl_seconds=60)
    assert await c.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_returns_none_on_malformed_json():
    uid = uuid.uuid4()
    # 200 but body is missing required keys: UserContact construction raises KeyError.
    c = _client(lambda req: httpx.Response(200, json={"unexpected": "shape"}))
    contact = await c.get(uid)
    c._restore()
    assert contact is None


@pytest.mark.asyncio
async def test_cache_expires_after_ttl():
    """A zero TTL means every entry is already expired, forcing a re-fetch."""
    uid = uuid.uuid4()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200, json={"user_id": str(uid), "email": "a@b.com", "username": "alice"}
        )

    c = _client(handler)
    c._cache._ttl = 0  # entry expires immediately -> _cache_hit returns (False, None)
    await c.get(uid)
    await c.get(uid)
    c._restore()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_invalidate_drops_cache_entry():
    uid = uuid.uuid4()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200, json={"user_id": str(uid), "email": "a@b.com", "username": "alice"}
        )

    c = _client(handler)
    await c.get(uid)
    c.invalidate(uid)
    await c.get(uid)
    c._restore()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_concurrent_callers_fetch_once():
    """Two cache-missing callers serialize on the lock; the second takes the
    in-lock cache-hit branch instead of issuing a second fetch."""
    uid = uuid.uuid4()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_handler(request):
        calls["n"] += 1
        started.set()
        await release.wait()
        return httpx.Response(
            200, json={"user_id": str(uid), "email": "a@b.com", "username": "alice"}
        )

    c = _client(slow_handler)
    first = asyncio.create_task(c.get(uid))
    await started.wait()
    second = asyncio.create_task(c.get(uid))
    await asyncio.sleep(0)
    release.set()
    r1, r2 = await asyncio.gather(first, second)
    c._restore()
    assert calls["n"] == 1
    assert r1 is not None and r1.email == "a@b.com"
    assert r2 is not None and r2.email == "a@b.com"


def test_get_contact_client_is_lazy_singleton():
    set_contact_client(None)
    try:
        first = get_contact_client()
        second = get_contact_client()
        assert isinstance(first, ContactClient)
        assert first is second
    finally:
        set_contact_client(None)
