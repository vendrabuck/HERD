"""The ONE request-validation (422) handler every HERD service installs (issue #1455).

FastAPI's default handler renders each pydantic error with its `input` and `ctx`.
`input` is the submitted value, and for a missing field it is the whole body: a
secrets create without `name` echoed the plaintext `data`, a login echoed the
password, a webhook registration its signing secret, a device create the values of
its `field_data`. The default handler also fails on a lone surrogate: it echoes the
input back, Starlette cannot encode a surrogate as UTF-8, and the 422 became a 500.

This handler renders each error as exactly `{type, loc, msg}` and drops every other
key (`input`, `ctx`, `url`); `install_validation_errors` also makes the app's OpenAPI
document advertise that shape. What that guarantees, exactly:

- No 422 carries `input` or `ctx`, so none holds a submitted value as such: no
  secret's `data` value, password, webhook signing secret, or `field_data` value.
- No error this handler renders becomes a 500 because of a lone surrogate: every lone
  surrogate in a string in `loc` or `msg` is replaced with U+FFFD, so the body always
  encodes. A constrained string (a length bound, a pattern) refuses a surrogate as
  `string_unicode` with a fixed message; a plain `str`, and the keys and values of a
  `dict[str, str]`, accept one. A key of a free-form mapping can therefore carry one
  into `loc`, which is why the replacement is needed.
- Rule for validator authors: a custom validator's message must never hold a
  submitted string as is. Pydantic-core cannot build an error whose message holds a
  lone surrogate: a `ValueError` or `AssertionError` raised with one becomes a
  UnicodeEncodeError instead of a validation error (a `PydanticCustomError` fails
  the same way when its errors are read), so this handler never runs and the route
  answers 500. Quote a submitted value with `repr` (`{value!r}`), or a list of them
  (whose `str` is the `repr` of each item), which writes a surrogate, and any control
  character, as its escape. `services/common/tests/test_validation_errors.py` pins
  both halves.
- It is NOT a promise that no submitted text appears at all. Two keys still can:
  - `loc` names the keys the caller sent where a value failed or a key was refused:
    an unknown key on an `extra="forbid"` body, and a key of a free-form mapping.
    For the secrets service that is a key inside `data`
    (`["body", "data", "password"]` when that value is not a string), never the
    value; a caller who puts secret material in a key name gets it back.
  - `msg` is the validator's own text. Pydantic's built-in messages name the
    permitted values of an enum or literal, never the input, but a few quote part
    of it: the character at fault in a UUID, an email's invalid character or its
    IP-literal domain, the tag of a discriminated union. Three of the services' own
    validators quote input, each with `repr`, none of them a secret-bearing field:
    inventory's dropdown default and options
    (`FieldDefinition.validate_options_and_default`), integration's unknown event
    types (`WebhookCreate._validate_events`), and user-profile's `page_sizes` key
    names (`_validate_page_sizes`).
- A body a route builds itself (`HTTPException(detail=...)`) is not rendered by this
  handler, and some such details quote a submitted value.

A lone surrogate is refused at the body, for every JSON body (issues #1456, #1458,
#1459). A plain `str` field, the keys and values of a `dict[str, str]`, and any value
typed `Any` accept one, and every later step that must encode it as UTF-8 then fails: a
JSON response, an httpx call to another service, a database write, a NATS payload. So
`install_validation_errors` also installs `RefuseLoneSurrogates`. What it guarantees,
exactly:

- It looks only at a body FastAPI parses as JSON: content type `application/json` or
  an `application/*+json` subtype (FastAPI's own rule; with no content type FastAPI
  does not parse the body, `strict_content_type` being on everywhere in HERD). Any
  other body, multipart, a form, `text/plain`, octet-stream, or none, is passed through
  unread and unbuffered. A query string, a path, an urlencoded form, a multipart field,
  and a header cannot carry a lone surrogate anyway: each is percent-decoded with
  replacement, decoded strictly, or decoded as Latin-1. A JSON document a route parses
  itself out of an uploaded file is its own check, with `holds_lone_surrogate`.
- It never parses the body. Whether the body holds a lone surrogate is decided from
  its raw text (`_raw_text_holds_a_lone_surrogate`): for a body that is valid JSON the
  answer is exactly whether any member, also one a later duplicate key replaces, holds
  one once parsed. A body that is not valid JSON but holds a surrogate written as raw
  bytes, or an escape that would be one, is refused too, instead of the route's own
  JSON error; both are client errors.
- The answer is always the one item `LONE_SURROGATE_BODY_ERROR`,
  `{"type": "string_unicode", "loc": ["body"], "msg": ...}` with pydantic's own type
  and message, whatever field holds the surrogate: it never names the field, never
  holds the value, and never grows with the body. A field that pydantic itself would
  refuse (a constrained string) is answered this way too, since the check runs first.
- It runs when the app reads the body, so a route that does not exist (404), a method
  not allowed (405), and a route that takes no body are unchanged. A route that reads
  its body (every route with a body parameter) reads it before its dependencies and
  its own validation run, so the refusal comes before authentication, before a role
  gate, and before the schema, as FastAPI's own 422 for a body that is not JSON does.
- Its whole cost is that scan: linear in the body's length, at most about two copies
  of the body in memory, nothing that grows with the number of elements or escapes.
  It runs on the event loop, like FastAPI's own parse. The measured costs are in
  OPS-VALID-8 (`docs/specs/operations-and-observability.md`); they are observations,
  not a bound.

Every service installs it from its `app/main.py`; `tests/unit/test_validation_errors_wiring.py`
fails when one does not. The config service, which must not import herd_common,
installs a standalone copy (`services/config/app/validation_errors.py`) whose code is
pinned identical to this module's by `services/config/tests/test_validation_errors.py`,
and installs it with `refuse_lone_surrogates=False`: config already judges a lone
surrogate in each password with an answer of its own (OPS-CONFIG-8), which the body
check would replace. The wiring guard allows that keyword to config alone.
First written for auth (issue #1426), copied for config (issue #1428).
"""

