"""reservations installs the shared request-validation handler.

FastAPI's default handler rendered every refused request with pydantic's `input`
(the submitted value; for a missing field, the whole body) and `ctx`, and answered
500 when that input held a lone surrogate, because Starlette cannot encode one.
`app/main.py` installs `herd_common.validation_errors`, so each error is exactly
`{type, loc, msg}` and the body always encodes. The renderer itself is pinned in
`services/common/tests/test_validation_errors.py`; these tests pin it through this
service's real app.

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
from fastapi.exceptions import RequestValidationError
from herd_common.validation_errors import (
    RENDERED_KEYS,
    STRING_UNICODE_MSG,
    RefuseLoneSurrogates,
    validation_error_handler,
)
from httpx import ASGITransport, AsyncClient

SECRET_KEY = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
MARKER = "echo-canary-reservations-7Kq2"
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


_WINDOW = {"start_time": "2030-01-01T00:00:00Z", "end_time": "2030-01-01T01:00:00Z"}
_CASES = {
    "missing": ("POST", "/", {"device_ids": [str(uuid.uuid4())], "purpose": MARKER}),
    "wrong_type": ("POST", "/", {"device_ids": MARKER, **_WINDOW}),
    "lone_surrogate": (
        "POST",
        "/",
        {"device_ids": [str(uuid.uuid4())], "purpose": MARKER + SUR, **_WINDOW},
    ),
}


@pytest.mark.parametrize(("method", "path", "body"), _CASES.values(), ids=_CASES.keys())
async def test_a_refused_body_never_echoes_the_submitted_value(method, path, body):
    resp = await _send(method, path, body, _headers("user"))
    _assert_clean_422(resp)


# -- The shared body check refuses a lone surrogate (issues #1456, #1458, #1459) -------

LOW = chr(0xDFFF)
_BODY_ROUTES = [("POST", "/"), ("PATCH", f"/{uuid.uuid4()}/purpose-category")]
# Bodies no schema is consulted for: the check reads the parsed JSON, so a field the
# route does not know, a key, and a nested value are each refused where they are.
_SURROGATE_BODIES = {
    "value": ({"probe": "v" + SUR}, ["body", "probe"]),
    "key": ({"probe" + LOW: "v"}, ["body"]),
    "nested": ({"a": [{"b": LOW + "x"}]}, ["body", "a", 0, "b"]),
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
