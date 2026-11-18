"""The ONE request-validation (422) handler every HERD service installs.

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
- No 422 becomes a 500 because of a lone surrogate: every lone surrogate in a string
  in `loc` or `msg` is replaced with U+FFFD, so the body always encodes. No route is
  known to put one there (pydantic refuses a string holding one as `string_unicode`,
  with a fixed message), but a 500 is the price of being wrong.
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
    validators quote input, none of them a secret-bearing field: inventory's
    dropdown default (`FieldDefinition.validate_options_and_default`), integration's
    unknown event types (`WebhookCreate._validate_events`), and user-profile's
    `page_sizes` key names (`_validate_page_sizes`).

Every service installs it from its `app/main.py`; `tests/unit/test_validation_errors_wiring.py`
fails when one does not. The config service, which must not import herd_common,
installs a standalone copy (`services/config/app/validation_errors.py`) whose code is
pinned identical to this module's by `services/config/tests/test_validation_errors.py`.
First written for auth (issue #1426), copied for config (issue #1428).
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
        return _LONE_SURROGATE.sub("�", value)
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
