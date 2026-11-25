"""The request body limit `install_validation_errors` installs (issue #1466).

Nothing bounded a request body: FastAPI reads and parses a whole JSON body before a
route's dependencies run, so a client with no token could make any service parse
megabytes before its 401. `LimitRequestBody` refuses a body over its route's limit
with 413, before the body is parsed, scanned for a lone surrogate, or authenticated.

The unit level drives the middleware with scripted ASGI messages (what was read,
what was handed on, what was raised); the app level runs a FastAPI app built the way
every service builds its own. The per-service suites pin the same through each real
app, and `tests/unit/test_request_body_limit_wiring.py` enumerates every service.
"""

from __future__ import annotations

import json
import logging
import time
import tracemalloc
from typing import Any

import pytest
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from herd_common import validation_errors
from herd_common.validation_errors import (
    BODY_TOO_LARGE_TYPE,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    EXTRA_BODY_BYTES_ATTRIBUTE,
    FORWARD_ENVELOPE_BYTES,
    LARGE_JSON_BODY_BYTES,
    MIN_MAX_REQUEST_BODY_BYTES,
    LimitRequestBody,
    RefuseLoneSurrogates,
    body_too_large_error,
    declares_more_than,
    extends_body_limit,
    install_validation_errors,
    max_body_bytes_from_text,
    route_body_limit,
)
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

LIMIT = 64
_JSON = {"content-type": "application/json"}
SUR_ESCAPE = b'"\\ud800"'


# -- The arithmetic ---------------------------------------------------------------------


def _cl(*values: bytes) -> list[tuple[bytes, bytes]]:
    return [(b"content-type", b"application/json")] + [(b"content-length", v) for v in values]


@pytest.mark.parametrize(
    ("headers", "over"),
    [
        (_cl(), False),
        (_cl(b"0"), False),
        (_cl(b"63"), False),
        (_cl(b"64"), False),
        (_cl(b"65"), True),
        (_cl(b" 65 "), True),
        (_cl(b"0000000000000000000000000065"), True),
        (_cl(b"0000000000000000000000000064"), False),
        (_cl(b"9" * 19), True),
        (_cl(b"1" + b"0" * 5000), True),
        (_cl(b""), False),
        (_cl(b"-1"), False),
        (_cl(b"-100000"), False),
        (_cl(b"+65"), False),
        (_cl(b"65.0"), False),
        (_cl(b"1e9"), False),
        (_cl(b"abc"), False),
        (_cl(b"6 5"), False),
        (_cl(b"\xd9\xa5\xd9\xa5"), False),  # Arabic-Indic digits are not ASCII digits
        (_cl(b"10", b"65"), True),
        (_cl(b"65", b"10"), True),
        (_cl(b"10", b"10"), False),
        (_cl(b"junk", b"65"), True),
        ([(b"x-content-length", b"65")], False),
    ],
    ids=[
        "missing",
        "zero",
        "under",
        "exactly",
        "one_over",
        "padded",
        "leading_zeros_over",
        "leading_zeros_at",
        "nineteen_digits",
        "five_thousand_digits",
        "empty",
        "negative_one",
        "negative",
        "plus_sign",
        "decimal",
        "exponent",
        "text",
        "inner_space",
        "non_ascii_digits",
        "duplicate_second_over",
        "duplicate_first_over",
        "duplicate_both_under",
        "invalid_then_over",
        "other_header",
    ],
)
def test_declares_more_than(headers, over):
    assert declares_more_than(headers, LIMIT) is over


async def _upload_endpoint():
    return None


def test_route_body_limit():
    """The default for a route with no mark, no endpoint, or no route at all; the
    default plus the route's upload bytes, read at each call, for a marked one."""
    budget = {"bytes": 1000}

    class Route:
        endpoint = extends_body_limit(lambda: budget["bytes"])(_upload_endpoint)

    class Unmarked:
        async def endpoint(self):
            return None

    assert route_body_limit(None, LIMIT) == LIMIT
    assert route_body_limit(object(), LIMIT) == LIMIT
    assert route_body_limit(Unmarked(), LIMIT) == LIMIT
    assert route_body_limit(Route(), LIMIT) == LIMIT + 1000
    budget["bytes"] = 7
    assert route_body_limit(Route(), LIMIT) == LIMIT + 7


