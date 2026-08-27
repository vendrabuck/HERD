"""Tests for herd_common.internal_client.

Covers the failure modes rather than just the happy path: transport error
propagation, non-2xx passthrough, missing internal token, and that each auth
variant sets the header it claims to.
"""

import httpx
import pytest
from herd_common.internal_client import ForwardedAuth, InternalTokenAuth, call_service


@pytest.mark.asyncio
async def test_internal_token_auth_sets_x_internal_token_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def _fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(*args, **kwargs)

    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    try:
        resp = await call_service(
            "http://svc",
            "GET",
            "/internal/thing",
            timeout=5.0,
            auth=InternalTokenAuth(token="tok"),
        )
    finally:
        mod.httpx.AsyncClient = orig

    assert resp.status_code == 200
    assert seen["headers"]["x-internal-token"] == "tok"
    assert "authorization" not in seen["headers"]


@pytest.mark.asyncio
async def test_forwarded_auth_sets_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    try:
        resp = await call_service(
            "http://svc",
            "GET",
            "/groups/user/abc",
            timeout=5.0,
            auth=ForwardedAuth(authorization="Bearer jwt-here"),
        )
    finally:
        mod.httpx.AsyncClient = orig

    assert resp.status_code == 200
    assert seen["headers"]["authorization"] == "Bearer jwt-here"
    assert "x-internal-token" not in seen["headers"]


@pytest.mark.asyncio
async def test_missing_internal_token_raises_runtime_error_with_caller_message():
    with pytest.raises(RuntimeError, match="cannot reach cabling forks"):
        await call_service(
            "http://cabling",
            "GET",
            "/internal/forks",
            timeout=10.0,
            auth=InternalTokenAuth(
                token="",
                missing_token_message=(
                    "internal_api_token not configured; cannot reach cabling forks"
                ),
            ),
        )


@pytest.mark.asyncio
async def test_none_internal_token_raises_runtime_error():
    with pytest.raises(RuntimeError):
        await call_service(
            "http://cabling",
            "GET",
            "/internal/forks",
            timeout=10.0,
            auth=InternalTokenAuth(token=None),
        )


@pytest.mark.asyncio
async def test_transport_error_propagates_as_httpx_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)

    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    try:
        with pytest.raises(httpx.HTTPError):
            await call_service(
                "http://svc",
                "GET",
                "/internal/thing",
                timeout=5.0,
                auth=InternalTokenAuth(token="tok"),
            )
    finally:
        mod.httpx.AsyncClient = orig


@pytest.mark.asyncio
async def test_non_2xx_response_returned_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    transport = httpx.MockTransport(handler)

    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    try:
        resp = await call_service(
            "http://svc",
            "GET",
            "/internal/thing",
            timeout=5.0,
            auth=InternalTokenAuth(token="tok"),
        )
    finally:
        mod.httpx.AsyncClient = orig

    assert resp.status_code == 503
    assert resp.text == "unavailable"


@pytest.mark.asyncio
async def test_json_body_and_method_forwarded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(201, json={"created": True})

    transport = httpx.MockTransport(handler)

    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    try:
        resp = await call_service(
            "http://cabling",
            "PUT",
            "/internal/forks/abc",
            json_body={"a": 1},
            timeout=10.0,
            auth=InternalTokenAuth(token="tok"),
        )
    finally:
        mod.httpx.AsyncClient = orig

    assert resp.status_code == 201
    assert seen["method"] == "PUT"
    assert seen["url"] == "http://cabling/internal/forks/abc"
    assert seen["body"] == b'{"a":1}'


@pytest.mark.asyncio
async def test_params_become_query_string():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    import herd_common.internal_client as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    try:
        resp = await call_service(
            "http://user-profile",
            "GET",
            "/preferences/internal",
            params={"user_id": "abc-123"},
            timeout=5.0,
            auth=InternalTokenAuth(token="tok"),
        )
    finally:
        mod.httpx.AsyncClient = orig

    assert resp.status_code == 200
    assert seen["params"] == {"user_id": "abc-123"}
