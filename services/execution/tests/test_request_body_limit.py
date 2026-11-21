"""execution bounds every request body (issue #1466, OPS-VALID-9).

`app/main.py` passes `settings.max_request_body_bytes` (`MAX_REQUEST_BODY_BYTES`) to
`install_validation_errors`, which installs `LimitRequestBody` just outside the
lone-surrogate check. These requests carry no credentials. A body that is not JSON
answers FastAPI's `json_invalid` at the limit, so it was handed on whole and parsed,
and 413 one byte over it, so it was never parsed; an honest body is refused by the
route's authentication while one over the limit is the 413, so the limit comes
first. The middleware itself is pinned in `services/common/tests/test_request_body_limit.py`.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.main import app
from herd_common.validation_errors import (
    LimitRequestBody,
    RefuseLoneSurrogates,
    body_too_large_error,
)
from httpx import ASGITransport, AsyncClient

LIMIT = settings.max_request_body_bytes
_JSON = {"content-type": "application/json"}
_BODY_ROUTES = [("POST", "/device-check")]
_AUTHENTICATED_ROUTE = ("POST", "/device-check")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _refused(resp) -> bool:
    return resp.status_code == 413 and resp.json() == {"detail": [body_too_large_error(LIMIT)]}


def test_the_body_limit_is_installed_just_outside_the_body_check():
    assert [m.cls for m in app.user_middleware[-2:]] == [LimitRequestBody, RefuseLoneSurrogates]
    assert app.user_middleware[-2].kwargs == {"max_body_bytes": LIMIT}


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed(method, path):
    async with _client() as client:
        at = await client.request(method, path, content=b"x" * LIMIT, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (LIMIT + 1), headers=_JSON)
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert _refused(over), over.text[:300]
    assert "connection" not in over.headers  # the connection stays usable


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_a_chunked_body_over_the_limit_is_413(method, path):
    async def parts():
        for _ in range(5):
            yield b" " * (LIMIT // 4)

    async with _client() as client:
        resp = await client.request(method, path, content=parts(), headers=_JSON)
    assert _refused(resp), resp.text[:300]


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize("chunked", [False, True], ids=["declared", "chunked"])
async def test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422(
    method, path, chunked
):
    """Chunked, there is no Content-Length to refuse early: only a limit outside the
    surrogate check stops it from reading the whole body and answering its 422."""
    head = b'["\\ud800"'

    async def parts():
        yield head
        for _ in range(5):
            yield b" " * (LIMIT // 4)

    body = parts() if chunked else head + b" " * LIMIT + b"]"
    async with _client() as client:
        resp = await client.request(method, path, content=body, headers=_JSON)
    assert _refused(resp), resp.text[:300]


async def test_without_credentials_the_limit_answers_before_authentication():
    method, path = _AUTHENTICATED_ROUTE
    async with _client() as client:
        honest = await client.request(method, path, content=b"{}", headers=_JSON)
        over = await client.request(method, path, content=b"{}" + b" " * LIMIT, headers=_JSON)
    assert honest.status_code in (401, 403), honest.text[:300]
    assert _refused(over), over.text[:300]


# -- The package validator takes its own limit ---------------------------------------------
#
# Its JSON body carries the package as base64, so it may hold the base64 length of
# VALIDATE_PACKAGE_MAX_BYTES beyond the default. The setting is read at each request,
# so it is shrunk here to keep the bodies small.


def test_base64_length_is_the_padded_length():
    import base64

    from app.routers.validation import base64_length

    for size in range(0, 40):
        assert base64_length(size) == len(base64.b64encode(b"\0" * size))


async def test_the_package_validator_takes_its_own_limit(monkeypatch):
    """Over the default but within the route's limit, the body reaches the route's
    internal-token gate (no token, so 401, 403, or 500 when none is configured); over
    the route's limit it is the 413 naming that limit."""
    from app.routers.validation import base64_length

    monkeypatch.setattr(settings, "validate_package_max_bytes", 3000)
    route_limit = LIMIT + base64_length(3000)
    head = b'{"package_b64": "'
    within = head + b"A" * (LIMIT + 1000) + b'"}'
    over = head + b"A" * (route_limit - len(head) - 1) + b'"}'
    assert len(over) == route_limit + 1
    async with _client() as client:
        reached = await client.post("/internal/validate-package", content=within, headers=_JSON)
        refused = await client.post("/internal/validate-package", content=over, headers=_JSON)
    assert reached.status_code in (401, 403, 500), reached.text[:300]
    assert refused.status_code == 413, refused.text[:300]
    assert refused.json() == {"detail": [body_too_large_error(route_limit)]}


# -- A route another service forwards a body to takes the envelope (OPS-VALID-10) ---------
#
# inventory's immediate apply and ai-orchestrator's commit forward a device
# configuration to `/execute`, inventory's apply scheduler to `/execute/internal`.
# Each sender admitted the configuration through a large-JSON route (issue #1484), so
# these take the default limit plus LARGE_JSON_BODY_BYTES plus FORWARD_ENVELOPE_BYTES.

_FORWARD_ROUTES = [("POST", "/execute"), ("POST", "/execute/internal")]


@pytest.mark.parametrize(("method", "path"), _FORWARD_ROUTES)
async def test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413(method, path):
    from herd_common.validation_errors import FORWARD_ENVELOPE_BYTES, LARGE_JSON_BODY_BYTES

    route_limit = LIMIT + LARGE_JSON_BODY_BYTES + FORWARD_ENVELOPE_BYTES
    async with _client() as client:
        at = await client.request(method, path, content=b"x" * route_limit, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (route_limit + 1), headers=_JSON)
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert over.status_code == 413, over.text[:300]
    assert over.json() == {"detail": [body_too_large_error(route_limit)]}
