"""The config service's request-validation (422) handler.

A standalone copy of the auth service's `app/validation_errors.py` (issue #1426),
with the same behavior and guarantee: the config service imports neither
herd_common nor auth (CONVENTIONS, Config service), so the two copies are kept in
step by hand.

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
