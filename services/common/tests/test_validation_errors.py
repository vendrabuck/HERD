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
    LONE_SURROGATE_BODY_ERROR,
    RENDERED_KEYS,
    STRING_UNICODE_MSG,
    STRING_UNICODE_TYPE,
    RefuseLoneSurrogates,
    _raw_text_holds_a_lone_surrogate,
    holds_lone_surrogate,
    install_validation_errors,
    render_validation_errors,
    restrict_validation_error_schema,
    validation_error_handler,
)
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from tests._cost import cpu_ratio, growth_ratios

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
        {"type": "string_unicode", "loc": ["body"], "msg": STRING_UNICODE_MSG}
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
ONE_ITEM = [{"type": "string_unicode", "loc": ["body"], "msg": STRING_UNICODE_MSG}]
_JSON = {"content-type": "application/json"}


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
    assert LONE_SURROGATE_BODY_ERROR == ONE_ITEM[0]


@pytest.mark.parametrize(
    ("body", "refused"),
    [
        (b'{"a": "x\\ud800"}', True),
        (b'{"a": "x\\uDBFF"}', True),
        (b'{"a": "\\udc00x"}', True),
        (b'{"a": "x\\udE00"}', True),  # a hex digit in either case
        (b'{"\\udfff": 1}', True),
        (b'"\\ud800"', True),
        (b'{"a": [1, {"b": "\\ud800"}]}', True),
        # a surrogate written as raw UTF-8 bytes: json.loads decodes with surrogatepass
        ('{"a": "x\ud800"}'.encode("utf-8", "surrogatepass"), True),
        # a UTF-16 document, which JSON detects from its NUL bytes
        ('{"a": "x\ud800"}'.encode("utf-16-le", "surrogatepass"), True),
        ('{"a": "x\ud800\u0090"}'.encode("utf-16-le", "surrogatepass"), True),
        ('{"a": "\\ud83d\\ude00"}'.encode("utf-16"), False),
        # a pair is one code point, but only when both halves are real escapes
        (b'{"a": "\\\\ud83d\\ude00"}', True),
        (b'{"a": "\\ud83d\\\\ude00"}', True),
        (b'{"a": "\\ud83d\\ud83d\\ude00"}', True),
        (b'{"a": "\\ud83d\\ude00\\ude00"}', True),
        (b'{"a": "\\n\\"\\u0041\\udc00"}', True),
        (b'{"a": "\\ud800\\udc00\\n", "b": "x\\udfff"}', True),
        # a member a later duplicate key replaces is refused all the same
        (b'{"a": "\\ud800", "a": "ok"}', True),
        # an escaped backslash followed by text that reads like an escape
        (b'{"a": "\\\\ud800"}', False),
        (b'{"a": "\\\\\\ud800"}', True),  # a backslash pair, then a real escape
        (b'{"a": "\\\\\\\\ud800"}', False),
        (b'{"a": "\\ud83d\\udd11"}', False),
        (b'{"a": "\\uD83D\\uDD11"}', False),
        (b'{"a": "\\ud55c \\uD7A3 \\ufffd \\uFFFD \\ue000"}', False),  # near, not surrogates
        ('{"a": "한"}'.encode(), False),
        (b"\xef\xbb\xbf" + b'{"a": "\\ud800"}', True),
        # not JSON: decided from the text alone, never parsed (OPS-VALID-6)
        (b'{"a": "x\\ud800"', True),
        (b"a=\\ud800", True),
        (b'{"a": "x"', False),
        (b"", False),
    ],
    ids=[
        "value",
        "uppercase_hex",
        "low_first",
        "mixed_case_low",
        "key",
        "bare_string",
        "nested",
        "raw_utf8_bytes",
        "utf16",
        "utf16_valid_utf8_lookalike",
        "utf16_pair",
        "escaped_backslash_then_pair",
        "pair_then_escaped_backslash",
        "high_high_low",
        "high_low_low",
        "low_after_other_escapes",
        "pair_then_lone_low_elsewhere",
        "duplicate_key",
        "escaped_backslash",
        "backslash_pair_then_escape",
        "two_backslash_pairs",
        "valid_pair",
        "valid_pair_uppercase",
        "near_escapes",
        "hangul_raw",
        "utf8_bom",
        "malformed_json",
        "not_json",
        "malformed_clean",
        "empty",
    ],
)
def test_the_raw_scan(body, refused):
    assert _raw_text_holds_a_lone_surrogate(body) is refused


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": ["x", {"b": "y" + SUR}]}, True),
        ({"k" + LOW: 1}, True),
        ({"a": [PAIR, "café", 1, None]}, False),
        ({1, 2}, False),  # not JSON data: walked, nothing found
        ([0] * 40 + [{"x": [type("S", (str,), {})("x" + SUR)]}], True),  # a str subclass
    ],
    ids=["nested_value", "key", "clean", "not_json", "str_subclass"],
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
        return {"length": len(await request.body())}

    @app.post("/nobody")
    async def nobody():
        return {"ok": True}

    @app.post("/form")
    async def form(field: str = Form(...), upload: UploadFile = File(...)):
        return {"field": field, "filename": upload.filename}

    @app.post("/urlencoded")
    async def urlencoded(field: str = Form(...)):
        return {"has_surrogate": holds_lone_surrogate(field)}

    @app.get("/q/{item}")
    async def query(item: str, q: str = "", request: Request = None):  # type: ignore[assignment]
        texts = [item, q, request.headers.get("x-probe", ""), request.cookies.get("c", "")]
        return {"has_surrogate": holds_lone_surrogate(texts)}

    def deny():
        raise HTTPException(status_code=401, detail="Not authenticated")

    @app.post("/guarded", dependencies=[Depends(deny)])
    async def guarded(body: _Strict):
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/str", {"s": "a" + SUR}),
        ("/map", {"k": "v" + SUR}),
        ("/map", {"k" + LOW: "v"}),
        ("/any", {"a": [{"b": SUR}]}),
        ("/raw", [SUR, "ok", LOW]),
    ],
    ids=["plain_str", "mapping_value", "mapping_key", "any_nested", "raw_request_body"],
)
async def test_a_lone_surrogate_anywhere_in_a_json_body_is_the_one_item(path, body):
    async with _client(_open_app()) as client:
        resp = await client.post(path, content=_esc(body), headers=_JSON)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": ONE_ITEM}


