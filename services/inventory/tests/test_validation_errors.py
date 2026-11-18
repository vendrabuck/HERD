"""inventory installs the shared request-validation handler.

FastAPI's default handler rendered every refused request with pydantic's `input`
(the submitted value; for a missing field, the whole body) and `ctx`, and answered
500 when that input held a lone surrogate, because Starlette cannot encode one.
`app/main.py` installs `herd_common.validation_errors`, so each error is exactly
`{type, loc, msg}` and the body always encodes. The renderer itself is pinned in
`services/common/tests/test_validation_errors.py`; these tests pin it through this
service's real app.
A device's `field_data` holds its credential fields.

Every case below fails validation, so no route handler runs and nothing is stored.
Each body is sent raw with every non-ASCII code point escaped (`content=`, because
`json=` cannot encode a lone surrogate), and the marker it carries must not appear
in the answer in any encoding.
"""

from __future__ import annotations

import json
import uuid

import jwt
import pytest
from app.main import app
from app.schemas.template import FieldDefinition
from fastapi.exceptions import RequestValidationError
from herd_common.validation_errors import (
    RENDERED_KEYS,
    STRING_UNICODE_MSG,
    RefuseLoneSurrogates,
    validation_error_handler,
)
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

SECRET_KEY = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
MARKER = "echo-canary-inventory-7Kq2"
SUR = chr(0xD800)  # a lone high surrogate, built so no source escape is decoded early


def _headers(role: str = "admin") -> dict:
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "role": role, "username": "probe"},
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "content-type": "application/json"}


def _raw(value) -> bytes:
    return json.dumps(value, ensure_ascii=True).encode("ascii")


def _forms(text: str) -> list[bytes]:
    """Every way `text` could appear in a response body."""
    return [
        text.encode("utf-8", "surrogatepass"),
        json.dumps(text, ensure_ascii=True)[1:-1].encode("ascii"),
    ]


async def _send(method: str, path: str, body, headers: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, content=_raw(body), headers=headers)


def _assert_clean_422(resp) -> list[dict]:
    assert resp.status_code == 422, resp.content[:300]
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert [set(error) for error in detail] == [set(RENDERED_KEYS)] * len(detail)
    for form in _forms(MARKER):
        assert form not in resp.content, resp.content[:300]
    return detail


def test_the_shared_handler_is_installed():
    assert app.exception_handlers[RequestValidationError] is validation_error_handler


def test_the_openapi_document_advertises_exactly_the_rendered_keys():
    app.openapi_schema = None  # rebuild through the override, not a cached copy
    component = app.openapi()["components"]["schemas"]["ValidationError"]
    assert set(component["properties"]) == set(RENDERED_KEYS) == {"type", "loc", "msg"}
    assert sorted(component["required"]) == ["loc", "msg", "type"]


_DEVICE = {
    "template_id": str(uuid.uuid4()),
    "topology_type": "PHYSICAL",
    "field_data": {"password": MARKER},
}
_CASES = {
    # no name: the default `input` was the body, field_data included
    "missing": ("POST", "/devices", _DEVICE),
    "wrong_type": ("POST", "/devices", {**_DEVICE, "name": "d1", "field_data": [MARKER]}),
    "lone_surrogate": ("POST", "/devices", {**_DEVICE, "name": MARKER + SUR}),
}


@pytest.mark.parametrize(("method", "path", "body"), _CASES.values(), ids=_CASES.keys())
async def test_a_refused_body_never_echoes_the_submitted_value(method, path, body):
    resp = await _send(method, path, body, _headers())
    _assert_clean_422(resp)


async def test_msg_can_still_quote_a_dropdown_default_by_documented_limit():
    """The shared handler's documented limit: `msg` is the validator's own text,
    and `FieldDefinition.validate_options_and_default` quotes a dropdown default
    that is not one of the options. A dropdown is never a password field."""
    field = {"key": "k", "label": "K", "type": "dropdown", "options": ["a"], "default": MARKER}
    body = {"name": "t", "sections": [{"name": "s", "fields": [field]}]}
    resp = await _send("POST", "/templates", body, _headers())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [set(error) for error in detail] == [set(RENDERED_KEYS)]
    assert detail[0]["msg"] == f"Value error, Default '{MARKER}' is not in options: 'a'"


_DROPDOWN_SURROGATES = {
    "default": (
        {"options": ["a"], "default": "Z" + SUR},
        "Default 'Z\\ud800' is not in options: 'a'",
    ),
    "option": (
        {"options": ["a" + SUR], "default": "Z"},
        "Default 'Z' is not in options: 'a\\ud800'",
    ),
}


@pytest.mark.parametrize(
    ("dropdown", "msg"), _DROPDOWN_SURROGATES.values(), ids=_DROPDOWN_SURROGATES.keys()
)
async def test_a_lone_surrogate_the_dropdown_message_quotes_is_a_422_not_a_500(dropdown, msg):
    """A plain `str` takes a lone surrogate, and pydantic-core cannot build an error
    whose message holds one: it raised UnicodeEncodeError instead of a validation
    error, so the handler never ran and the route answered 500. The shared body check
    now refuses the body before the schema runs; the message still quotes each value
    with `repr`, which writes a surrogate as its escape, for any value that reaches the
    validator another way."""
    field = {"key": "k", "label": "K", "type": "dropdown", **dropdown}
    body = {"name": "t", "sections": [{"name": "s", "fields": [field]}]}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/templates", content=_raw(body), headers=_headers())
    assert resp.status_code == 422, resp.content[:300]
    detail = resp.json()["detail"]
    assert [set(error) for error in detail] == [set(RENDERED_KEYS)]
    assert detail[0]["type"] == "string_unicode"
    assert detail[0]["loc"] == ["body"]
    with pytest.raises(ValidationError) as caught:
        FieldDefinition.model_validate(field)
    (direct,) = caught.value.errors()
    assert direct["msg"] == "Value error, " + msg


# -- The shared body check refuses a lone surrogate (issues #1456, #1458, #1459) -------

LOW = chr(0xDFFF)
_BODY_ROUTES = [("POST", "/devices"), ("POST", "/templates")]
# Bodies no schema is consulted for: the check reads the parsed JSON, so a field the
# route does not know, a key, and a nested value are each refused where they are.
_SURROGATE_BODIES = {
    "value": ({"probe": "v" + SUR}, ["body"]),
    "key": ({"probe" + LOW: "v"}, ["body"]),
    "nested": ({"a": [{"b": LOW + "x"}]}, ["body"]),
}


def test_the_body_check_is_installed():
    """Last in the list, so innermost: every route reads its body through it."""
    assert app.user_middleware[-1].cls is RefuseLoneSurrogates


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize(("body", "loc"), _SURROGATE_BODIES.values(), ids=_SURROGATE_BODIES.keys())
async def test_a_lone_surrogate_anywhere_in_a_body_is_refused(method, path, body, loc):
    resp = await _send(method, path, body, _headers())
    assert resp.status_code == 422, resp.content[:300]
    assert resp.json() == {
        "detail": [{"type": "string_unicode", "loc": loc, "msg": STRING_UNICODE_MSG}]
    }


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_an_escaped_surrogate_pair_is_not_refused_by_the_check(method, path):
    """A character outside the BMP, sent as two escapes, is one code point once parsed.
    A list is no route's body, so the route's own validation answers."""
    resp = await _send(method, path, ["\U0001f511"], _headers())
    assert resp.status_code == 422, resp.content[:300]
    assert "string_unicode" not in resp.text
