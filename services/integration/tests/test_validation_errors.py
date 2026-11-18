"""integration installs the shared request-validation handler.

FastAPI's default handler rendered every refused request with pydantic's `input`
(the submitted value; for a missing field, the whole body) and `ctx`, and answered
500 when that input held a lone surrogate, because Starlette cannot encode one.
`app/main.py` installs `herd_common.validation_errors`, so each error is exactly
`{type, loc, msg}` and the body always encodes. The renderer itself is pinned in
`services/common/tests/test_validation_errors.py`; these tests pin it through this
service's real app.
The facade's `/reservations` and the webhook routes are covered; a webhook
body can carry the caller's signing `secret`.

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
MARKER = "echo-canary-integration-7Kq2"
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
_HOOK = {"target_url": "https://example.com/hook", "event_types": ["reservation.created"]}
_CASES = {
    "facade-missing": (
        "POST",
        "/reservations",
        {"device_ids": [str(uuid.uuid4())], "purpose": MARKER},
    ),
    "facade-wrong_type": ("POST", "/reservations", {"device_ids": MARKER, **_WINDOW}),
    "facade-lone_surrogate": (
        "POST",
        "/reservations",
        {"device_ids": [str(uuid.uuid4())], "purpose": MARKER + SUR, **_WINDOW},
    ),
    # no target_url: the default `input` was the body, the signing secret included
    "webhook-missing": (
        "POST",
        "/webhooks",
        {"event_types": ["reservation.created"], "secret": MARKER},
    ),
    "webhook-wrong_type": ("POST", "/webhooks", {**_HOOK, "secret": [MARKER]}),
    "webhook-secret_too_long": ("POST", "/webhooks", {**_HOOK, "secret": MARKER * 40}),
    "webhook-lone_surrogate": ("POST", "/webhooks", {**_HOOK, "secret": MARKER + SUR}),
    "webhook-patch-extra_key": (
        "PATCH",
        f"/webhooks/{uuid.uuid4()}",
        {"is_active": True, "secret": MARKER},
    ),
}


@pytest.mark.parametrize(("method", "path", "body"), _CASES.values(), ids=_CASES.keys())
async def test_a_refused_body_never_echoes_the_submitted_value(method, path, body):
    resp = await _send(method, path, body, _headers())
    _assert_clean_422(resp)


async def test_a_lone_surrogate_in_an_unknown_event_type_is_a_422_not_a_500():
    """pydantic-core cannot build an error whose message holds a lone surrogate (it
    raises UnicodeEncodeError and the route answers 500). `_validate_events` quotes
    the unknown names as a list, whose `repr` writes a surrogate as its escape."""
    body = {"target_url": "https://example.com/hook", "event_types": ["Z" + SUR]}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/webhooks", content=_raw(body), headers=_headers())
    assert resp.status_code == 422, resp.content[:300]
    (error,) = resp.json()["detail"]
    assert set(error) == set(RENDERED_KEYS)
    assert error["msg"].startswith("Value error, unknown event_types ['Z\\ud800']; allowed: ")
