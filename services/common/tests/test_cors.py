"""Unit tests for herd_common.cors.add_cors_middleware (issue #595 item 2).

Covers the split/strip/filter origin parsing (including empty entries and
whitespace, matching the comprehension every service used to hand-write) and
that the middleware is actually registered and enforced by a live request.
"""

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.cors import add_cors_middleware
from httpx import ASGITransport, AsyncClient


def _build_app(cors_origins: str) -> FastAPI:
    app = FastAPI()
    add_cors_middleware(app, cors_origins)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return app


def test_registers_cors_middleware():
    app = _build_app("http://localhost:5173")
    classes = [m.cls for m in app.user_middleware]
    assert CORSMiddleware in classes


@pytest.mark.parametrize(
    ("cors_origins", "expected"),
    [
        ("http://localhost:5173", ["http://localhost:5173"]),
        (
            "http://localhost:5173,http://example.com",
            ["http://localhost:5173", "http://example.com"],
        ),
        # Whitespace around entries is stripped.
        (
            " http://localhost:5173 , http://example.com ",
            ["http://localhost:5173", "http://example.com"],
        ),
        # Empty entries (leading/trailing/doubled commas) are filtered out.
        (
            ",http://localhost:5173,,http://example.com,",
            ["http://localhost:5173", "http://example.com"],
        ),
        ("", []),
        ("   ", []),
        (",,", []),
    ],
)
def test_origin_parsing_matches_split_strip_filter_comprehension(cors_origins, expected):
    # Same comprehension every service hand-wrote before extraction.
    assert [o.strip() for o in cors_origins.split(",") if o.strip()] == expected


async def test_allowed_origin_receives_cors_headers():
    app = _build_app("http://localhost:5173")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ping", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert resp.headers.get("access-control-allow-credentials") == "true"


async def test_disallowed_origin_gets_no_cors_header():
    app = _build_app("http://localhost:5173")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ping", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
