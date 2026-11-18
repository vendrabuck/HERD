"""The shared request-validation (422) handler, herd_common.validation_errors.

FastAPI's default handler renders every pydantic error with its `input` (the
submitted value; for a missing field, the whole body) and `ctx`, and Starlette
cannot encode a lone surrogate, so a surrogate in the echoed input turned the 422
into a 500. The shared handler renders each error as exactly `{type, loc, msg}` and
replaces any lone surrogate in `loc` or `msg` with U+FFFD. These tests pin the
renderer directly and through a small app that installs it; each service's own
suite pins it through that service's real app.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from herd_common.validation_errors import (
    RENDERED_KEYS,
    STRING_UNICODE_MSG,
    STRING_UNICODE_TYPE,
    RefuseLoneSurrogates,
    body_lone_surrogate_errors,
    install_validation_errors,
    lone_surrogate_locs,
    render_validation_errors,
    restrict_validation_error_schema,
    validation_error_handler,
)
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

MARKER = "canary-Secret-7q"


async def _render(errors: list[dict]) -> tuple[int, object]:
    response = await validation_error_handler(None, RequestValidationError(errors))
    return response.status_code, json.loads(response.body)


async def test_the_handler_keeps_type_loc_msg_and_drops_input_ctx_and_everything_else():
    status, body = await _render(
        [
            {
                "type": "string_too_short",
                "loc": ("body", "password"),
                "msg": "String should have at least 8 characters",
                "input": "secret-pw",
                "ctx": {"min_length": 8},
                "url": "https://errors.pydantic.dev/",
            },
            {
                "type": "missing",
                "loc": ("body", "email"),
                "msg": "Field required",
                "input": {"password": "secret-pw"},
            },
        ]
    )
    assert status == 422
    assert body == {
        "detail": [
            {
                "type": "string_too_short",
                "loc": ["body", "password"],
                "msg": "String should have at least 8 characters",
            },
            {"type": "missing", "loc": ["body", "email"], "msg": "Field required"},
        ]
    }


async def test_the_handler_replaces_a_lone_surrogate_in_loc_and_msg_and_keeps_integers():
    """Defensive: no route is known to put a surrogate in `loc` or `msg`
    (pydantic refuses a string holding one as `string_unicode`), but a 500 is the
    cost of being wrong, so the handler never assumes it."""
    status, body = await _render(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", "a\ud800b", 3),
                "msg": "found `\udfff` at 1",
                "input": "\ud800",
            }
        ]
    )
    assert status == 422
    assert body == {
        "detail": [
            {
                "type": "extra_forbidden",
                "loc": ["body", "a�b", 3],
                "msg": "found `�` at 1",
            }
        ]
    }


async def test_the_handler_keeps_honest_non_ascii_text_unchanged():
    status, body = await _render(
        [{"type": "value_error", "loc": ("body", "full_name"), "msg": "Caf\u00e9 \U0001f511"}]
    )
    assert status == 422
    assert body["detail"][0]["msg"] == "Caf\u00e9 \U0001f511"


def test_render_takes_any_iterable_of_errors_and_an_error_without_a_loc():
    assert render_validation_errors(iter([{"type": "t", "msg": "m"}])) == [
        {"type": "t", "loc": [], "msg": "m"}
    ]


def test_restrict_leaves_a_document_without_the_component_alone():
    document = {"openapi": "3.1.0", "paths": {}}
    assert restrict_validation_error_schema(document) == {"openapi": "3.1.0", "paths": {}}


# -- Installed on an app ----------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    data: dict[str, str]


def _app() -> FastAPI:
    app = FastAPI()
    install_validation_errors(app)

    @app.post("/things")
    async def create(body: _Body):
        return {"ok": True}

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_install_registers_the_handler_for_request_validation_errors():
    app = _app()
    assert app.exception_handlers[RequestValidationError] is validation_error_handler


def test_install_makes_the_openapi_document_advertise_exactly_the_rendered_keys():
    app = _app()
    component = app.openapi()["components"]["schemas"]["ValidationError"]
    assert set(component["properties"]) == set(RENDERED_KEYS) == {"type", "loc", "msg"}
    assert sorted(component["required"]) == ["loc", "msg", "type"]
    assert app.openapi() is app.openapi()  # built once, then cached


@pytest.mark.parametrize(
    "raw",
    [
        # missing `name`: the default handler's `input` was the whole body
        b'{"data": {"password": "' + MARKER.encode() + b'"}}',
        # wrong type for a data value, next to a marker value
        b'{"name": "n", "data": {"password": "' + MARKER.encode() + b'", "n": 5}}',
        # a lone surrogate in a value: the default handler answered 500
        b'{"data": {"password": "' + MARKER.encode() + b'\\ud800"}}',
    ],
    ids=["missing", "wrong_type", "lone_surrogate"],
)
async def test_an_installed_app_never_echoes_the_body(raw):
    async with _client(_app()) as client:
        resp = await client.post(
            "/things", content=raw, headers={"Content-Type": "application/json"}
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail and all(set(error) == set(RENDERED_KEYS) for error in detail)
    assert MARKER.encode() not in resp.content


async def test_an_installed_app_still_names_a_refused_key_in_loc():
    """The documented limit: `loc` names the keys the caller sent."""
    async with _client(_app()) as client:
        resp = await client.post("/things", json={"name": "n", "data": {}, MARKER: "v"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {
            "type": "extra_forbidden",
            "loc": ["body", MARKER],
            "msg": "Extra inputs are not permitted",
        }
    ]


# -- A validator's own message ----------------------------------------------------------


class _Quoting(BaseModel):
    plain: str = ""
    quoted: str = ""

    @field_validator("plain")
    @classmethod
    def _plain(cls, value: str) -> str:
        raise ValueError(f"bad {value}")

    @field_validator("quoted")
    @classmethod
    def _quoted(cls, value: str) -> str:
        raise ValueError(f"bad {value!r}")


def _quoting_app(*, refuse: bool = False) -> FastAPI:
    """Without the body check by default: these tests pin what pydantic-core does with a
    validator's message, which the check now keeps a request body from reaching
    (OPS-VALID-5 stays the rule for any other data a validator sees)."""
    app = FastAPI()
    install_validation_errors(app, refuse_lone_surrogates=refuse)

    @app.post("/q")
    async def q(body: _Quoting):
        return {"ok": True}

    return app


async def test_a_validator_message_holding_a_lone_surrogate_is_a_500_the_handler_cannot_see():
    """The rule for validator authors (OPS-VALID-5): a plain `str` takes a lone
    surrogate, and pydantic-core cannot build an error whose message holds one. It
    raises UnicodeEncodeError instead of a validation error, so this handler never
    runs and the route answers 500. If a pydantic-core upgrade changes that, this
    test says so."""
    transport = ASGITransport(app=_quoting_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/q", content=b'{"plain": "Z\\ud800"}', headers={"Content-Type": "application/json"}
        )
    assert resp.status_code == 500


async def test_with_the_body_check_a_validator_never_sees_a_lone_surrogate():
    async with _client(_quoting_app(refuse=True)) as client:
        resp = await client.post(
            "/q", content=b'{"plain": "Z\\ud800"}', headers={"Content-Type": "application/json"}
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "string_unicode", "loc": ["body", "plain"], "msg": STRING_UNICODE_MSG}
    ]


async def test_a_validator_message_quoting_with_repr_is_a_clean_422():
    async with _client(_quoting_app()) as client:
        resp = await client.post(
            "/q", content=b'{"quoted": "Z\\ud800"}', headers={"Content-Type": "application/json"}
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "quoted"], "msg": "Value error, bad 'Z\\ud800'"}
    ]


# -- A lone surrogate is refused at the body (issues #1456, #1458, #1459) --------------

SUR = chr(0xD800)  # built, so no source escape is decoded early
LOW = chr(0xDFFF)
PAIR = "\U0001f511"  # one code point outside the BMP: a surrogate PAIR as JSON escapes
UNICODE = {"type": "string_unicode", "msg": STRING_UNICODE_MSG}


def _esc(value) -> bytes:
    """A JSON body with every non-ASCII code point escaped, so a lone surrogate reaches
    the app exactly as written (`json=` cannot send one)."""
    return json.dumps(value, ensure_ascii=True).encode("ascii")


def test_the_message_is_pydantics_own_for_a_constrained_string():
    class Bounded(BaseModel):
        s: str = Field(max_length=50)

    with pytest.raises(ValidationError) as caught:
        Bounded.model_validate({"s": "x" + SUR})
    (error,) = caught.value.errors()
    assert (error["type"], error["msg"]) == (STRING_UNICODE_TYPE, STRING_UNICODE_MSG)


@pytest.mark.parametrize(
    ("value", "locs"),
    [
        ("x" + SUR, [()]),
        (LOW + "x", [()]),
        ({"a": "x" + SUR}, [("a",)]),
        ({"a" + SUR: "v"}, [()]),
        ({"a" + SUR: {"b": SUR}}, [()]),  # a refused key's value is not searched
        ({"a": {"b": [1, "ok", SUR]}}, [("a", "b", 2)]),
        ({"k" + SUR: 1, "j" + LOW: 2}, [()]),  # one container, located once
        ([SUR, {"x": [SUR]}, "ok", {LOW: 1}], [(0,), (1, "x", 0), (3,)]),
        ({"a": PAIR, PAIR: "b", "c": [PAIR * 3]}, []),
        ({"a": "café 日本 한", "n": None, "t": True, "f": 1.5}, []),
        (LOW + SUR, [()]),  # a reversed pair is two lone surrogates
    ],
    ids=[
        "string",
        "low_surrogate",
        "value",
        "key",
        "key_hides_value",
        "nested",
        "two_keys_one_container",
        "document_order",
        "pairs",
        "non_ascii",
        "reversed_pair",
    ],
)
def test_lone_surrogate_locs(value, locs):
    assert lone_surrogate_locs(value) == locs


def test_lone_surrogate_locs_is_not_limited_by_the_recursion_limit():
    deep: object = SUR
    for _ in range(5000):
        deep = [deep]
    (loc,) = lone_surrogate_locs(deep)
    assert loc == (0,) * 5000


@pytest.mark.parametrize(
    ("body", "locs"),
    [
        (b'{"a": "x\\ud800"}', [["body", "a"]]),
        (b'{"a": "x\\uDBFF"}', [["body", "a"]]),
        (b'{"a": "\\udc00x"}', [["body", "a"]]),
        (b'{"\\udfff": 1}', [["body"]]),
        (b'"\\ud800"', [["body"]]),
        (b'{"a": [1, {"b": "\\ud800"}]}', [["body", "a", 1, "b"]]),
        # a surrogate written as raw UTF-8 bytes: json.loads decodes with surrogatepass
        ('{"a": "x\ud800"}'.encode("utf-8", "surrogatepass"), [["body", "a"]]),
        # a UTF-16 document, which JSON detects from its NUL bytes and always parses
        ('{"a": "x\ud800"}'.encode("utf-16-le", "surrogatepass"), [["body", "a"]]),
        ('{"a": "x\ud800\u0090"}'.encode("utf-16-le", "surrogatepass"), [["body", "a"]]),
        # an escaped backslash followed by text that reads like an escape
        (b'{"a": "\\\\ud800"}', []),
        # a backslash pair, then a real escape
        (b'{"a": "\\\\\\ud800"}', [["body", "a"]]),
        (b'{"a": "\\ud83d\\udd11"}', []),
        (b'{"a": "\\ud55c \\uD7A3"}', []),
        ('{"a": "한"}'.encode(), []),
        (b"\xef\xbb\xbf" + b'{"a": "\\ud800"}', [["body", "a"]]),
        (b'{"a": "x\\ud800"', []),  # not JSON: the route answers it as before
        (b"a=\\ud800", []),
        (b"", []),
    ],
    ids=[
        "value",
        "uppercase_hex",
        "low_first",
        "key",
        "bare_string",
        "nested",
        "raw_utf8_bytes",
        "utf16",
        "utf16_valid_utf8_lookalike",
        "escaped_backslash",
        "backslash_pair_then_escape",
        "valid_pair",
        "hangul_escapes",
        "hangul_raw",
        "utf8_bom",
        "malformed_json",
        "not_json",
        "empty",
    ],
)
def test_body_lone_surrogate_errors(body, locs):
    assert body_lone_surrogate_errors(body) == [{**UNICODE, "loc": loc} for loc in locs]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s: str = ""


def _open_app(*, refuse: bool = True) -> FastAPI:
    """Routes whose types accept a lone surrogate: a plain str, a free-form mapping,
    Any, a raw `Request`, plus routes that take no body, a form, and a query."""
    app = FastAPI()
    install_validation_errors(app, refuse_lone_surrogates=refuse)

    @app.post("/str")
    async def plain(body: _Strict):
        return {"len": len(body.s)}

    @app.post("/map")
    async def mapping(body: dict[str, str]):
        return {"keys": len(body)}

    @app.post("/any")
    async def anything(body: Any = Body(...)):
        return {"ok": True}

    @app.post("/raw")
    async def raw(request: Request):
        return {"type": type(json.loads(await request.body())).__name__}

    @app.post("/nobody")
    async def nobody():
        return {"ok": True}

    @app.post("/form")
    async def form(field: str = Form(...), upload: UploadFile = File(...)):
        return {"field": field, "filename": upload.filename}

    @app.post("/urlencoded")
    async def urlencoded(field: str = Form(...)):
        return {"has_surrogate": bool(lone_surrogate_locs(field))}

    @app.get("/q/{item}")
    async def query(item: str, q: str = "", request: Request = None):  # type: ignore[assignment]
        texts = [item, q, request.headers.get("x-probe", ""), request.cookies.get("c", "")]
        return {"has_surrogate": bool(lone_surrogate_locs(texts))}

    def deny():
        raise HTTPException(status_code=401, detail="Not authenticated")

    @app.post("/guarded", dependencies=[Depends(deny)])
    async def guarded(body: _Strict):
        return {"ok": True}

    return app


_JSON = {"content-type": "application/json"}


@pytest.mark.parametrize(
    ("path", "body", "locs"),
    [
        ("/str", {"s": "a" + SUR}, [["body", "s"]]),
        ("/map", {"k": "v" + SUR}, [["body", "k"]]),
        ("/map", {"k" + LOW: "v"}, [["body"]]),
        ("/any", {"a": [{"b": SUR}]}, [["body", "a", 0, "b"]]),
        ("/raw", [SUR, "ok", LOW], [["body", 0], ["body", 2]]),
    ],
    ids=["plain_str", "mapping_value", "mapping_key", "any_nested", "raw_request_body"],
)
async def test_a_lone_surrogate_anywhere_in_a_body_is_a_clean_422(path, body, locs):
    async with _client(_open_app()) as client:
        resp = await client.post(path, content=_esc(body), headers=_JSON)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": [{**UNICODE, "loc": loc} for loc in locs]}


@pytest.mark.parametrize("headers", [{}, {"content-type": "text/plain"}], ids=["none", "text"])
async def test_the_check_does_not_depend_on_the_content_type(headers):
    async with _client(_open_app()) as client:
        resp = await client.post("/raw", content=_esc({"a": SUR}), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{**UNICODE, "loc": ["body", "a"]}]


async def test_without_the_check_those_bodies_reach_the_route():
    """The check is what refuses them: each type accepts a lone surrogate, and the
    response that echoes one cannot be encoded (mutation proof for the middleware)."""
    async with _client(_open_app(refuse=False)) as client:
        assert (await client.post("/str", content=_esc({"s": SUR}), headers=_JSON)).json() == {
            "len": 1
        }
        resp = await client.post("/map", content=_esc({"k" + LOW: "v"}), headers=_JSON)
        assert resp.json() == {"keys": 1}
        resp = await client.post("/raw", content=_esc([SUR]), headers=_JSON)
        assert resp.json() == {"type": "list"}


@pytest.mark.parametrize(
    "body",
    [
        {"s": PAIR},
        {"s": "café 日本 한 \x00"},
        {"s": "\\ud800"},  # the six characters, not an escape
    ],
    ids=["surrogate_pair_escape", "non_ascii_and_nul", "escaped_backslash"],
)
async def test_honest_text_is_accepted(body):
    async with _client(_open_app()) as client:
        resp = await client.post("/str", content=_esc(body), headers=_JSON)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"len": len(body["s"])}


async def test_a_route_that_reads_no_body_and_an_unknown_route_are_unchanged():
    async with _client(_open_app()) as client:
        nobody = await client.post("/nobody", content=_esc({"a": SUR}), headers=_JSON)
        unknown = await client.post("/missing", content=_esc({"a": SUR}), headers=_JSON)
        wrong_method = await client.put("/str", content=_esc({"s": SUR}), headers=_JSON)
    assert (nobody.status_code, unknown.status_code, wrong_method.status_code) == (200, 404, 405)


async def test_the_refusal_comes_before_a_dependency_as_a_malformed_body_does():
    """FastAPI reads and parses the body before it runs a route's dependencies, so a
    body that is not JSON is a 422 before authentication; the surrogate check runs at
    that read, so it is the same."""
    async with _client(_open_app()) as client:
        malformed = await client.post("/guarded", content=b'{"s": ', headers=_JSON)
        surrogate = await client.post("/guarded", content=_esc({"s": SUR}), headers=_JSON)
        honest = await client.post("/guarded", content=_esc({"s": "x"}), headers=_JSON)
    assert (malformed.status_code, surrogate.status_code, honest.status_code) == (422, 422, 401)


async def test_a_multipart_body_is_passed_through_and_cannot_carry_a_lone_surrogate():
    """Starlette decodes a multipart field strictly (a file name falls back to Latin-1),
    so a surrogate written as raw bytes never becomes one; an escape stays text."""
    raw_bytes = "x\ud800".encode("utf-8", "surrogatepass")
    boundary = b"b0undary"
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="field"',
        b"",
        b"\\ud800 as text",
        b"--" + boundary,
        b'Content-Disposition: form-data; name="upload"; filename="f' + raw_bytes + b'.txt"',
        b"Content-Type: text/plain",
        b"",
        b'{"a": "\\ud800"}',
        b"--" + boundary + b"--",
        b"",
    ]
    headers = {"content-type": "multipart/form-data; boundary=" + boundary.decode()}
    async with _client(_open_app()) as client:
        resp = await client.post("/form", content=b"\r\n".join(parts), headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["field"] == "\\ud800 as text"
    assert not lone_surrogate_locs(resp.json()["filename"])


async def test_a_query_path_header_cookie_and_urlencoded_form_cannot_carry_one():
    """Percent-decoding replaces an invalid UTF-8 sequence, and headers and cookies are
    Latin-1, so none of these can hold a lone surrogate."""
    async with _client(_open_app()) as client:
        query = await client.get(
            "/q/a%ED%A0%80b?q=%ED%A0%80",
            headers={"x-probe": b"\xed\xa0\x80", "cookie": 'c="\\355\\240\\200"'},
        )
        form = await client.post(
            "/urlencoded",
            content=b"field=%ED%A0%80",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert query.json() == {"has_surrogate": False}
    assert form.json() == {"has_surrogate": False}


async def _drive(app, messages: list[dict], app_reads: int = 1) -> list[dict]:
    """Run the middleware around `app` with a scripted `receive`; return what was sent."""
    sent: list[dict] = []
    queue = list(messages)

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RefuseLoneSurrogates(app)(scope, receive, send)
    return sent


async def test_a_body_split_across_messages_is_checked_whole():
    """An escape cut between two `http.request` messages is still found."""
    seen = {}

    async def app(scope, receive, send):
        try:
            await receive()
        except HTTPException as exc:
            seen["detail"] = exc.detail

    await _drive(
        app,
        [
            {"type": "http.request", "body": b'{"a": "x\\ud8', "more_body": True},
            {"type": "http.request", "body": b'00"}', "more_body": False},
        ],
    )
    assert seen["detail"] == [{**UNICODE, "loc": ["body", "a"]}]


async def test_a_clean_body_is_handed_on_as_one_message_and_later_reads_pass_through():
    got = []

    async def app(scope, receive, send):
        got.append(await receive())
        got.append(await receive())

    await _drive(
        app,
        [
            {"type": "http.request", "body": b'{"a": ', "more_body": True},
            {"type": "http.request", "body": b'"b"}', "more_body": False},
        ],
    )
    assert got == [
        {"type": "http.request", "body": b'{"a": "b"}', "more_body": False},
        {"type": "http.disconnect"},
    ]


async def test_a_read_after_the_response_started_is_not_refused():
    """Starlette's disconnect listener can make the first read after a route that never
    read its body has started answering; raising then would break that response."""
    got = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        got.append(await receive())
        await send({"type": "http.response.body", "body": b""})

    sent = await _drive(app, [{"type": "http.request", "body": _esc({"a": SUR})}])
    assert got[0]["body"] == _esc({"a": SUR})
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


async def test_a_multipart_body_is_handed_on_as_it_arrives_unread():
    """An upload is streamed to the app chunk by chunk, never buffered whole by the
    check, and is never refused by it."""
    got = []
    sent: list[dict] = []
    chunks = [
        {"type": "http.request", "body": b'--b\r\n{"a": "\\ud800"}', "more_body": True},
        {"type": "http.request", "body": b"\r\n--b--\r\n", "more_body": False},
    ]
    queue = list(chunks)

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        got.append(await receive())
        got.append(await receive())

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"Multipart/Form-Data; boundary=b")],
    }
    await RefuseLoneSurrogates(app)(scope, receive, send)
    assert got == chunks


async def test_a_disconnect_while_reading_is_handed_on():
    got = []

    async def app(scope, receive, send):
        got.append(await receive())

    await _drive(app, [{"type": "http.request", "body": b"{", "more_body": True}])
    assert got == [{"type": "http.disconnect"}]


async def test_a_websocket_or_lifespan_scope_is_passed_through_untouched():
    calls = []

    async def app(scope, receive, send):
        calls.append(receive)

    async def receive():
        return {"type": "lifespan.startup"}

    await RefuseLoneSurrogates(app)({"type": "lifespan"}, receive, None)
    assert calls == [receive]


def test_install_adds_the_check_unless_told_not_to():
    def middleware(app):
        return [m.cls for m in app.user_middleware]

    assert middleware(_open_app()) == [RefuseLoneSurrogates]
    assert middleware(_open_app(refuse=False)) == []


class _Logger(BaseHTTPMiddleware):
    """Stands in for herd_common.logging.RequestLoggingMiddleware, a BaseHTTPMiddleware,
    which runs the `receive` it hands on in a task group."""

    async def dispatch(self, request, call_next):
        return await call_next(request)


@pytest.mark.parametrize("order", ["logger_first", "logger_after"])
async def test_the_refusal_survives_a_base_http_middleware_in_either_order(order):
    """Installed outermost, the refusal reached FastAPI's body reader inside an
    ExceptionGroup and became 400 `There was an error parsing the body`."""
    app = FastAPI()
    if order == "logger_first":
        app.add_middleware(_Logger)
    install_validation_errors(app)
    if order == "logger_after":
        app.add_middleware(_Logger)

    @app.post("/m")
    async def m(body: dict[str, str]):
        return body

    async with _client(app) as client:
        refused = await client.post("/m", content=_esc({"k": SUR}), headers=_JSON)
        honest = await client.post("/m", content=_esc({"k": PAIR}), headers=_JSON)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == [{**UNICODE, "loc": ["body", "k"]}]
    assert honest.json() == {"k": PAIR}
    assert [m.cls for m in app.user_middleware][-1] is RefuseLoneSurrogates


async def test_install_after_the_app_started_is_refused():
    app = _open_app(refuse=False)
    async with _client(app) as client:
        await client.post("/nobody")
    with pytest.raises(RuntimeError):
        install_validation_errors(app)