async def test_the_answer_is_these_exact_bytes():
    async with _client(_open_app()) as client:
        resp = await client.post("/str", content=_esc({"s": SUR}), headers=_JSON)
    assert resp.content == (
        b'{"detail":[{"type":"string_unicode","loc":["body"],"msg":"Input should be a valid '
        b'string, unable to parse raw data as a unicode string"}]}'
    )
    assert len(resp.content) == 138
    assert resp.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "Application/JSON",
        "application/problem+json",
        "application/vnd.herd.v1+json",
    ],
)
async def test_every_json_content_type_is_scanned(content_type):
    async with _client(_open_app()) as client:
        resp = await client.post(
            "/raw", content=_esc([SUR]), headers={"content-type": content_type}
        )
    assert (resp.status_code, resp.json()) == (422, {"detail": ONE_ITEM})


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/plain"},
        {"content-type": "application/x-www-form-urlencoded"},
        {"content-type": "application/octet-stream"},
        {"content-type": "application/jsonl"},
        {"content-type": "text/json"},
        {},
    ],
    ids=["text_plain", "urlencoded", "octet_stream", "jsonl", "text_json", "none"],
)
async def test_a_body_fastapi_does_not_parse_as_json_is_not_scanned(headers):
    """FastAPI parses a body as JSON only for `application/json` or an
    `application/*+json` subtype (and, with `strict_content_type` on, not with no
    content type at all); any other body reaches the route as it was sent."""
    body = _esc([SUR])
    async with _client(_open_app()) as client:
        resp = await client.post("/raw", content=body, headers=headers)
    assert (resp.status_code, resp.json()) == (200, {"length": len(body)})


