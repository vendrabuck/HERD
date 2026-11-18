"""Auth installs the shared request-validation handler (issue #1426).

FastAPI's default handler renders every pydantic error with its `input`, which for
a login or registration body is the submitted password (for a missing field it is
the whole body), and Starlette cannot encode a lone surrogate, so a surrogate in
the input turned the 422 into a 500. Auth installs `herd_common.validation_errors`,
which renders each error as exactly `{type, loc, msg}` (the shape the specification
documents) and replaces any lone surrogate in `loc` or `msg` with U+FFFD; the
renderer itself is pinned in `services/common/tests/test_validation_errors.py`.
These tests pin that auth's real app uses it. The password-specific cases are in
`test_password_input_validation.py`.
"""

from __future__ import annotations

import json

import pytest
from app.main import app
from fastapi.exceptions import RequestValidationError
from herd_common.validation_errors import (
    RENDERED_KEYS,
    STRING_UNICODE_MSG,
    RefuseLoneSurrogates,
    validation_error_handler,
)
from httpx import ASGITransport, AsyncClient


def test_the_handler_is_installed_for_request_validation_errors():
    assert app.exception_handlers[RequestValidationError] is validation_error_handler


def test_the_openapi_document_advertises_exactly_the_rendered_keys():
    """The OpenAPI `ValidationError` lists `type`, `loc`, and `msg` only, the keys
    the handler sends, and still requires all three; FastAPI's default also listed
    `input` and `ctx`."""
    app.openapi_schema = None  # rebuild through the override, not a cached copy
    component = app.openapi()["components"]["schemas"]["ValidationError"]
    assert set(component["properties"]) == set(RENDERED_KEYS) == {"type", "loc", "msg"}
    assert sorted(component["required"]) == ["loc", "msg", "type"]
    assert app.openapi() is app.openapi()  # built once, then cached


async def test_every_rendered_error_has_exactly_the_advertised_keys():
    advertised = set(app.openapi()["components"]["schemas"]["ValidationError"]["properties"])
    response = await validation_error_handler(
        None,
        RequestValidationError(
            [{"type": "missing", "loc": ("body", "x"), "msg": "Field required", "input": {}}]
        ),
    )
    body = json.loads(response.body)
    assert [set(error) for error in body["detail"]] == [advertised]


# -- The shared body check refuses a lone surrogate (issues #1456, #1458, #1459) -------
#
# The check answers first, with one `string_unicode` item at `["body"]` whatever field
# holds the surrogate, where the constrained password field answered at
# `["body", "password"]` (OPS-VALID-6; IAM-LOGIN-5, IAM-REG-2).

SUR = chr(0xD800)  # built, so no source escape is decoded early
LOW = chr(0xDFFF)
_SURROGATE_BODIES = {
    "value": ({"probe": "v" + SUR}, ["body"]),
    "key": ({"probe" + LOW: "v"}, ["body"]),
    "nested": ({"a": [{"b": LOW + "x"}]}, ["body"]),
    "email": ({"email": "a" + SUR + "@example.com", "password": "pw"}, ["body"]),
}


def _raw(value) -> bytes:
    return json.dumps(value, ensure_ascii=True).encode("ascii")


def test_the_body_check_is_installed():
    """Last in the list, so innermost: every route reads its body through it."""
    assert app.user_middleware[-1].cls is RefuseLoneSurrogates


@pytest.mark.parametrize(("body", "loc"), _SURROGATE_BODIES.values(), ids=_SURROGATE_BODIES.keys())
async def test_a_lone_surrogate_anywhere_in_a_login_body_is_refused(body, loc):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/login", content=_raw(body), headers={"content-type": "application/json"}
        )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [{"type": "string_unicode", "loc": loc, "msg": STRING_UNICODE_MSG}]
    }
