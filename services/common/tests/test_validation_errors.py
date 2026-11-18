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
    MAX_REPORTED_LOCATIONS,
    RENDERED_KEYS,
    STRING_UNICODE_MSG,
    STRING_UNICODE_TYPE,
    RefuseLoneSurrogates,
    body_lone_surrogate_errors,
    holds_lone_surrogate,
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
        # a pair is one code point, but only when both halves are real escapes
        (b'{"a": "\\\\ud83d\\ude00"}', [["body", "a"]]),
        (b'{"a": "\\ud83d\\\\ude00"}', [["body", "a"]]),
        (b'{"a": "\\ud83d\\ud83d\\ude00"}', [["body", "a"]]),
        (b'{"a": "\\ud83d\\ude00\\ude00"}', [["body", "a"]]),
        # a member a later duplicate key replaces is refused all the same, at the body
        (b'{"a": "\\ud800", "a": "ok"}', [["body"]]),
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
        "escaped_backslash_then_pair",
        "pair_then_escaped_backslash",
        "high_high_low",
        "high_low_low",
        "duplicate_key",
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


# -- At most ten locations, and a cost that does not explode (review of #1456) --------


def test_a_refusal_reports_the_first_ten_locations_only():
    """A body of many lone surrogates was answered with one item each: a 10 MB body
    gave 1,111,111 items and a 155.6 MB 422. The search stops at the tenth."""
    assert MAX_REPORTED_LOCATIONS == 10
    errors = body_lone_surrogate_errors(_esc([SUR] * 25))
    assert errors == [{**UNICODE, "loc": ["body", i]} for i in range(10)]


def test_the_locator_stops_at_its_limit_and_keeps_document_order():
    value = {"a": [SUR, "ok", SUR], "b" + LOW: 1, "c": {"d": SUR}}
    assert lone_surrogate_locs(value) == [("a", 0), ("a", 2), (), ("c", "d")]
    assert lone_surrogate_locs(value, limit=2) == [("a", 0), ("a", 2)]
    assert lone_surrogate_locs(value, limit=1) == [("a", 0)]


_WIDE = 40  # a wide container


@pytest.mark.parametrize(
    ("value", "locs"),
    [
        ([0] * _WIDE + [[1, SUR]], [(_WIDE, 1)]),  # numbers skipped, a nested hit found
        (["ok"] * _WIDE + [None, SUR, "ok", SUR], [(_WIDE + 1,), (_WIDE + 3,)]),
        ({**{f"k{i}": i for i in range(_WIDE)}, "z" + SUR: {"y": SUR}}, [()]),
        ({**{f"k{i}": "ok" for i in range(_WIDE)}, "z": [SUR]}, [("z", 0)]),
        ([1.5, True, None, "ok"] * _WIDE, []),
        # a str subclass is not what json.loads makes: every child is looked at
        ([0] * _WIDE + [type("S", (str,), {})("x" + SUR)], [(_WIDE,)]),
    ],
    ids=["numbers", "strings", "key", "dict_values", "clean", "str_subclass"],
)
def test_lone_surrogate_locs_in_large_containers(value, locs):
    assert lone_surrogate_locs(value) == locs


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": ["x", {"b": "y" + SUR}]}, True),
        ({"k" + LOW: 1}, True),
        ({"a": [PAIR, "café", 1, None]}, False),
        ({1, 2}, False),  # not JSON data: searched by the locator, which finds nothing
    ],
    ids=["nested_value", "key", "clean", "not_json"],
)
def test_holds_lone_surrogate(value, expected):
    assert holds_lone_surrogate(value) is expected


def test_holds_lone_surrogate_on_a_value_too_deep_to_serialize():
    for leaf, expected in ((SUR, True), ("ok", False)):
        deep: object = leaf
        for _ in range(10_000):
            deep = [deep]
        with pytest.raises(RecursionError):
            json.dumps(deep)
        assert holds_lone_surrogate(deep) is expected


# The reviewer's two attack bodies, scaled to about 1 MB, plus the deep-and-wide body
# that must be parsed: before the fix the locator built a location tuple per element,
# so memory grew with elements times depth (1,422 MB for the first) and time with it.
_DEEP_PAIR = b"[" * 600 + b'"\\ud83d\\ude00",' + b"0," * 300_000 + b"0" + b"]" * 600
_FLAT_PAIR = b'["\\ud83d\\ude00",' + b"0," * 500_000 + b"0]"
_DEEP_LONE = b"[" * 600 + b"0," * 300_000 + b'"\\ud800"' + b"]" * 600