def test_extends_body_limit_returns_the_endpoint_unchanged():
    async def endpoint(a: int, b: str = "x"):
        return a

    marked = extends_body_limit(lambda: 5)(endpoint)
    assert marked is endpoint
    assert getattr(endpoint, EXTRA_BODY_BYTES_ATTRIBUTE)() == 5


def test_the_one_item_names_the_limit_and_nothing_else():
    assert body_too_large_error(LIMIT) == {
        "type": "body_too_large",
        "loc": ["body"],
        "msg": "Request body is larger than 64 bytes",
    }
    assert BODY_TOO_LARGE_TYPE == "body_too_large"


def test_the_default_and_the_allowances_are_pinned():
    """The one place each number lives (the config copy and the compose defaults are
    pinned to it elsewhere). The default is 256 KiB since issue #1484; the large-JSON
    allowance keeps the 4 MiB every route took before it."""
    assert DEFAULT_MAX_REQUEST_BODY_BYTES == 256 * 1024 == 262144
    assert LARGE_JSON_BODY_BYTES == 4 * 1024 * 1024 == 4194304
    assert MIN_MAX_REQUEST_BODY_BYTES == 64 * 1024
    assert FORWARD_ENVELOPE_BYTES == 256 * 1024
    assert MIN_MAX_REQUEST_BODY_BYTES <= DEFAULT_MAX_REQUEST_BODY_BYTES < LARGE_JSON_BODY_BYTES


@pytest.mark.parametrize(
    "setting", [MIN_MAX_REQUEST_BODY_BYTES, DEFAULT_MAX_REQUEST_BODY_BYTES, 8 * 1024 * 1024]
)
def test_route_body_limit_of_a_large_json_route_adds_the_allowance_to_any_setting(setting):
    """A mark adds to the setting, so an operator who raises MAX_REQUEST_BODY_BYTES
    past the allowance raises the large-JSON routes too: the limit is never below
    max(setting, LARGE_JSON_BODY_BYTES). A forward of a large body adds the envelope."""

    async def canvas_save(body: dict = Body(...)):
        return body

    async def forwarded_canvas_save(body: dict = Body(...)):
        return body

    class LargeJson:
        endpoint = extends_body_limit(lambda: LARGE_JSON_BODY_BYTES)(canvas_save)

    class LargeForward:
        endpoint = extends_body_limit(lambda: LARGE_JSON_BODY_BYTES + FORWARD_ENVELOPE_BYTES)(
            forwarded_canvas_save
        )

    large = route_body_limit(LargeJson(), setting)
    assert large == setting + LARGE_JSON_BODY_BYTES
    assert large > max(setting, LARGE_JSON_BODY_BYTES)
    assert route_body_limit(LargeForward(), setting) == large + FORWARD_ENVELOPE_BYTES
    assert route_body_limit(None, setting) == setting


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("65536", 65536),
        (" 4194304\n", 4194304),
        ("0000065536", 65536),
        ("99999999999999999999999", 99999999999999999999999),
        ("65535", None),
        ("0", None),
        ("", None),
        ("   ", None),
        ("-65536", None),
        ("+65536", None),
        ("8388608.0", None),
        ("1_000_000", None),
        ("1e6", None),
        ("0x100000", None),
        ("4MB", None),
        ("\u0661\u0660\u0660\u0660\u0660\u0660", None),  # Arabic-Indic digits
    ],
)
def test_the_setting_is_read_as_decimal_digits_at_least_the_floor(raw, value):
    """The one reading both the Settings field and config's reader use."""
    assert max_body_bytes_from_text(raw) == value


@pytest.mark.parametrize(
    ("limit", "declared", "over"),
    [
        (10**18 - 1, b"1" + b"0" * 18, True),
        (10**18, b"1" + b"0" * 18, False),
        (10**18, b"1" + b"0" * 17 + b"1", True),
        (10**18, b"9" * 18, False),
        (10**30, b"9" * 30, False),
        (10**30, b"1" + b"0" * 29 + b"1", True),
        (10**30, b"1" + b"0" * 31, True),
        (10**30, b"0" * 40 + b"1" + b"0" * 30, False),
        (5, b"6", True),
        (5, b"5", False),
        (5, b"10", True),
    ],
)
def test_declares_more_than_is_exact_for_any_limit(limit, declared, over):
    assert declares_more_than([(b"content-length", declared)], limit) is over


