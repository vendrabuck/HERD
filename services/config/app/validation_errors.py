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