from __future__ import annotations

import email.message
import json
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _encodable(value: Any) -> Any:
    """`value` with every lone surrogate in a string replaced by U+FFFD."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    return value


def render_validation_errors(errors: Any) -> list[dict[str, Any]]:
    """Each error as `{type, loc, msg}`, encodable as UTF-8, with no input value."""
    return [
        {
            "type": _encodable(error.get("type")),
            "loc": [_encodable(part) for part in error.get("loc", ())],
            "msg": _encodable(error.get("msg")),
        }
        for error in errors
    ]


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": render_validation_errors(exc.errors())},
    )


# The keys every rendered error has, and the only ones the OpenAPI document lists.
RENDERED_KEYS = ("type", "loc", "msg")


def restrict_validation_error_schema(openapi: dict[str, Any]) -> dict[str, Any]:
    """Make the generated `ValidationError` component list only `RENDERED_KEYS`.

    FastAPI's component also lists `input` and `ctx`, which this handler never
    sends. Changed in place and returned; a document without the component (no
    route takes a body or a parameter) is returned unchanged.
    """
    component = openapi.get("components", {}).get("schemas", {}).get("ValidationError")
    if component is not None:
        properties = component.get("properties", {})
        component["properties"] = {k: v for k, v in properties.items() if k in RENDERED_KEYS}
    return openapi


# Pydantic's own type and message for a constrained string holding a lone surrogate.
# The body check's one item reads the same, at `["body"]`.
STRING_UNICODE_TYPE = "string_unicode"
STRING_UNICODE_MSG = "Input should be a valid string, unable to parse raw data as a unicode string"
LONE_SURROGATE_BODY_ERROR = {
    "type": STRING_UNICODE_TYPE,
    "loc": ["body"],
    "msg": STRING_UNICODE_MSG,
}

# The bytes a UTF-8 JSON text needs to hold a lone surrogate: the start of a `\u`
# escape, or the lead byte 0xED of a surrogate written as raw UTF-8 bytes (`json.loads`
# decodes with `surrogatepass`). A body with neither is passed after two searches.
_RAW_SURROGATE_BYTES = re.compile(rb"\xed[\xa0-\xbf]")

# The JSON escape grammar, for the raw scan. In a JSON string every backslash starts an
# escape, so once each escaped backslash pair is replaced by one other byte, every
# backslash left starts an escape. `json.loads` joins a high-surrogate escape
# (`\uD800` to `\uDBFF`) immediately followed by a low-surrogate escape (`\uDC00` to
# `\uDFFF`) into one code point; every other surrogate escape stays a lone surrogate.
# On that copy, lower-cased (a hex digit may be either case): a high escape not
# followed by a low one is lone (a search whose literal prefix `\ud` lets the engine
# skip to candidates); with none, every high escape begins a pair, which holds one low,
# so a low escape is lone exactly when there are more low escapes than high ones.
_HIGH_NOT_PAIRED = re.compile(rb"\\ud[89ab][0-9a-f]{2}(?!\\ud[c-f][0-9a-f]{2})")
_LOW_ESCAPES = (b"\\udc", b"\\udd", b"\\ude", b"\\udf")
_HIGH_ESCAPES = (b"\\ud8", b"\\ud9", b"\\uda", b"\\udb")


def _escapes_hold_a_lone_surrogate(text: bytes) -> bool:
    """Whether the UTF-8 JSON text `text` holds a lone surrogate escape.

    Makes at most two copies of the text (the escaped backslash pairs replaced, and
    lower-cased), each in C; the search and the eight counts are C passes too. So the
    cost is linear in the text's length, and memory is at most about twice the UTF-8
    text, whatever its number of escapes or elements (a UTF-16 or UTF-32 body is
    decoded first, which adds its own copies).
    """
    if b"\\\\" in text:
        text = text.replace(b"\\\\", b"_")
    text = text.lower()
    if _HIGH_NOT_PAIRED.search(text):
        return True
    lows = sum(text.count(low) for low in _LOW_ESCAPES)
    return lows > 0 and lows > sum(text.count(high) for high in _HIGH_ESCAPES)


def _raw_text_holds_a_lone_surrogate(body: bytes) -> bool:
    """Whether a JSON body holds a lone surrogate, decided from its raw text.

    For a body that is valid JSON the answer is exactly whether any member, also one a
    later duplicate key replaces, holds a lone surrogate once parsed. For a body that
    is not valid JSON it is whether its text holds a surrogate written as raw UTF-8
    bytes or a `\\u` escape that would be one.
    """
    encoding = json.detect_encoding(body)
    if encoding in ("utf-8", "utf-8-sig"):
        if b"\\u" not in body and b"\xed" not in body:
            return False
        if _RAW_SURROGATE_BYTES.search(body):
            return True
        return _escapes_hold_a_lone_surrogate(body)
    try:  # UTF-16 or UTF-32, decoded exactly as `json.loads` decodes it
        text = body.decode(encoding, "surrogatepass")
    except UnicodeDecodeError:
        return False
    if _LONE_SURROGATE.search(text):
        return True
    # No lone surrogate is left in the text, so it encodes; its escapes are then read
    # as a UTF-8 body's are.
    return _escapes_hold_a_lone_surrogate(text.encode("utf-8"))


def holds_lone_surrogate(value: Any) -> bool:
    """Whether a parsed JSON value holds a lone surrogate in any string or key.

    Encodes the value once in C (`json.dumps` with `ensure_ascii=False`, then UTF-8,
    which refuses a lone surrogate). A value too deep to serialize, or one that is not
    JSON data, is walked instead, with a stack, until the first string that holds one.
    """
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    except (RecursionError, TypeError, ValueError):
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                if _LONE_SURROGATE.search(item):
                    return True
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return False


def _parses_as_json(scope: Any) -> bool:
    """Whether FastAPI would parse this request's body as JSON: its content type is
    `application/json` or an `application/*+json` subtype. FastAPI (`strict_content_type`,
    on by default and turned off nowhere in HERD) does not parse a body with no
    content type, so neither does this check. The same header parsing as FastAPI's."""
    for name, value in scope.get("headers", ()):
        if name == b"content-type":
            message = email.message.Message()
            message["content-type"] = value.decode("latin-1")
            if message.get_content_maintype() != "application":
                return False
            subtype = message.get_content_subtype()
            return subtype == "json" or subtype.endswith("+json")
    return False


