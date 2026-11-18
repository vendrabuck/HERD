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
from fastapi.exceptions import RequestValidationError
from herd_common.validation_errors import RENDERED_KEYS, validation_error_handler
from httpx import ASGITransport, AsyncClient

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
    assert detail[0]["msg"] == f"Value error, Default '{MARKER}' is not in options: a"
