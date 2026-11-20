"""The request body limit over real sockets (issue #1466, review finding H1).

The ASGI-level tests (`test_request_body_limit.py`) cannot see what happens on the
wire after a refusal. The first version answered with `Connection: close`: uvicorn
then closed the socket with the rest of the body unread, the kernel reset the
connection, and a client still writing its body (httpx, which every HERD
service-to-service call uses, and the gateway) got a read error instead of the 413.

These tests start uvicorn in this process on 127.0.0.1 port 0, once per HTTP
implementation installed (h11 and httptools), with an app built the way a HERD
service builds its own (CORS, the request logger, `install_validation_errors`), and
talk to it with `httpx.AsyncClient`. Every attempt must get the exact 413 (no retry
hides a reset), and the same connection must then serve a normal request: uvicorn
reads and discards what is left of a refused body. A 64 MiB chunked body refused at
the limit must not grow memory with the body: the discard holds nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import tracemalloc
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import Body, FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.logging import RequestLoggingMiddleware
from herd_common.validation_errors import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    install_validation_errors,
)

LIMIT = DEFAULT_MAX_REQUEST_BODY_BYTES
ATTEMPTS = 10
_MIB = 1024 * 1024
_JSON = {"content-type": "application/json"}
EXPECTED = (
    b'{"detail":[{"type":"body_too_large","loc":["body"],'
    b'"msg":"Request body is larger than ' + str(LIMIT).encode() + b' bytes"}]}'
)

HTTP_IMPLS = [impl for impl in ("h11", "httptools") if importlib.util.find_spec(impl)]


class _ClientPorts:
    """Pure ASGI: records the client port of every HTTP request, refused ones too, so
    a test can tell whether two requests shared one connection."""

    def __init__(self, app: Any, ports: list[int]) -> None:
        self.app = app
        self.ports = ports

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.ports.append(scope["client"][1])
        await self.app(scope, receive, send)


def _app(ports: list[int]) -> FastAPI:
    app = FastAPI()
    add_cors_middleware(app, "https://herd.example")
    app.add_middleware(RequestLoggingMiddleware)
    install_validation_errors(app, max_body_bytes=LIMIT)
    app.add_middleware(_ClientPorts, ports=ports)

    @app.post("/json")
    async def json_route(body: dict[str, Any] = Body(...)):
        return {"keys": len(body)}

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    return app


@contextlib.asynccontextmanager
async def _serving(app: FastAPI, http: str) -> AsyncIterator[str]:
    """uvicorn on 127.0.0.1, an ephemeral port, in this event loop; shut down cleanly."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        http=http,
        ws="none",
        lifespan="off",
        log_config=None,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        # Readiness polling only: the outcome of a test never depends on it.
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.005)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


async def _chunks(total: int, size: int = 256 * 1024) -> AsyncIterator[bytes]:
    """`total` bytes as chunks of one shared object, so the sender allocates nothing."""
    chunk = b"x" * size
    sent = 0
    while sent < total:
        part = chunk if total - sent >= size else chunk[: total - sent]
        sent += len(part)
        yield part


def _client(base_url: str) -> httpx.AsyncClient:
    # One connection at most: a refused body that cost the connection shows as a new
    # client port on the next request.
    return httpx.AsyncClient(
        base_url=base_url, timeout=30.0, limits=httpx.Limits(max_connections=1)
    )


@pytest.mark.parametrize("http", HTTP_IMPLS)
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(lambda: b"x" * (LIMIT + 1), id="declared_limit_plus_one"),
        pytest.param(lambda: b"x" * (16 * _MIB), id="declared_16_mib"),
        pytest.param(lambda: _chunks(LIMIT + 1), id="chunked_limit_plus_one"),
        pytest.param(lambda: _chunks(16 * _MIB), id="chunked_16_mib"),
    ],
)
async def test_every_refusal_reaches_the_client_and_the_connection_serves_the_next_request(
    http, body
):
    ports: list[int] = []
    async with _serving(_app(ports), http) as base_url, _client(base_url) as client:
        for attempt in range(ATTEMPTS):
            ports.clear()
            refused = await client.post("/json", content=body(), headers=_JSON)
            assert refused.status_code == 413, (attempt, refused.status_code)
            assert refused.content == EXPECTED, attempt
            assert "close" not in refused.headers.get("connection", "").lower()
            ok = await client.get("/ok")
            assert ok.json() == {"ok": True}, attempt
            assert len(ports) == 2 and ports[0] == ports[1], (attempt, ports)


@pytest.mark.parametrize("http", HTTP_IMPLS)
async def test_a_body_at_the_limit_still_reaches_the_route(http):
    body = b'{"k":"' + b"v" * (LIMIT - 8) + b'"}'
    assert len(body) == LIMIT
    async with _serving(_app([]), http) as base_url, _client(base_url) as client:
        resp = await client.post("/json", content=body, headers=_JSON)
    assert resp.json() == {"keys": 1}


@pytest.mark.parametrize("http", HTTP_IMPLS)
async def test_refusing_a_64_mib_chunked_body_holds_no_more_than_the_limit(http):
    """The server reads the body up to the limit (the route's own body reader and the
    surrogate check each join what they read), refuses, and then discards the other
    60 MiB as they arrive. The peak of everything this process allocates while that
    happens, client and server, stays under three times the limit, so under a fifth
    of the body: nothing holds the discarded part."""
    total = 64 * _MIB
    async with _serving(_app([]), http) as base_url, _client(base_url) as client:
        await client.get("/ok")  # the connection and the app are warm before measuring
        tracemalloc.start()
        try:
            refused = await client.post("/json", content=_chunks(total), headers=_JSON)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert refused.content == EXPECTED
        assert (await client.get("/ok")).json() == {"ok": True}
    assert peak < 3 * LIMIT
    assert peak < total / 5
