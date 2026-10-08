"""The config service's CORS policy (issue #1111).

The config service keeps `allow_origins=["*"]` on purpose (it is the one service
that does not use herd_common's CORS helper), and it never allows credentials:
its session is a bearer token the page sends in the Authorization header, so a
credentialed cross-origin request has nothing to carry, and a wildcard origin
with credentials allowed would echo any origin back with
`Access-Control-Allow-Credentials: true`.
"""

import pytest
from app.main import app
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

_FOREIGN_ORIGIN = "https://elsewhere.example"


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _cors_options() -> dict:
    entries = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(entries) == 1, "the config app must register exactly one CORS middleware"
    return dict(entries[0].kwargs)


def test_cors_middleware_options_are_wildcard_without_credentials():
    options = _cors_options()
    assert options["allow_origins"] == ["*"]
    assert options.get("allow_credentials", False) is False
    assert options["allow_methods"] == ["*"]
    assert options["allow_headers"] == ["*"]


@pytest.mark.asyncio
async def test_preflight_from_any_origin_allows_no_credentials(async_client):
    resp = await async_client.options(
        "/settings",
        headers={
            "Origin": _FOREIGN_ORIGIN,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in resp.headers


@pytest.mark.asyncio
async def test_simple_request_with_cookie_does_not_echo_the_origin(async_client):
    # With credentials allowed, Starlette answers a request that carries a
    # cookie by echoing its Origin; without them the answer stays "*".
    resp = await async_client.get(
        "/status", headers={"Origin": _FOREIGN_ORIGIN, "Cookie": "session=x"}
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in resp.headers
