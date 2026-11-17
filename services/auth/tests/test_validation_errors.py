"""Auth's request-validation handler (issue #1426).

FastAPI's default handler renders every pydantic error with its `input`, which for
a login or registration body is the submitted password (for a missing field it is
the whole body), and Starlette cannot encode a lone surrogate, so a surrogate in
the input turned the 422 into a 500. Auth's handler renders each error as exactly
`{type, loc, msg}` (the shape the specification documents) and replaces any lone
surrogate in `loc` or `msg` with U+FFFD, so the body always encodes.
"""

from __future__ import annotations

import json

from app.main import app
from app.validation_errors import (
    RENDERED_KEYS,
    restrict_validation_error_schema,
    validation_error_handler,
)
from fastapi.exceptions import RequestValidationError


async def _render(errors: list[dict]) -> tuple[int, object]:
    response = await validation_error_handler(None, RequestValidationError(errors))
    return response.status_code, json.loads(response.body)


def test_the_handler_is_installed_for_request_validation_errors():
    assert app.exception_handlers[RequestValidationError] is validation_error_handler


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
    """Defensive: no route here is known to put a surrogate in `loc` or `msg`
    (pydantic refuses a surrogate key at the parent's loc), but a 500 is the
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
                "loc": ["body", "a\ufffdb", 3],
                "msg": "found `\ufffd` at 1",
            }
        ]
    }


async def test_the_handler_keeps_honest_non_ascii_text_unchanged():
    status, body = await _render(
        [{"type": "value_error", "loc": ("body", "full_name"), "msg": "Caf\u00e9 \U0001f511"}]
    )
    assert body["detail"][0]["msg"] == "Caf\u00e9 \U0001f511"


# -- The advertised shape ---------------------------------------------------------------


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
    _, body = await _render(
        [{"type": "missing", "loc": ("body", "x"), "msg": "Field required", "input": {}}]
    )
    assert [set(error) for error in body["detail"]] == [advertised]


def test_restrict_leaves_a_document_without_the_component_alone():
    document = {"openapi": "3.1.0", "paths": {}}
    assert restrict_validation_error_schema(document) == {"openapi": "3.1.0", "paths": {}}