def _peak(fn) -> int:
    """Peak bytes allocated while `fn` runs, as tracemalloc sees it."""
    import tracemalloc

    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _best_time(fn, runs: int = 3) -> float:
    """The fastest of `runs` timings: robust to a busy host and to a single core."""
    import time

    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("body", [_DEEP_PAIR, _FLAT_PAIR], ids=["deep_pair", "flat_pair"])
def test_a_body_that_holds_no_lone_surrogate_is_never_parsed(body):
    """A valid pair anywhere was enough to make the check parse the body and walk it.
    It is now decided from the raw text: no parse, no allocation that grows with the
    body, and less time than the one parse FastAPI does anyway (bounds are relative, so
    they hold pinned to one core and on a fast host)."""
    assert body_lone_surrogate_errors(body) == []
    assert _peak(lambda: body_lone_surrogate_errors(body)) < 64 * 1024
    assert _best_time(lambda: body_lone_surrogate_errors(body)) < _best_time(
        lambda: json.loads(body)
    )


def _fill(unit: str, head: str, tail: str, size: int = 1 << 20) -> bytes:
    """`unit` repeated to about `size` bytes between `head` and `tail`."""
    return (head + unit * ((size - len(head) - len(tail)) // len(unit)) + tail).encode()


_LONE_TEXT = '"\\ud800"'
# The second review's shapes (round 3): each about 1 MB, the surrogate where the search
# reaches it last or never. Built on demand, so collecting the tests stays cheap.
_REFUSED_SHAPES = {
    "chain900": lambda: _fill("[" * 900 + "]" * 900 + ",", '{"name":"a","z":[', _LONE_TEXT + "]}"),
    "empty_lists": lambda: _fill("[],", '{"name":"a","z":[', _LONE_TEXT + "]}"),
    "triples": lambda: _fill("[[[]]],", '{"name":"a","z":[', _LONE_TEXT + "]}"),
    "empty_objects": lambda: _fill("{},", "[", _LONE_TEXT + "]"),
    "duplicate_key_chain900": lambda: _fill(
        "[" * 900 + "]" * 900 + ",", '{"name":' + _LONE_TEXT + ',"name":"a","z":[', "0]}"
    ),
    "duplicate_key_empty_lists": lambda: _fill(
        "[],", '{"name":' + _LONE_TEXT + ',"name":"a","z":[', "0]}"
    ),
    "deep600_wide": lambda: _DEEP_LONE,
    "nested_dicts": lambda: (
        b"{"
        + b",".join(b'"k%d":{"a":{"b":1}}' % i for i in range((1 << 20) // 22))
        + b',"z":"\\ud800"}'
    ),
}


@pytest.mark.parametrize("make", _REFUSED_SHAPES.values(), ids=_REFUSED_SHAPES.keys())
def test_a_refused_body_costs_at_most_about_one_more_parse(make):
    """A body the scan says holds one is parsed once and refused; the location search
    is bounded by its step budget and runs with the collector paused. So the whole
    check, parse included, costs under twice the parse alone (measured 1.05 to 1.5),
    where the walk it replaced cost 3 to 8 times, and its peak memory is the parse's
    own plus a fixed margin. The route then never parses the body."""
    body = make()
    assert body_lone_surrogate_errors(body)[0]["type"] == "string_unicode"

    def check(body=body):
        return body_lone_surrogate_errors(body)

    def parse(body=body):
        return json.loads(body)

    assert _best_time(check) < 2 * _best_time(parse)
    assert _peak(check) < _peak(parse) + 1024 * 1024


def test_a_body_whose_only_surrogate_a_duplicate_key_drops_is_refused_at_the_body():
    """Decision (round 3): a surrogate escape in a member a later duplicate key
    replaces is never honest input, so the body is refused without a second search
    and without passing it on to be parsed again."""
    body = b'{"name": "\\ud800", "name": "a"}'
    assert json.loads(body) == {"name": "a"}
    assert body_lone_surrogate_errors(body) == [{**UNICODE, "loc": ["body"]}]


def test_the_search_is_bounded_by_its_budget():
    from herd_common.validation_errors import LOCATION_SEARCH_BUDGET

    early = _esc([SUR] + [0] * (LOCATION_SEARCH_BUDGET + 5))
    late = _esc([0] * (LOCATION_SEARCH_BUDGET + 5) + [SUR])
    assert body_lone_surrogate_errors(early) == [{**UNICODE, "loc": ["body", 0]}]
    assert body_lone_surrogate_errors(late) == [{**UNICODE, "loc": ["body"]}]
    value = json.loads(late)
    assert lone_surrogate_locs(value, budget=LOCATION_SEARCH_BUDGET) == []
    assert lone_surrogate_locs(value) == [(LOCATION_SEARCH_BUDGET + 5,)]


def test_the_answer_is_bounded_in_size_and_never_cuts_a_key():
    """`loc` repeats the path for each reported item, so a long key made the answer
    ten times the body (a 16 MB key gave a 160 MB 422). A location whose parts pass
    MAX_LOC_CHARS is left out whole, and all of them stay within MAX_LOCS_CHARS."""
    from herd_common.validation_errors import MAX_LOC_CHARS, MAX_LOCS_CHARS

    long_key = "k" * (1 << 20)
    body = _esc({"name": "a", long_key: [SUR] * 10})
    errors = body_lone_surrogate_errors(body)
    assert errors == [{**UNICODE, "loc": ["body"]}]
    assert len(json.dumps({"detail": errors})) < 300
    fits = "k" * (MAX_LOC_CHARS - 2)  # the key plus a one-digit index: MAX_LOC_CHARS - 1
    errors = body_lone_surrogate_errors(_esc({fits: [SUR] * 9, "short": SUR}))
    locs = [error["loc"][1:] for error in errors]
    assert all(loc[0] in (fits, "short") for loc in locs)  # each key whole
    assert sum(len(str(part)) for loc in locs for part in loc) <= MAX_LOCS_CHARS
    # eight long items fill MAX_LOCS_CHARS, the ninth is left out, "short" still fits
    assert len(locs) == 9 and locs[-1] == ["short"]
    assert len(json.dumps({"detail": errors})) < MAX_LOCS_CHARS + 10 * 200


def test_the_locator_holds_memory_for_the_depth_not_the_elements():
    parsed = json.loads(_DEEP_LONE)
    assert _peak(lambda: lone_surrogate_locs(parsed)) < 256 * 1024


def test_a_body_of_many_lone_surrogates_stops_at_the_tenth():
    body = _esc([SUR] * 200_000)
    parse_peak = _peak(lambda: json.loads(body))
    assert len(body_lone_surrogate_errors(body)) == 10
    assert _peak(lambda: body_lone_surrogate_errors(body)) < parse_peak + 1024 * 1024
    assert _best_time(lambda: body_lone_surrogate_errors(body)) < 2 * _best_time(
        lambda: json.loads(body)
    )


def test_holds_lone_surrogate_costs_about_one_serialization():
    """The other callers (the import checks, secrets' reveal, config's save) test a
    parsed value: one serialization in C, so its peak memory is under three times the
    parse's own and its time a small multiple of the parse's, at any depth (before,
    the locator's per-element tuples made both grow with elements times depth)."""
    for body, expected in ((_DEEP_LONE, True), (_DEEP_PAIR, False)):
        parsed = json.loads(body)
        assert holds_lone_surrogate(parsed) is expected

        def check(parsed=parsed):
            return holds_lone_surrogate(parsed)

        def parse(body=body):
            return json.loads(body)

        assert _peak(check) < 3 * _peak(parse)
        assert _best_time(check) < 3 * _best_time(parse)


def test_through_an_app_the_check_adds_nothing_to_an_unauthenticated_request():
    """The reviewer's measurement on the real acl app, in miniature: a route behind a
    failing dependency, sent the deep pair body with no credentials. With the check the
    request costs what it costs without it, and still answers the dependency's 401."""
    import asyncio

    def peak_of(refuse: bool) -> tuple[int, int]:
        app = _open_app(refuse=refuse)

        async def send() -> int:
            async with _client(app) as client:
                resp = await client.post("/guarded", content=_DEEP_PAIR, headers=_JSON)
                return resp.status_code

        status: list[int] = []
        peak = _peak(lambda: status.append(asyncio.run(send())))
        return status[0], peak

    without_status, without_peak = peak_of(False)
    with_status, with_peak = peak_of(True)
    assert without_status == with_status == 401
    assert with_peak < without_peak + 1024 * 1024


@pytest.mark.parametrize("shape", ["chain900", "duplicate_key_chain900", "empty_lists"])
def test_through_an_app_a_refused_body_costs_about_what_main_pays(shape):
    """A route behind a failing dependency, sent the review's shape with no credentials:
    without the check FastAPI parses the body and answers 401; with it the check parses
    it and answers 422. The request costs under twice as much."""
    import asyncio
    import time

    body = _REFUSED_SHAPES[shape]()

    def timed(refuse: bool) -> tuple[float, int]:
        app = _open_app(refuse=refuse)

        async def send() -> int:
            async with _client(app) as client:
                resp = await client.post("/guarded", content=body, headers=_JSON)
                return resp.status_code

        best, status = float("inf"), 0
        for _ in range(3):
            start = time.perf_counter()
            status = asyncio.run(send())
            best = min(best, time.perf_counter() - start)
        return best, status

    main_time, main_status = timed(False)
    branch_time, branch_status = timed(True)
    assert (main_status, branch_status) == (401, 422)
    assert branch_time < 2 * main_time
