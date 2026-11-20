"""ai-orchestrator bounds every request body (issue #1466, OPS-VALID-9).

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
_BODY_ROUTES = [("POST", "/commit")]
_AUTHENTICATED_ROUTE = ("POST", "/commit")


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
    assert over.headers["connection"] == "close"


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


# -- The generate route takes its own limit ----------------------------------------------
#
# The default plus every file `_read_uploads` accepts: UPLOAD_MAX_FILES times
# UPLOAD_MAX_FILE_BYTES, read at each request, so they are shrunk here to keep the
# bodies small.


async def test_the_generate_route_takes_its_own_limit(monkeypatch):
    """Over the default but within the route's limit, the body reaches the route's
    authentication (no credentials, so 401 or 403); over the route's limit it is the
    413 naming that limit."""
    monkeypatch.setattr(settings, "upload_max_files", 2)
    monkeypatch.setattr(settings, "upload_max_file_bytes", 1000)
    upload = 2 * 1000
    data = {"prompt": "two leaf switches"}
    within = {"files": ("a.txt", b"z" * (LIMIT + 100))}
    over = {"files": ("a.txt", b"z" * (LIMIT + upload + 1))}
    async with _client() as client:
        reached = await client.post("/generate", files=within, data=data)
        refused = await client.post("/generate", files=over, data=data)
    assert reached.status_code in (401, 403), reached.text[:300]
    assert refused.status_code == 413, refused.text[:300]
    assert refused.json() == {"detail": [body_too_large_error(LIMIT + upload)]}


def test_the_generate_route_is_the_only_one_with_its_own_limit():
    from fastapi.routing import iter_route_contexts
    from herd_common.validation_errors import route_body_limit

    limits = {
        (method, route.path): route_body_limit(route, LIMIT)
        for route in iter_route_contexts(app.routes)
        for method in route.methods or ()
    }
    assert {k: v for k, v in limits.items() if v != LIMIT} == {
        ("POST", "/generate"): LIMIT + settings.upload_max_files * settings.upload_max_file_bytes
    }
