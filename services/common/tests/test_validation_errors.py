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

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from herd_common.validation_errors import (
    RENDERED_KEYS,
    install_validation_errors,
    render_validation_errors,
    restrict_validation_error_schema,
    validation_error_handler,
)
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def _quoting_app() -> FastAPI:
    app = FastAPI()
    install_validation_errors(app)

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


async def test_a_validator_message_quoting_with_repr_is_a_clean_422():
    async with _client(_quoting_app()) as client:
        resp = await client.post(
            "/q", content=b'{"quoted": "Z\\ud800"}', headers={"Content-Type": "application/json"}
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "quoted"], "msg": "Value error, bad 'Z\\ud800'"}
    ]