# -- The middleware, driven with scripted messages ---------------------------------------


class _Script:
    """A scripted `receive` that counts how many messages were taken from it."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.taken = 0

    async def __call__(self) -> dict:
        self.taken += 1
        return self.messages.pop(0) if self.messages else {"type": "http.disconnect"}


def _chunks(*sizes: int) -> list[dict]:
    return [
        {"type": "http.request", "body": b"x" * size, "more_body": i < len(sizes) - 1}
        for i, size in enumerate(sizes)
    ]


async def _drive(
    messages: list[dict],
    *,
    headers: list | None = None,
    route: Any = None,
    start_first: bool = False,
    reads: int = 10,
) -> tuple[list[dict], HTTPException | None, _Script, list[dict]]:
    """Run the middleware around an app that reads up to `reads` messages; return what
    the app got, the refusal it saw (if any), the script, and what was sent."""
    got: list[dict] = []
    refusal: list[HTTPException] = []
    sent: list[dict] = []

    async def app(scope, receive, send):
        if start_first:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            for _ in range(reads):
                message = await receive()
                got.append(message)
                if message["type"] != "http.request" or not message.get("more_body"):
                    break
        except HTTPException as exc:
            refusal.append(exc)

    async def send(message):
        sent.append(message)

    script = _Script(messages)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or _cl()}
    if route is not None:
        scope["route"] = route
    await LimitRequestBody(app, max_body_bytes=LIMIT)(scope, script, send)
    return got, (refusal[0] if refusal else None), script, sent


def _assert_refused(refusal: HTTPException | None, limit: int = LIMIT) -> None:
    assert refusal is not None
    assert refusal.status_code == 413
    assert refusal.detail == [body_too_large_error(limit)]
    assert refusal.headers is None  # never Connection: close


async def test_a_declared_length_over_the_limit_is_refused_before_anything_is_read():
    got, refusal, script, _ = await _drive(_chunks(10), headers=_cl(b"65"))
    _assert_refused(refusal)
    assert (got, script.taken) == ([], 0)


async def test_a_body_of_exactly_the_limit_is_handed_on():
    messages = _chunks(LIMIT)
    got, refusal, _, _ = await _drive(list(messages), headers=_cl(b"64"))
    assert refusal is None
    assert got == messages


async def test_a_chunked_body_is_refused_at_the_read_that_crosses_the_limit():
    """No Content-Length: 40 bytes are handed on, the read that would make 80 is
    refused instead, and nothing after it is read."""
    got, refusal, script, _ = await _drive(_chunks(40, 40, 40))
    _assert_refused(refusal)
    assert [len(m["body"]) for m in got] == [40]
    assert script.taken == 2


async def test_a_body_longer_than_its_content_length_is_refused_by_the_count():
    got, refusal, script, _ = await _drive(_chunks(30, 30, 30), headers=_cl(b"10"))
    _assert_refused(refusal)
    assert sum(len(m["body"]) for m in got) == 60
    assert script.taken == 3


@pytest.mark.parametrize(
    "value", [b"abc", b"-1", b"", b"1e9"], ids=["text", "negative", "empty", "exponent"]
)
async def test_an_invalid_content_length_leaves_the_count_to_bound_the_body(value):
    small, refusal, _, _ = await _drive(_chunks(LIMIT), headers=_cl(value))
    assert refusal is None and len(small[0]["body"]) == LIMIT
    _, refusal, _, _ = await _drive(_chunks(LIMIT, 1), headers=_cl(value))
    _assert_refused(refusal)


async def test_messages_are_handed_on_as_received_never_copied_or_joined():
    messages = _chunks(10, 20, 30)
    got, refusal, _, _ = await _drive(list(messages))
    assert refusal is None
    assert len(got) == 3
    assert all(a is b for a, b in zip(got, messages, strict=True))


async def test_a_marked_route_takes_its_own_limit():
    class Route:
        endpoint = extends_body_limit(lambda: 100)(_upload_endpoint)

    got, refusal, _, _ = await _drive(_chunks(150), headers=_cl(b"150"), route=Route())
    assert refusal is None and len(got[0]["body"]) == 150
    _, refusal, _, _ = await _drive(_chunks(165), headers=_cl(b"165"), route=Route())
    _assert_refused(refusal, LIMIT + 100)
    _, refusal, _, _ = await _drive(_chunks(100, 65), route=Route())
    _assert_refused(refusal, LIMIT + 100)


async def test_after_the_response_started_nothing_is_refused_and_the_reading_stops():
    """Starlette's disconnect listener can read after a route that never read its body
    has started answering: raising then would break that response, so the read that
    crosses the limit answers `http.disconnect` and hands on no byte past it."""
    got, refusal, script, sent = await _drive(_chunks(40, 40, 40), start_first=True)
    assert refusal is None
    assert [m["type"] for m in got] == ["http.request", "http.disconnect"]
    assert script.taken == 2
    assert [m["type"] for m in sent] == ["http.response.start"]


async def test_after_the_response_started_a_declared_length_is_not_refused():
    """Declared over the limit, read after the response started: no refusal, nothing
    read, the read answers `http.disconnect`, and so does every later one."""
    got, refusal, script, sent = await _drive(
        _chunks(40, 40, 40), headers=_cl(b"120"), start_first=True
    )
    assert refusal is None
    assert got == [{"type": "http.disconnect"}]
    assert script.taken == 0
    assert [m["type"] for m in sent] == ["http.response.start"]


@pytest.mark.parametrize("declared", [True, False], ids=["declared", "received"])
async def test_a_read_racing_a_response_start_is_not_refused(declared):
    """The read Starlette's disconnect listener makes can come before the response it
    belongs to has sent its start, which is already scheduled. The check yields once
    before refusing, sees the start, and answers `http.disconnect` instead of raising
    into a response that has begun."""
    import anyio

    got: list[dict] = []
    sent: list[dict] = []

    async def app(scope, receive, send):
        async with anyio.create_task_group() as group:
            group.start_soon(send, {"type": "http.response.start", "status": 200, "headers": []})
            while True:
                message = await receive()
                got.append(message)
                if message["type"] != "http.request" or not message.get("more_body"):
                    break

    async def send(message):
        sent.append(message)

    script = _Script(_chunks(40, 40, 40))
    headers = _cl(b"120") if declared else _cl()
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    await LimitRequestBody(app, max_body_bytes=LIMIT)(scope, script, send)
    assert got[-1] == {"type": "http.disconnect"}
    assert sum(len(m.get("body", b"")) for m in got) <= LIMIT
    assert [m["type"] for m in sent] == ["http.response.start"]


async def test_a_streaming_route_that_never_reads_its_body_answers_with_no_middleware_around():
    """With no BaseHTTPMiddleware around the app: the route streams without reading
    its body, so the only reads are the disconnect listener's. Nothing is refused and
    nothing raises; the stream may end early when the listener sees
    `http.disconnect`, but never answers anything but 200. HERD's services are not
    built this way; see the known limit below."""
    async with _client(_app()) as client:
        for content in (b"x" * (LIMIT * 4), _parts(LIMIT * 4)):
            resp = await client.post("/stream", content=content)
            assert resp.status_code == 200
            assert b"abc".startswith(resp.content)


def _logged_streaming_app() -> FastAPI:
    """Built like a HERD service (the request logger, a BaseHTTPMiddleware, around
    the install), with a route that streams without reading its body."""
    from herd_common.logging import RequestLoggingMiddleware

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    install_validation_errors(app, max_body_bytes=LIMIT)

    @app.post("/stream")
    async def stream():
        async def parts():
            for part in (b"a", b"b", b"c"):
                yield part

        return StreamingResponse(parts(), media_type="text/event-stream")

    return app


def _stream_scope(headers: list) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }


def _answer(sent: list[dict]) -> tuple[list[int], bytes]:
    starts = [m["status"] for m in sent if m["type"] == "http.response.start"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return starts, body


async def test_known_limit_a_streaming_route_that_never_reads_its_body_fails_behind_the_logger():
    """Documents a known limit (OPS-VALID-9; issue #1483 kept it, by decision, and
    guards against it instead). No HERD route is affected: the one streaming route
    takes its JSON body as a parameter, and `tests/unit/test_request_body_limit_wiring.py`
    fails on a streaming route that does not, unless it is listed there.

    Behind the request logger (a BaseHTTPMiddleware, as in every HERD service), the
    disconnect listener of a streaming response makes the first read of a body whose
    `Content-Length` is over the limit while the response's start is still held in the
    logger's stream. The check sees no start and refuses; the listener's task group
    ends with the response never started, the logger has nothing to return, and
    Starlette raises `RuntimeError: No response returned.`: the client gets a 500 (a
    server that cannot send one resets the connection). What is pinned: the failure
    is a server error, never a served response, and nothing of the body is read."""
    script = _Script(_chunks(256))
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    with pytest.raises(RuntimeError, match="No response returned"):
        await _logged_streaming_app()(_stream_scope(_cl(b"256")), script, send)
    assert script.taken == 0
    assert _answer(sent) == ([500], b"Internal Server Error")


async def test_known_limit_the_same_route_with_a_chunked_body_over_the_limit_is_cut_short():
    """Documents the same limit for a body with no `Content-Length`: the read that
    takes the count past the limit comes after the response started, so it answers
    `http.disconnect`; the listener takes that as the client leaving and ends the
    stream, which is cut short (here after its first part). The message that crossed
    the limit is read and not handed on, so nothing of the body reaches the route."""
    script = _Script([{"type": "http.request", "body": b"x" * 256, "more_body": False}])
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await _logged_streaming_app()(_stream_scope(_cl()), script, send)
    assert script.taken == 1
    starts, body = _answer(sent)
    assert starts == [200]
    assert b"abc".startswith(body) and body != b"abc"


async def test_a_disconnect_and_later_reads_pass_through():
    got, refusal, _, _ = await _drive([{"type": "http.request", "body": b"x", "more_body": True}])
    assert refusal is None
    assert got[-1] == {"type": "http.disconnect"}


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_a_websocket_or_lifespan_scope_is_passed_through_untouched(scope_type):
    calls = []

    async def app(scope, receive, send):
        calls.append((receive, send))

    async def receive():
        return {"type": f"{scope_type}.connect"}

    async def send(message):
        return None

    headers = [(b"content-length", b"999999999")]
    await LimitRequestBody(app, max_body_bytes=1)(
        {"type": scope_type, "headers": headers}, receive, send
    )
    assert calls == [(receive, send)]


async def test_a_request_whose_body_is_never_read_judges_nothing():
    """A route that answers without reading: no read, no refusal, whatever is declared."""
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    script = _Script(_chunks(10))
    scope = {"type": "http", "method": "GET", "path": "/", "headers": _cl(b"999999999")}
    await LimitRequestBody(app, max_body_bytes=LIMIT)(scope, script, send)
    assert script.taken == 0
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


async def test_a_refusal_logs_the_limit_and_the_reason_never_the_body(caplog):
    marker = b"body-canary-1466"
    caplog.set_level(logging.INFO, logger="herd_common.validation_errors")
    await _drive(
        [{"type": "http.request", "body": marker * 10, "more_body": False}],
        headers=_cl(b"160"),
    )
    await _drive(_chunks(40, 40))
    records = [r for r in caplog.records if r.name == "herd_common.validation_errors"]
    assert [(r.action, r.limit_bytes, r.reason) for r in records] == [
        ("request_body_too_large", LIMIT, "declared"),
        ("request_body_too_large", LIMIT, "received"),
    ]
    for record in records:
        assert record.levelno == logging.INFO
        assert marker.decode() not in record.getMessage()
        assert "xxxx" not in record.getMessage()


# -- Through an app built the way every service builds its own ---------------------------


def _deny():
    raise HTTPException(status_code=401, detail="Not authenticated")


def _app(*, refuse: bool = True, limit: int = LIMIT) -> FastAPI:
    app = FastAPI()
    install_validation_errors(app, max_body_bytes=limit, refuse_lone_surrogates=refuse)

    @app.post("/guarded", dependencies=[Depends(_deny)])
    async def guarded(body: dict[str, Any]):
        return {"ok": True}

    @app.post("/open")
    async def open_route(body: Any = Body(None)):
        return {"type": type(body).__name__}

    @app.post("/raw")
    async def raw(request: Request):
        return {"length": len(await request.body())}

    @app.post("/nobody")
    async def nobody():
        return {"ok": True}

    @app.post("/stream")
    @app.get("/stream")
    async def stream():
        async def parts():
            for part in (b"a", b"b", b"c"):
                yield part

        return StreamingResponse(parts(), media_type="text/event-stream")

    @app.post("/upload")
    @extends_body_limit(lambda: 1000)
    async def upload(name: str = Form(...), file: UploadFile = File(...)):
        return {"name": name, "size": len(await file.read())}

    @app.post("/form")
    async def form(name: str = Form(...), file: UploadFile = File(...)):
        return {"name": name}

    return app


async def _parts(total: int, size: int = 16):
    chunk = b"y" * size
    for _ in range(total // size):
        yield chunk


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _padded(head: bytes, size: int) -> bytes:
    """A JSON body of exactly `size` bytes: `head` and spaces."""
    return head + b" " * (size - len(head))


async def test_the_refusal_is_these_exact_bytes():
    async with _client(_app()) as client:
        resp = await client.post("/open", content=b"[" * (LIMIT + 1), headers=_JSON)
    assert resp.status_code == 413
    assert resp.content == (
        b'{"detail":[{"type":"body_too_large","loc":["body"],'
        b'"msg":"Request body is larger than 64 bytes"}]}'
    )
    assert resp.headers["content-type"] == "application/json"
    assert "connection" not in resp.headers


async def test_over_the_limit_is_413_before_authentication():
    """With no credentials, under the limit is the dependency's 401; over it is 413."""
    async with _client(_app()) as client:
        under = await client.post("/guarded", content=_padded(b"{}", LIMIT), headers=_JSON)
        over = await client.post("/guarded", content=_padded(b"{}", LIMIT + 1), headers=_JSON)
    assert (under.status_code, over.status_code) == (401, 413)


