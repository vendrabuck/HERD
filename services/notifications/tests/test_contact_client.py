"""Unit tests for the ContactClient (auth /internal/users/{id}/contact)."""

import uuid

import httpx
import pytest
from app.services.contact_client import ContactClient


def _client(handler):
    transport = httpx.MockTransport(handler)
    c = ContactClient(base_url="http://auth", internal_token="tok", ttl_seconds=60)

    # Patch httpx.AsyncClient used inside _fetch to route through MockTransport.
    import app.services.contact_client as mod

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