async def test_without_the_check_those_bodies_reach_the_route():
    """The check is what refuses them: each type accepts a lone surrogate (mutation
    proof for the middleware)."""
    async with _client(_open_app(refuse=False)) as client:
        assert (await client.post("/str", content=_esc({"s": SUR}), headers=_JSON)).json() == {
            "len": 1
        }
        resp = await client.post("/map", content=_esc({"k" + LOW: "v"}), headers=_JSON)
        assert resp.json() == {"keys": 1}


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


async def test_a_body_that_is_not_json_but_holds_a_surrogate_escape_is_the_one_item():
    """By decision (round 4): the check never parses, so a malformed body holding a
    surrogate escape gets this 422 instead of the route's own JSON error; a malformed
    body without one still gets FastAPI's `json_invalid`."""
    async with _client(_open_app()) as client:
        refused = await client.post("/str", content=b'{"s": "\\ud800', headers=_JSON)
        malformed = await client.post("/str", content=b'{"s": "x', headers=_JSON)
    assert (refused.status_code, refused.json()) == (422, {"detail": ONE_ITEM})
    assert malformed.json()["detail"][0]["type"] == "json_invalid"


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
    assert not holds_lone_surrogate(resp.json()["filename"])


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


async def _drive(app, messages: list[dict], headers: list | None = None) -> list[dict]:
    """Run the middleware around `app` with a scripted `receive`; return what was sent."""
    sent: list[dict] = []
    queue = list(messages)

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    if headers is None:
        headers = [(b"content-type", b"application/json")]
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
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
    assert seen["detail"] == ONE_ITEM


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


@pytest.mark.parametrize(
    "content_type",
    [
        b"multipart/form-data; boundary=b",
        b"Multipart/Form-Data; boundary=b",
        b"text/plain",
        b"application/x-www-form-urlencoded",
        b"application/octet-stream",
        None,
    ],
    ids=["multipart", "multipart_mixed_case", "text_plain", "urlencoded", "octet_stream", "none"],
)
async def test_a_body_not_parsed_as_json_is_handed_on_as_it_arrives_unread(content_type):
    """Chunk by chunk, as the server sent it: never buffered whole, never refused."""
    got = []
    chunks = [
        {"type": "http.request", "body": b'["\\ud800",', "more_body": True},
        {"type": "http.request", "body": b'"\\udc00"]', "more_body": False},
    ]

    async def app(scope, receive, send):
        got.append(await receive())
        got.append(await receive())

    headers = [] if content_type is None else [(b"content-type", content_type)]
    await _drive(app, list(chunks), headers=headers)
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
    assert refused.json()["detail"] == ONE_ITEM
    assert honest.json() == {"k": PAIR}
    assert [m.cls for m in app.user_middleware][-1] is RefuseLoneSurrogates


async def test_install_after_the_app_started_is_refused():
    app = _open_app(refuse=False)
    async with _client(app) as client:
        await client.post("/nobody")
    with pytest.raises(RuntimeError):
        install_validation_errors(app)


# -- The cost: one scan of the raw bytes (round 4 of the #1456 review) ----------------
#
# Every time is CPU time read through `tests/_cost.py` (#1477), and every bound is
# relative, with margin over the worst measurement, and re-measured once before it
# fails. Bounds: the escape-heavy scan against itself at 4 and 16 times the size
# (linear work) and against one `bytes.lower` pass per shape (a constant factor); the
# structural scan and the parsed-value check against the parse they stand beside; the
# no-marker exit against one `bytes.lower` pass; a refused request against the same
# request without the check.

_MB = 1 << 20
# Each fourfold step in size may cost at most this many times the one before: linear
# work costs about 4, a dominant quadratic about 16 (`tests/_cost.py` says what else it
# does and does not catch).
_GROWTH_BOUND = 8.0