async def test_a_body_over_the_limit_is_never_parsed():
    """A body that is not JSON: at the limit FastAPI parses it and answers
    `json_invalid`; one byte over it is the 413, so no parse ran."""
    async with _client(_app()) as client:
        at = await client.post("/open", content=b"x" * LIMIT, headers=_JSON)
        over = await client.post("/open", content=b"x" * (LIMIT + 1), headers=_JSON)
    assert at.status_code == 422 and at.json()["detail"][0]["type"] == "json_invalid"
    assert over.status_code == 413


async def test_a_body_over_the_limit_is_never_scanned_for_a_lone_surrogate(monkeypatch):
    """The surrogate check reads through the limit, so it never sees a body over it:
    a surrogate body at the limit is its 422, over the limit the 413, unscanned."""
    scanned: list[int] = []
    original = validation_errors._raw_text_holds_a_lone_surrogate

    def counting(body: bytes) -> bool:
        scanned.append(len(body))
        return original(body)

    monkeypatch.setattr(validation_errors, "_raw_text_holds_a_lone_surrogate", counting)
    async with _client(_app()) as client:
        at = await client.post("/open", content=_padded(SUR_ESCAPE, LIMIT), headers=_JSON)
        over = await client.post("/open", content=_padded(SUR_ESCAPE, LIMIT + 1), headers=_JSON)
    assert at.status_code == 422 and at.json()["detail"][0]["type"] == "string_unicode"
    assert over.status_code == 413
    assert scanned == [LIMIT]


