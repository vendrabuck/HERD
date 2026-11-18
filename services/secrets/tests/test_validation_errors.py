"""secrets installs the shared request-validation handler.

FastAPI's default handler rendered every refused request with pydantic's `input`
(the submitted value; for a missing field, the whole body) and `ctx`, and answered
500 when that input held a lone surrogate, because Starlette cannot encode one.
`app/main.py` installs `herd_common.validation_errors`, so each error is exactly
`{type, loc, msg}` and the body always encodes. The renderer itself is pinned in
`services/common/tests/test_validation_errors.py`; these tests pin it through this
service's real app.
Every body-bearing route is covered (`POST /secrets`, `PUT /secrets/{id}`;
the rest take no body): the marker is the plaintext of a secret's `data`.

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
from herd_common.validation_errors import RENDERED_KEYS, validation_error_handler
from httpx import ASGITransport, AsyncClient

SECRET_KEY = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
MARKER = "echo-canary-secrets-7Kq2"
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


_SECRET_PATH = f"/secrets/{uuid.uuid4()}"
_CASES = {
    # no name: the default `input` was the body, plaintext data included
    "create-missing": ("POST", "/secrets", {"type": "password", "data": {"password": MARKER}}),
    "create-data_not_a_mapping": ("POST", "/secrets", {"name": "n", "data": [MARKER]}),
    "create-data_value_wrong_type": (
        "POST",
        "/secrets",
        {"name": "n", "data": {"password": [MARKER]}},
    ),
    "create-other_field_wrong_type": (
        "POST",
        "/secrets",
        {"name": ["n"], "type": "bogus", "data": {"password": MARKER}},
    ),
    "create-lone_surrogate": (
        "POST",
        "/secrets",
        # no name, so the default `input` was the body: data's surrogate made it a 500
        {"data": {"password": MARKER + SUR}},
    ),
    "create-name_lone_surrogate": (
        "POST",
        "/secrets",
        {"name": "n" + SUR, "data": {"password": MARKER}},
    ),
    "update-not_an_object": ("PUT", _SECRET_PATH, [MARKER]),
    "update-data_not_a_mapping": ("PUT", _SECRET_PATH, {"data": MARKER}),
    "update-data_value_wrong_type": ("PUT", _SECRET_PATH, {"data": {"password": {"v": MARKER}}}),
    "update-other_field_wrong_type": (
        "PUT",
        _SECRET_PATH,
        {"type": "bogus", "data": {"password": MARKER}},
    ),
    "update-lone_surrogate": ("PUT", _SECRET_PATH, {"data": [MARKER + SUR]}),
    "update-description_lone_surrogate": (
        "PUT",
        _SECRET_PATH,
        {"description": "d" + SUR, "data": {"password": MARKER}},
    ),
}


@pytest.mark.parametrize(("method", "path", "body"), _CASES.values(), ids=_CASES.keys())
async def test_a_refused_body_never_echoes_the_submitted_value(method, path, body):
    resp = await _send(method, path, body, _headers())
    _assert_clean_422(resp)


async def test_loc_still_names_a_data_key_but_never_its_value_by_documented_limit():
    """The shared handler's documented limit: `loc` names the keys the caller sent.
    A `data` value that is not a string is refused at its key, so the key name
    comes back and the plaintext beside it does not."""
    body = {"name": "n", "data": {"api_key": 5, "password": MARKER}}
    resp = await _send("POST", "/secrets", body, _headers())
    detail = _assert_clean_422(resp)
    assert detail == [
        {
            "type": "string_type",
            "loc": ["body", "data", "api_key"],
            "msg": "Input should be a valid string",
        }
    ]
