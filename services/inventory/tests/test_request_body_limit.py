"""inventory bounds every request body (issue #1466, OPS-VALID-9).

`app/main.py` passes `settings.max_request_body_bytes` (`MAX_REQUEST_BODY_BYTES`) to
`install_validation_errors`, which installs `LimitRequestBody` just outside the
lone-surrogate check. These requests carry no credentials. A body that is not JSON
answers FastAPI's `json_invalid` at the limit, so it was handed on whole and parsed,
and 413 one byte over it, so it was never parsed; an honest body is refused by the
route's authentication while one over the limit is the 413, so the limit comes
first. The middleware itself is pinned in `services/common/tests/test_request_body_limit.py`.
"""

from __future__ import annotations

import uuid

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
_BODY_ROUTES = [("POST", "/devices"), ("POST", "/templates")]
_AUTHENTICATED_ROUTE = ("POST", "/devices")


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


# -- The upload routes take their own limit ------------------------------------------------
#
# The default plus the file bound each route already enforces: DRIVER_MAX_SIZE_BYTES for
# a driver package, and the import file bound `IMPORT_UPLOAD_BYTES` for a bulk import
# (whose own read is still unbounded, issue #1417). Both are read at each request, so
# they are shrunk here to keep the bodies small.

_UPLOAD_BYTES = 2000
_UPLOAD_ROUTES = {
    "driver_create": ("POST", "/drivers", {"name": "n", "connection_type": "ssh"}),
    "driver_replace": ("PUT", f"/drivers/{uuid.uuid4()}/file", {}),
    "device_import": ("POST", "/devices/import", {}),
    "template_import": ("POST", "/templates/import", {}),
}


@pytest.fixture
def small_upload_bounds(monkeypatch):
    from app.routers import bulk

    monkeypatch.setattr(settings, "driver_max_size_bytes", _UPLOAD_BYTES)
    monkeypatch.setattr(bulk, "IMPORT_UPLOAD_BYTES", _UPLOAD_BYTES)


@pytest.mark.usefixtures("small_upload_bounds")
@pytest.mark.parametrize(("method", "path", "data"), _UPLOAD_ROUTES.values(), ids=_UPLOAD_ROUTES)
async def test_an_upload_route_takes_its_own_limit(method, path, data):
    """Over the default but within the route's limit, the body reaches the route's
    admin gate (no credentials, so 401 or 403); over the route's limit it is the 413
    naming that limit."""
    within = {"file": ("f.bin", b"z" * (LIMIT + 100))}
    over = {"file": ("f.bin", b"z" * (LIMIT + _UPLOAD_BYTES + 1))}
    async with _client() as client:
        reached = await client.request(method, path, files=within, data=data)
        refused = await client.request(method, path, files=over, data=data)
    assert reached.status_code in (401, 403), reached.text[:300]
    assert refused.status_code == 413, refused.text[:300]
    assert refused.json() == {"detail": [body_too_large_error(LIMIT + _UPLOAD_BYTES)]}


def test_the_upload_routes_follow_the_driver_setting_and_the_import_bound():
    """Unshrunk: the driver routes add DRIVER_MAX_SIZE_BYTES, the imports 10 MiB."""
    from app.routers import bulk
    from fastapi.routing import iter_route_contexts
    from herd_common.validation_errors import route_body_limit

    limits = {
        (method, route.path): route_body_limit(route, LIMIT)
        for route in iter_route_contexts(app.routes)
        for method in route.methods or ()
    }
    assert bulk.IMPORT_UPLOAD_BYTES == 10 * 1024 * 1024
    driver = LIMIT + settings.driver_max_size_bytes
    assert limits[("POST", "/drivers")] == limits[("PUT", "/drivers/{driver_id}/file")] == driver
    assert limits[("POST", "/devices/import")] == LIMIT + bulk.IMPORT_UPLOAD_BYTES
    assert limits[("POST", "/templates/import")] == LIMIT + bulk.IMPORT_UPLOAD_BYTES
    assert limits[("POST", "/devices")] == LIMIT


# -- The large-JSON routes take the allowance (OPS-VALID-10, issue #1484) ----------------
#
# A device configuration version: the default limit plus LARGE_JSON_BODY_BYTES. A body
# that is not JSON at that
# limit is handed on whole and parsed (json_invalid); one byte more is the 413 naming the
# route's limit; a body just over the default reaches the route's authentication.

_LARGE_JSON_ROUTES = [("POST", f"/devices/{uuid.uuid4()}/config-versions")]


@pytest.mark.parametrize(("method", "path"), _LARGE_JSON_ROUTES)
async def test_a_large_json_route_takes_the_allowance(method, path):
    from herd_common.validation_errors import LARGE_JSON_BODY_BYTES

    route_limit = LIMIT + LARGE_JSON_BODY_BYTES
    async with _client() as client:
        over_default = await client.request(
            method, path, content=b"{}" + b" " * LIMIT, headers=_JSON
        )
        at = await client.request(method, path, content=b"x" * route_limit, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (route_limit + 1), headers=_JSON)
    assert over_default.status_code in (401, 403), over_default.text[:300]
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert over.status_code == 413, over.text[:300]
    assert over.json() == {"detail": [body_too_large_error(route_limit)]}
    assert "connection" not in over.headers


def test_the_larger_routes_are_exactly_the_uploads_and_the_config_version_create():
    from app.routers import bulk
    from fastapi.routing import iter_route_contexts
    from herd_common.validation_errors import LARGE_JSON_BODY_BYTES, route_body_limit

    limits = {
        (method, route.path): route_body_limit(route, LIMIT)
        for route in iter_route_contexts(app.routes)
        for method in route.methods or ()
    }
    driver = LIMIT + settings.driver_max_size_bytes
    assert {k: v for k, v in limits.items() if v != LIMIT} == {
        ("POST", "/drivers"): driver,
        ("PUT", "/drivers/{driver_id}/file"): driver,
        ("POST", "/devices/import"): LIMIT + bulk.IMPORT_UPLOAD_BYTES,
        ("POST", "/templates/import"): LIMIT + bulk.IMPORT_UPLOAD_BYTES,
        ("POST", "/devices/{device_id}/config-versions"): LIMIT + LARGE_JSON_BODY_BYTES,
    }
