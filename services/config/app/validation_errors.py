"""The config service's request-validation (422) handler.

A standalone copy of the shared `herd_common.validation_errors`, the handler every
other service installs (first written for auth, issue #1426), with the same
behavior and guarantee: the config service imports neither herd_common nor auth
(CONVENTIONS, Config service), so the copy is kept in step by hand, and
`tests/test_validation_errors.py` (`test_the_code_is_identical_to_the_shared_module`)
fails when its code, docstrings aside, differs from the shared module's.

FastAPI's default handler renders each pydantic error with its `input`: for a
login or change-password body that is the submitted password, and for a missing
field it is the whole body, password included. It also failed on a lone
surrogate: the default handler echoes the input back, and Starlette cannot
encode a surrogate as UTF-8, so the 422 became a 500.

This handler renders each error as exactly `{type, loc, msg}` and drops every
other key (`input`, `ctx`, `url`); `install_validation_errors` also makes the
config service's OpenAPI document advertise that shape. What that guarantees,
exactly:

- No 422 carries `input` or `ctx`.
- No 422 holds a submitted password: every error a password field can raise
  (missing, wrong type) has fixed text. Which strings may become the password is
  judged after validation by `config_store.new_password_problem`, whose 422
  detail is a fixed string.
- It is NOT a promise that no submitted text appears at all. `msg` is the
  validator's own text and could quote part of a value, and `loc` names the keys
  the caller sent.

No route is known to put a lone surrogate in `loc` or `msg`, but a 500 is the
price of being wrong, so each one is replaced with U+FFFD and the body always
encodes.

The shared module also refuses a request body holding a lone surrogate
(`RefuseLoneSurrogates`, issues #1456, #1458, #1459). The copy carries that code
too, so the two stay identical, but `app/main.py` installs it with
`refuse_lone_surrogates=False`: config judges a lone surrogate in a password itself
(OPS-CONFIG-8: a login answers 401, a new password the fixed 422 `Password must be
valid UTF-8 text`), and a body-level refusal would replace those pinned answers.
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
    cost is linear in the text's length, and memory is at most about twice the text,
    whatever its number of escapes or elements.
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