async def test_a_chunked_body_over_the_limit_is_refused_before_the_surrogate_check_buffers_it(
    monkeypatch,
):
    """With no Content-Length the order is what decides: the surrogate check reads the
    whole body before it scans, so only a limit outside it stops that read at the
    limit. Placed inside, the check would buffer and scan the whole body first."""
    scanned: list[int] = []
    original = validation_errors._raw_text_holds_a_lone_surrogate

    def counting(body: bytes) -> bool:
        scanned.append(len(body))
        return original(body)

    monkeypatch.setattr(validation_errors, "_raw_text_holds_a_lone_surrogate", counting)

    async def parts():
        yield SUR_ESCAPE
        for _ in range(4):
            yield b" " * 20

    async with _client(_app()) as client:
        resp = await client.post("/open", content=parts(), headers=_JSON)
    assert resp.status_code == 413
    assert resp.json() == {"detail": [body_too_large_error(LIMIT)]}
    assert scanned == []


async def test_a_chunked_body_over_the_limit_is_413_through_the_app():
    async def parts():
        for _ in range(4):
            yield b" " * 20

    async with _client(_app()) as client:
        json_route = await client.post("/open", content=parts(), headers=_JSON)
        raw_route = await client.post(
            "/raw", content=parts(), headers={"content-type": "text/plain"}
        )
    assert (json_route.status_code, raw_route.status_code) == (413, 413)


