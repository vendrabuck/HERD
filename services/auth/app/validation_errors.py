"""Auth's request-validation (422) handler (issue #1426).

FastAPI's default handler renders each pydantic error with its `input`: for a
login or registration body that is the submitted password, and for a missing field
it is the whole body, password included. It also fails on a lone surrogate:
pydantic refuses one with `string_unicode`, the default handler echoes it back,
and Starlette cannot encode it as UTF-8, so the 422 became a 500.

This handler renders each error as exactly `{type, loc, msg}`, the shape the
specification documents (identity-and-access section 9), and drops every other
key (`input`, `ctx`, `url`); `install_validation_errors` also makes auth's OpenAPI
document advertise that shape. What that guarantees, exactly:

- No 422 carries `input` or `ctx`.
- No 422 holds a submitted password: every error a password field can raise
  (missing, wrong type, length, the NUL check, `string_unicode`) has fixed text.
- It is NOT a promise that no submitted text appears at all. `msg` is the
  validator's own text and can quote part of a value: an email's invalid
  character or its IP-literal domain, the character at fault in a UUID. `loc`
  names the keys the caller sent, an unknown key on an `extra="forbid"` body
  included. Both are pinned by tests as known behavior.

No route is known to put a lone surrogate in `loc` or `msg` (pydantic refuses any
string holding one as `string_unicode`, with a fixed message, at the parent's
`loc` for a key), but a 500 is the price of being wrong, so each one is replaced
with U+FFFD and the body always encodes.

Nothing here is auth-specific, but only auth installs it: every other service still
uses FastAPI's default, echo and surrogate 500 included (the config service's case
is issue #1428).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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


def install_validation_errors(app: FastAPI) -> None:
    """Use `validation_error_handler` for every 422 and advertise its shape.

    The OpenAPI override is FastAPI's documented one: replace `app.openapi`
    with a function that builds the default document once, adjusts it, and
    caches it in `app.openapi_schema`.
    """
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = restrict_validation_error_schema(FastAPI.openapi(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