class RefuseLoneSurrogates:
    """ASGI middleware: a JSON request body holding a lone surrogate is a 422.

    A body FastAPI would not parse as JSON (`_parses_as_json`) is passed through unread
    and unbuffered. For a JSON body it wraps `receive`: the first time the app reads
    the body, the whole body is read, scanned with `_raw_text_holds_a_lone_surrogate`
    (no parse), and handed on as one message; when the scan finds a lone surrogate, the
    read raises `HTTPException(422)` with the one item `LONE_SURROGATE_BODY_ERROR`,
    which FastAPI's body reader passes on and the app's exception handling renders as
    `{"detail": [...]}`. A read made after the response has started (Starlette's
    disconnect listener on a route that never read its body) is handed on unchecked.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not _parses_as_json(scope):
            await self.app(scope, receive, send)
            return
        read = False
        started = False

        async def send_tracking_start(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def receive_checked() -> Any:
            nonlocal read
            if read:
                return await receive()
            read = True
            chunks = []
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return message
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            if not started and _raw_text_holds_a_lone_surrogate(body):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=[dict(LONE_SURROGATE_BODY_ERROR)],
                )
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, receive_checked, send_tracking_start)


def install_validation_errors(app: FastAPI, *, refuse_lone_surrogates: bool = True) -> None:
    """Use `validation_error_handler` for every 422 and advertise its shape, and,
    unless `refuse_lone_surrogates` is False (the config service alone), refuse a
    request body holding a lone surrogate with `RefuseLoneSurrogates`.

    The OpenAPI override is FastAPI's documented one: replace `app.openapi`
    with a function that builds the default document once, adjusts it, and
    caches it in `app.openapi_schema`.
    """
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    if refuse_lone_surrogates:
        # Innermost, next to the routes, whatever the order of the app's other
        # middleware: `add_middleware` would put it outermost, and a
        # BaseHTTPMiddleware (the request logger) runs the `receive` it passes on in a
        # task group, which wraps the refusal in an ExceptionGroup that FastAPI's body
        # reader turns into a 400. The same guard as `add_middleware`.
        if app.middleware_stack is not None:
            raise RuntimeError("Cannot add middleware after an application has started")
        app.user_middleware.append(Middleware(RefuseLoneSurrogates))

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = restrict_validation_error_schema(FastAPI.openapi(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