async def test_every_content_type_is_bounded():
    """The limit, unlike the surrogate check, does not look at the content type."""
    async with _client(_app()) as client:
        for content_type in ("text/plain", "application/octet-stream", None):
            headers = {} if content_type is None else {"content-type": content_type}
            at = await client.post("/raw", content=b"x" * LIMIT, headers=headers)
            over = await client.post("/raw", content=b"x" * (LIMIT + 1), headers=headers)
            assert (at.status_code, at.json(), over.status_code) == (200, {"length": LIMIT}, 413)


async def test_an_upload_route_takes_its_own_limit_and_another_form_does_not():
    files = {"file": ("f.bin", b"z" * 500)}
    async with _client(_app()) as client:
        marked = await client.post("/upload", files=files, data={"name": "n"})
        unmarked = await client.post("/form", files=files, data={"name": "n"})
        too_big = await client.post(
            "/upload", files={"file": ("f.bin", b"z" * 1100)}, data={"name": "n"}
        )
    assert marked.status_code == 200 and marked.json() == {"name": "n", "size": 500}
    assert unmarked.status_code == 413
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": [body_too_large_error(LIMIT + 1000)]}


async def test_a_route_that_reads_no_body_an_unknown_route_and_a_stream_are_unchanged():
    big = b"x" * (LIMIT * 4)
    async with _client(_app()) as client:
        nobody = await client.post("/nobody", content=big, headers=_JSON)
        unknown = await client.post("/missing", content=big, headers=_JSON)
        wrong_method = await client.put("/open", content=big, headers=_JSON)
        stream = await client.get("/stream")
    assert (nobody.status_code, unknown.status_code, wrong_method.status_code) == (200, 404, 405)
    assert (stream.status_code, stream.content) == (200, b"abc")


