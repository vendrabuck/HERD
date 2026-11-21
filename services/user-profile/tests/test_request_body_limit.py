"""user-profile bounds every request body (issue #1466, OPS-VALID-9).

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
_BODY_ROUTES = [("PUT", "/preferences")]
_AUTHENTICATED_ROUTE = ("PUT", "/preferences")


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


# -- A route another service forwards a body to takes the envelope (OPS-VALID-10) ---------
#
# notifications forwards merged notification preferences here.
# The body it admitted, plus what it adds, fits under the default limit plus
# FORWARD_ENVELOPE_BYTES.

_FORWARD_ROUTES = [("PATCH", "/preferences")]


@pytest.mark.parametrize(("method", "path"), _FORWARD_ROUTES)
async def test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413(method, path):
    from herd_common.validation_errors import FORWARD_ENVELOPE_BYTES

    route_limit = LIMIT + FORWARD_ENVELOPE_BYTES
    async with _client() as client:
        at = await client.request(method, path, content=b"x" * route_limit, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (route_limit + 1), headers=_JSON)
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert over.status_code == 413, over.text[:300]
    assert over.json() == {"detail": [body_too_large_error(route_limit)]}


# -- The largest preferences body fits the default (issue #1484) ---------------------------
#
# Measured, not marked: two blobs at their 64 KiB cap as the validator serializes them
# (json.dumps), and 200 page sizes. The schema bounds no page size key's length, so the
# keys are taken at 32 characters, longer than any page key the frontend writes.


def _largest_preferences_body() -> bytes:
    import json

    from app.schemas.preferences import _MAX_BLOB_BYTES, _MAX_KEYS, _MAX_PAGE_SIZE

    blob = {"f": "x" * (_MAX_BLOB_BYTES - len(json.dumps({"f": ""})))}
    assert len(json.dumps(blob)) == _MAX_BLOB_BYTES
    page_sizes = {f"{i:032d}": _MAX_PAGE_SIZE for i in range(_MAX_KEYS)}
    body = {"saved_filters": blob, "page_sizes": page_sizes, "extras": blob}
    return json.dumps(body).encode()


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
async def test_the_largest_preferences_body_fits_the_default_and_is_valid(method):
    """139319 bytes (the measure the wiring guard records), under the default: it is
    valid against the schema, and it reaches the route's authentication, not the 413."""
    import json

    from app.schemas.preferences import PreferencesPatchRequest, PreferencesReplaceRequest

    body = _largest_preferences_body()
    assert len(body) == 139_319
    assert len(body) <= LIMIT
    PreferencesReplaceRequest.model_validate(json.loads(body))
    PreferencesPatchRequest.model_validate(json.loads(body))
    async with _client() as client:
        resp = await client.request(method, "/preferences", content=body, headers=_JSON)
    assert resp.status_code in (401, 403), resp.text[:300]