def _fill(unit: str, head: str, tail: str, size: int = _MB) -> bytes:
    """`unit` repeated to about `size` bytes between `head` and `tail`."""
    return (head + unit * ((size - len(head) - len(tail)) // len(unit)) + tail).encode()


def _peak(fn) -> int:
    """Peak bytes allocated while `fn` runs, as tracemalloc sees it."""
    import tracemalloc

    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


_LONE_TEXT = '"\\ud800"'
_BS = "\\"
# Structural bodies, a lone surrogate at the end: millions of elements, cheap to scan.
_STRUCTURAL = {
    "chain900": lambda: _fill("[" * 900 + "]" * 900 + ",", '{"name":"a","z":[', _LONE_TEXT + "]}"),
    "empty_lists": lambda: _fill("[],", '{"name":"a","z":[', _LONE_TEXT + "]}"),
    "triples": lambda: _fill("[[[]]],", '{"name":"a","z":[', _LONE_TEXT + "]}"),
    "nested_dicts": lambda: _fill('{"a":{"b":1}},', "[", _LONE_TEXT + "]"),
}
# Escape-heavy string bodies, the scan's own worst: json.loads decodes them fast. Each
# takes the body's size, so the cost test can grow it.
_ESCAPE_HEAVY = {
    "pairs": lambda size=_MB: _fill(_BS + "ud83d" + _BS + "ude00", '{"name":"', '"}', size),
    "pairs_upper_then_low": lambda size=_MB: _fill(
        _BS + "uD83D" + _BS + "uDE00", '{"name":"', _BS + 'udc00"}', size
    ),
    "backslash_pairs_then_low": lambda size=_MB: _fill(_BS * 2, '{"name":"', _BS + 'udc00"}', size),
    "u0041_then_low": lambda size=_MB: _fill(_BS + "u0041", '{"name":"', _BS + 'udc00"}', size),
    "four_backslashes_and_text": lambda size=_MB: _fill(_BS * 4 + "ude00", '{"name":"', '"}', size),
}
# Per shape, the worst CPU-time ratio of the scan of the 1 MB body to one `bytes.lower`
# pass over it, measured in #1477 (PROVISIONAL, development host only: AMD Threadripper
# PRO 5955WX; 20 to 50 rounds in each of unpinned, `taskset -c 0`, pinned beside CPU
# hogs, under coverage, and beside a spinning or memory-bound process on the SMT
# sibling core). The bound is _PASS_MARGIN times it, so a scan that does more than
# about twice its usual work on that shape fails. The ratio differs per shape (by its
# escape density) far more than per run, which is why each shape has its own bound: one
# bound for all of them would let the cheapest shape slow down sevenfold.
_ESCAPE_PASSES_WORST = {
    "pairs": 27.0,
    "pairs_upper_then_low": 25.7,
    "backslash_pairs_then_low": 22.6,
    "u0041_then_low": 10.7,
    "four_backslashes_and_text": 20.9,
}
_PASS_MARGIN = 2.0


@pytest.mark.parametrize("make", _STRUCTURAL.values(), ids=_STRUCTURAL.keys())
def test_a_structural_body_costs_a_small_part_of_its_parse(make):
    """The body is refused without a parse: the scan costs under a fifth of what
    `json.loads` of it costs, and its memory does not grow with the millions of
    elements (at most two copies of the bytes). The comparison with the parse is the
    property. The costliest shapes to scan (`empty_lists`, `nested_dicts`) measured
    about 0.07 at worst, so the bound catches a scan about three times slower."""
    body = make()
    assert _raw_text_holds_a_lone_surrogate(body) is True
    ratio = cpu_ratio(
        lambda: _raw_text_holds_a_lone_surrogate(body), lambda: json.loads(body), bound=0.2
    )
    assert ratio < 0.2
    assert _peak(lambda: _raw_text_holds_a_lone_surrogate(body)) < 2.2 * len(body)


@pytest.mark.parametrize("shape", _ESCAPE_HEAVY)
def test_an_escape_heavy_body_costs_linear_work_in_two_copies(shape):
    """A body of nothing but escapes is the scan's worst: a few C passes (two copies, a
    search, eight counts). Three bounds, none of which the others replace:

    - growth: each fourfold step in size costs at most `_GROWTH_BOUND` times the one
      before, so the cost is linear (a quadratic scan fails);
    - passes: the scan of the 1 MB body costs under `_PASS_MARGIN` times the worst ratio
      to one `bytes.lower` pass measured for this shape, so a linear scan that does
      about twice its work, or one whose cost follows the number of escapes, fails;
    - memory: at most two copies of the bytes.

    These replaced a bound of 12 times `json.loads` of the same body (#1477), which a
    wall clock read at 18.5 on a shared CI runner: `json.loads` runs at its own speed
    on each CPU, while `bytes.lower` is one plain C pass like the scan's own."""
    make = _ESCAPE_HEAVY[shape]
    n, times, ratios = growth_ratios(
        make, _raw_text_holds_a_lone_surrogate, bound=_GROWTH_BOUND, start=64 * 1024
    )
    assert max(ratios) <= _GROWTH_BOUND, (n, times, ratios)
    body = make()
    bound = _PASS_MARGIN * _ESCAPE_PASSES_WORST[shape]
    passes = cpu_ratio(
        lambda: _raw_text_holds_a_lone_surrogate(body), lambda: body.lower(), bound=bound
    )
    assert passes < bound, (shape, passes)
    assert _peak(lambda: _raw_text_holds_a_lone_surrogate(body)) < 2.2 * len(body)


@pytest.mark.parametrize(
    "body",
    [
        _fill('{"name":"device-1","rack":"r1"},', "[", "0]"),
        _fill("日本", '["', '"]'),
        _fill('"' + "\\n" * 8 + '",', "[", "0]"),
    ],
    ids=["ascii_objects", "cjk_text", "other_escapes_only"],
)
def test_a_body_with_no_surrogate_marker_is_passed_after_two_searches(body):
    """No `\\u` and no 0xED byte: two `in` searches and no copy (the early exit).

    The memory bound is what proves the exit: without it the scan copies the body.
    The time bound is loose on purpose (two substring searches cost about one
    `bytes.lower` pass, and a 1x wall-clock bound failed once on a CI runner under
    coverage: 0.82 ms against 0.74 ms); at 4 it sits over three times above the worst
    ratio measured (about 1.1) and catches a scan that does several passes."""
    assert _raw_text_holds_a_lone_surrogate(body) is False
    assert _peak(lambda: _raw_text_holds_a_lone_surrogate(body)) < 64 * 1024
    ratio = cpu_ratio(lambda: _raw_text_holds_a_lone_surrogate(body), lambda: body.lower(), bound=4)
    assert ratio < 4


def test_through_an_app_a_refused_structural_body_costs_less_than_without_the_check():
    """A route behind a failing dependency, with no credentials: without the check
    FastAPI parses the body and answers 401; with it the body is refused (422) after
    the scan alone, which costs less than that parse (under a twentieth of it in every
    measurement, so this bound only catches a check that parses or walks the body)."""
    import asyncio

    body = _STRUCTURAL["chain900"]()
    apps = {refuse: _open_app(refuse=refuse) for refuse in (False, True)}
    statuses: dict[bool, set[int]] = {False: set(), True: set()}

    def request(refuse: bool) -> None:
        async def send() -> None:
            async with _client(apps[refuse]) as client:
                resp = await client.post("/guarded", content=body, headers=_JSON)
                statuses[refuse].add(resp.status_code)

        asyncio.run(send())

    ratio = cpu_ratio(lambda: request(True), lambda: request(False), bound=1.0, repeats=3)
    assert (statuses[False], statuses[True]) == ({401}, {422})
    assert ratio < 1.0


def test_holds_lone_surrogate_costs_about_one_serialization():
    """The callers that test a PARSED value (the import checks, secrets' reveal,
    config's save): one serialization in C, peak memory under three times the parse's
    own, time under three times the parse's (about 1.2 typically, 1.9 the worst single
    measurement in the #1477 review), which catches a check about twice as costly, and
    the Python walk the check falls back to."""
    deep_lone = b"[" * 600 + b"0," * 300_000 + b'"\\ud800"' + b"]" * 600
    deep_pair = b"[" * 600 + b'"\\ud83d\\ude00",' + b"0," * 300_000 + b"0" + b"]" * 600
    for body, expected in ((deep_lone, True), (deep_pair, False)):
        parsed = json.loads(body)
        assert holds_lone_surrogate(parsed) is expected

        def check(parsed=parsed):
            return holds_lone_surrogate(parsed)

        def parse(body=body):
            return json.loads(body)

        assert _peak(check) < 3 * _peak(parse)
        assert cpu_ratio(check, parse, bound=3) < 3