async def test_the_limit_holds_without_the_surrogate_check():
    """Config installs without the surrogate check; the limit is still installed."""
    async with _client(_app(refuse=False)) as client:
        over = await client.post("/open", content=b"x" * (LIMIT + 1), headers=_JSON)
        at = await client.post("/open", content=_padded(b"{}", LIMIT), headers=_JSON)
    assert (over.status_code, at.status_code) == (413, 200)


def test_install_puts_the_limit_just_outside_the_surrogate_check():
    """Both innermost, the limit first: the surrogate check reads through it."""
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=_passthrough)
    install_validation_errors(app, max_body_bytes=123)
    assert [m.cls for m in app.user_middleware] == [
        BaseHTTPMiddleware,
        LimitRequestBody,
        RefuseLoneSurrogates,
    ]
    assert app.user_middleware[1].kwargs == {"max_body_bytes": 123}


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1024", None])
def test_install_refuses_a_limit_that_is_not_a_positive_int(bad):
    with pytest.raises((TypeError, ValueError)):
        install_validation_errors(FastAPI(), max_body_bytes=bad)


def test_install_requires_the_limit():
    with pytest.raises(TypeError):
        install_validation_errors(FastAPI())  # type: ignore[call-arg]


async def _passthrough(request, call_next):
    return await call_next(request)


