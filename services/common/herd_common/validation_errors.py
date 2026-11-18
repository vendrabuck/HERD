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

A lone surrogate is refused at the body, for every route (issues #1456, #1458, #1459).
A plain `str` field, the keys and values of a `dict[str, str]`, and any value typed
`Any` accept one, and every later step that must encode it as UTF-8 then fails: a
JSON response, an httpx call to another service, a database write, a NATS payload. So
`install_validation_errors` also installs `RefuseLoneSurrogates`, which checks each
request body when the app reads it and answers 422 when the parsed JSON holds a lone
surrogate anywhere: a value, a key, nested, in a field of any type. What that
guarantees, exactly:

- The answer is the same `{"detail": [...]}` list, each item `{type, loc, msg}`, with
  pydantic's own `string_unicode` type and message. `loc` is `["body", ...]` and ends
  at the string that holds the surrogate, or, for a key, at the object holding the key
  (as pydantic refuses such a key on an `extra="forbid"` body); it never holds the
  surrogate or the value.
- It runs only when the app reads the body, so a route that does not exist (404), a
  method not allowed (405), and a route that takes no body are unchanged. A route that
  reads its body (every route with a body parameter) reads it before its dependencies
  run, so the refusal comes before authentication, as FastAPI's own 422 for a body
  that is not JSON does.
- Every body is checked except a `multipart/*` one, which is passed through unread:
  Starlette decodes a multipart field strictly as UTF-8 (falling back to Latin-1 for a
  file name), so no lone surrogate can come from one. A query string, a path, an
  urlencoded form, and a header cannot carry one either: each is percent-decoded with
  replacement or decoded as Latin-1. A JSON document a route parses itself out of an
  uploaded file is its own check, with `lone_surrogate_locs`.
- The cost is at most three substring searches over the body. Only a body holding the
  text `\\ud` or `\\uD`, or the byte 0xED (the start of a surrogate escape, or the
  UTF-8 lead byte of a surrogate written as raw bytes), is searched with a regular
  expression, and only a body that matches it is parsed a second time to look; a body
  that JSON would not decode as UTF-8 (a UTF-16 or UTF-32 document) is always parsed.
- A body that is not JSON passes through, so the route answers as it would have.

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


# Pydantic's own type and message for a constrained string holding a lone surrogate,
# so the body check's refusal reads the same as the schema's.
STRING_UNICODE_TYPE = "string_unicode"
STRING_UNICODE_MSG = "Input should be a valid string, unable to parse raw data as a unicode string"

# A JSON escape whose first hex digit is D (`\ud800` to `\udfff` among them), or a raw
# UTF-8 lead byte 0xED followed by 0xA0 to 0xBF: the two ways a JSON text decoded as
# UTF-8 by `json.loads` (which uses `surrogatepass`) can produce a lone surrogate.
_MAY_HOLD_A_SURROGATE = (b"\\ud", b"\\uD", b"\xed")
_SURROGATE_ESCAPE_OR_BYTES = re.compile(rb"\\u[dD][89a-fA-F]|\xed[\xa0-\xbf]")


def lone_surrogate_locs(value: Any) -> list[tuple[str | int, ...]]:
    """Where a parsed JSON value holds a lone surrogate, in document order, each once.

    A string holding one is located at itself; a key holding one at the object that
    holds the key, whose value is then not searched. A located key or index is never
    a string that holds a surrogate. Iterative, so a deeply nested value cannot
    exhaust the stack.
    """
    found: list[tuple[str | int, ...]] = []
    stack: list[tuple[tuple[str | int, ...], Any]] = [((), value)]
    while stack:
        loc, item = stack.pop()
        if isinstance(item, str):
            if _LONE_SURROGATE.search(item):
                found.append(loc)
        elif isinstance(item, dict):
            children = []
            for key, child in item.items():
                if isinstance(key, str) and _LONE_SURROGATE.search(key):
                    found.append(loc)
                else:
                    children.append(((*loc, key), child))
            stack.extend(reversed(children))
        elif isinstance(item, list):
            stack.extend(reversed([((*loc, index), child) for index, child in enumerate(item)]))
    return list(dict.fromkeys(found))


def body_lone_surrogate_errors(body: bytes) -> list[dict[str, Any]]:
    """The rendered `string_unicode` errors for a request body, empty when it is clean.

    Empty, too, for a body `json.loads` refuses: the route answers that as it would
    have (FastAPI's `json_invalid`).
    """
    if json.detect_encoding(body) in ("utf-8", "utf-8-sig"):
        if not any(marker in body for marker in _MAY_HOLD_A_SURROGATE):
            return []
        if not _SURROGATE_ESCAPE_OR_BYTES.search(body):
            return []
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return []
    return render_validation_errors(
        {"type": STRING_UNICODE_TYPE, "loc": ("body", *loc), "msg": STRING_UNICODE_MSG}
        for loc in lone_surrogate_locs(parsed)
    )


def _is_multipart(scope: Any) -> bool:
    for name, value in scope.get("headers", ()):
        if name == b"content-type":
            return value.lstrip().lower().startswith(b"multipart/")
    return False


class RefuseLoneSurrogates:
    """ASGI middleware: a request body holding a lone surrogate is a 422.

    It wraps `receive`. The first time the app reads the body, the whole body is read,
    checked with `body_lone_surrogate_errors`, and handed on as one message; when the
    check finds a surrogate, the read raises `HTTPException(422)` instead, which
    FastAPI's body reader passes on and the app's exception handling renders as
    `{"detail": [...]}`. A read made after the response has started (Starlette's
    disconnect listener on a route that never read its body) is handed on unchecked.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or _is_multipart(scope):
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
            errors = [] if started else body_lone_surrogate_errors(body)
            if errors:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors
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