@pytest.mark.parametrize("order", ["logger_first", "logger_after"])
async def test_the_refusal_survives_a_base_http_middleware_in_either_order(order):
    """The request logger is a BaseHTTPMiddleware; a refusal raised through its task
    group became 400 `There was an error parsing the body` for the surrogate check."""
    app = FastAPI()
    if order == "logger_first":
        app.add_middleware(BaseHTTPMiddleware, dispatch=_passthrough)
    install_validation_errors(app, max_body_bytes=LIMIT)
    if order == "logger_after":
        app.add_middleware(BaseHTTPMiddleware, dispatch=_passthrough)

    @app.post("/m")
    async def m(body: Any = Body(None)):
        return {"ok": True}

    async with _client(app) as client:
        over = await client.post("/m", content=b"x" * (LIMIT + 1), headers=_JSON)
        at = await client.post("/m", content=_padded(b"{}", LIMIT), headers=_JSON)
    assert (over.status_code, at.status_code) == (413, 200)
    assert over.json() == {"detail": [body_too_large_error(LIMIT)]}


# -- The cost: constant work per message, no copy -----------------------------------------
#
# Relative bounds on `time.process_time()`, never wall clock, so they hold pinned to one
# core and on a fast host; memory from tracemalloc, which is exact.

_MB = 1 << 20


async def _hand_on(messages: list[dict], limit: int) -> int:
    """Pass `messages` through the middleware to an app that reads them all."""
    count = 0

    async def app(scope, receive, send):
        nonlocal count
        while True:
            message = await receive()
            count += 1
            if not message.get("more_body"):
                return

    queue = iter(messages)

    async def receive():
        return next(queue)

    async def send(message):
        return None

    scope = {"type": "http", "method": "POST", "path": "/", "headers": _cl()}
    await LimitRequestBody(app, max_body_bytes=limit)(scope, receive, send)
    return count


def _messages(count: int, size: int) -> list[dict]:
    chunk = b"x" * size
    return [
        {"type": "http.request", "body": chunk, "more_body": i < count - 1} for i in range(count)
    ]


def test_handing_on_a_body_allocates_nothing_that_grows_with_it():
    """32 MB in 512 messages: the peak allocation while handing it on stays under a
    few kilobytes, so no message is copied or joined."""
    import asyncio

    messages = _messages(512, 64 * 1024)
    tracemalloc.start()
    try:
        assert asyncio.run(_hand_on(messages, 64 * _MB)) == 512
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024


def _process_time(fn, runs: int = 5) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.process_time()
        fn()
        best = min(best, time.process_time() - start)
    return best


def test_the_cost_per_message_does_not_grow_with_its_size():
    """The same number of messages, one byte each or 256 KiB each (5 GiB in all, one
    chunk object shared, so never allocated): the time to hand them on is about the
    same, so the work per message does not depend on its size (measured 1.02 times,
    about 7 ms each, pinned to one core; bounded at 2). A copy of each message would
    move 5 GiB and fail by a wide margin."""
    import asyncio

    small = _messages(20_000, 1)
    large = _messages(20_000, 256 * 1024)
    limit = 1 << 40

    def run(messages):
        return lambda: asyncio.run(_hand_on(messages, limit))

    assert _process_time(run(large)) < 2 * _process_time(run(small))


def test_through_an_app_an_over_limit_body_costs_less_than_parsing_it():
    """A 9 MB structural JSON body to a route behind a 401 dependency: without the
    limit FastAPI parses it before the 401; with it the declared length is refused
    before a byte is read (measured 0.4 ms against 300 ms pinned to one core; bounded
    at a third, for margin)."""
    import asyncio

    body = json.dumps([[[]] for _ in range(1_500_000)]).encode()

    def timed(limit: int) -> tuple[float, int]:
        app = _app(limit=limit)
        status = 0

        async def send() -> None:
            nonlocal status
            async with _client(app) as client:
                resp = await client.post("/guarded", content=body, headers=_JSON)
                status = resp.status_code

        best = _process_time(lambda: asyncio.run(send()), runs=3)
        return best, status

    unbounded, unbounded_status = timed(len(body) * 2)
    bounded, bounded_status = timed(DEFAULT_MAX_REQUEST_BODY_BYTES)
    assert (unbounded_status, bounded_status) == (401, 413)
    assert bounded < unbounded / 3
