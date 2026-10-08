"""POST /execute: a reservation_id must name a reservation that holds the device and,
for a non-admin, one the caller owns (CFG-EXEC-3, issue #1112).

The check reads reservations' GET /internal/by-device/{device_id} with the internal
token before any device read, run row, or driver call; it fails closed with 503.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.database import Base, get_db
from app.main import app
from app.models.execution_run import ExecutionRun
from app.routers import executions as ex_router
from app.routers.executions import (
    EXECUTE_RESERVATION_MISMATCH_ADMIN_DETAIL,
    EXECUTE_RESERVATION_MISMATCH_DETAIL,
    EXECUTE_RESERVATION_UNAVAILABLE_DETAIL,
    _require_internal_token,
    get_current_user_payload,
    require_admin,
)
from app.services import execution_service as ex_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_RealAsyncClient = httpx.AsyncClient

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
OWN_RESERVATION = str(uuid.uuid4())
OTHER_RESERVATION = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "alice", "role": "user"}

# The reservations holding DEVICE_ID, as reservations' by-device route lists them.
HOLDERS = [
    {"id": OWN_RESERVATION, "user_id": USER_ID, "device_id": DEVICE_ID, "status": "ACTIVE"},
    {
        "id": OTHER_RESERVATION,
        "user_id": OTHER_USER_ID,
        "device_id": DEVICE_ID,
        "status": "PENDING",
    },
]

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
def pipeline(monkeypatch):
    """Stub everything after the gate; a non-admin always holds the manage grant."""
    monkeypatch.setattr(ex_router.settings, "internal_api_token", "internal-token")
    monkeypatch.setattr(ex_router, "_user_has_acl_manage", AsyncMock(return_value=True))
    device = {
        "id": DEVICE_ID,
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": "Management",
        "template_id": TEMPLATE_ID,
    }
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        ex_router, "fetch_template", AsyncMock(return_value={"id": TEMPLATE_ID, "sections": []})
    )
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_config_schema", AsyncMock(return_value=None))
    monkeypatch.setattr(ex_service, "validate_device_config", lambda *a, **k: None)
    monkeypatch.setattr(ex_service, "validate_device_config_with_schema", lambda *a, **k: None)
    sandbox = MagicMock(return_value={"success": True, "output": None, "duration_ms": 1})
    monkeypatch.setattr(ex_service, "execute_driver_method", sandbox)
    return sandbox


def _client_as(payload: dict) -> AsyncClient:
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    app.dependency_overrides[require_admin] = lambda: payload
    app.dependency_overrides[_require_internal_token] = lambda: None
    app.dependency_overrides[get_db] = _override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _reservations(handler, seen: list | None = None):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=httpx.MockTransport(wrapped), **kwargs)

    return factory


def _holders(rows):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    return handler


def _body(reservation_id: str | None) -> dict:
    body = {
        "device_id": DEVICE_ID,
        "action": "configure",
        "user_id": USER_ID,
        "method_kwargs": {"hostname": "r1"},
    }
    if reservation_id is not None:
        body["reservation_id"] = reservation_id
    return body


async def _run_count() -> int:
    async with TestSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(ExecutionRun))).scalar()


@pytest.mark.asyncio
async def test_owner_runs_configure_under_their_own_reservation(pipeline, monkeypatch):
    seen: list = []
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _reservations(_holders(HOLDERS), seen))
    async with _client_as(USER_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OWN_RESERVATION))
    assert resp.status_code == 201
    assert resp.json()["reservation_id"] == OWN_RESERVATION
    assert pipeline.call_count == 1
    # One read, with the internal token, scoped to the device.
    assert len(seen) == 1
    assert seen[0].url.path == f"/internal/by-device/{DEVICE_ID}"
    assert seen[0].headers["X-Internal-Token"] == "internal-token"


@pytest.mark.asyncio
async def test_non_admin_cannot_tag_a_run_with_another_users_reservation(pipeline, monkeypatch):
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _reservations(_holders(HOLDERS)))
    async with _client_as(USER_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OTHER_RESERVATION))
    assert resp.status_code == 422
    assert resp.json() == {"detail": EXECUTE_RESERVATION_MISMATCH_DETAIL}
    pipeline.assert_not_called()
    ex_router.fetch_device.assert_not_awaited()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_non_admin_cannot_tag_a_run_with_a_reservation_without_the_device(
    pipeline, monkeypatch
):
    unknown = str(uuid.uuid4())
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _reservations(_holders(HOLDERS)))
    async with _client_as(USER_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(unknown))
    assert resp.status_code == 422
    assert resp.json() == {"detail": EXECUTE_RESERVATION_MISMATCH_DETAIL}
    pipeline.assert_not_called()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_admin_may_tag_a_run_with_another_users_reservation_holding_the_device(
    pipeline, monkeypatch
):
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _reservations(_holders(HOLDERS)))
    async with _client_as(ADMIN_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OTHER_RESERVATION))
    assert resp.status_code == 201
    assert resp.json()["reservation_id"] == OTHER_RESERVATION


@pytest.mark.asyncio
async def test_admin_cannot_tag_a_run_with_a_reservation_without_the_device(pipeline, monkeypatch):
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _reservations(_holders([])))
    async with _client_as(ADMIN_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OWN_RESERVATION))
    assert resp.status_code == 422
    assert resp.json() == {"detail": EXECUTE_RESERVATION_MISMATCH_ADMIN_DETAIL}
    pipeline.assert_not_called()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_execute_without_a_reservation_asks_nothing(pipeline, monkeypatch):
    lookup = MagicMock(side_effect=AssertionError("no reservation, no lookup"))
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lookup)
    async with _client_as(USER_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(None))
    assert resp.status_code == 201
    assert resp.json()["reservation_id"] is None
    lookup.assert_not_called()


def _transport_failure(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("reservations down")


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"detail": "boom"})


def _forbidden(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"detail": "Invalid internal token"})


def _not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})


_UNANSWERABLE = [
    _transport_failure,
    _server_error,
    _forbidden,
    _not_json,
    _holders({"id": OWN_RESERVATION}),
    _holders(["not-an-object"]),
    _holders([{"id": OWN_RESERVATION}]),
    _holders([{"id": OWN_RESERVATION, "user_id": 5}]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    _UNANSWERABLE,
    ids=[
        "transport",
        "server_error",
        "forbidden",
        "not_json",
        "not_a_list",
        "row_not_an_object",
        "row_without_user_id",
        "user_id_not_a_string",
    ],
)
async def test_reservation_check_fails_closed(pipeline, monkeypatch, handler):
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _reservations(handler))
    async with _client_as(USER_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OWN_RESERVATION))
    assert resp.status_code == 503
    assert resp.json() == {"detail": EXECUTE_RESERVATION_UNAVAILABLE_DETAIL}
    pipeline.assert_not_called()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_reservation_check_without_an_internal_token_fails_closed(pipeline, monkeypatch):
    monkeypatch.setattr(ex_router.settings, "internal_api_token", "")
    lookup = MagicMock(side_effect=AssertionError("no token, no lookup"))
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lookup)
    async with _client_as(ADMIN_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OWN_RESERVATION))
    assert resp.status_code == 503
    assert resp.json() == {"detail": EXECUTE_RESERVATION_UNAVAILABLE_DETAIL}
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_non_admin_without_a_grant_is_refused_before_the_reservation_check(
    pipeline, monkeypatch
):
    monkeypatch.setattr(ex_router, "_user_has_acl_manage", AsyncMock(return_value=False))
    lookup = MagicMock(side_effect=AssertionError("authorization comes first"))
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lookup)
    async with _client_as(USER_PAYLOAD) as ac:
        resp = await ac.post("/execute", json=_body(OTHER_RESERVATION))
    assert resp.status_code == 403
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_internal_execute_reservation_is_not_checked(pipeline, monkeypatch):
    lookup = MagicMock(side_effect=AssertionError("internal execute trusts its caller"))
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lookup)
    async with _client_as(ADMIN_PAYLOAD) as ac:
        resp = await ac.post("/execute/internal", json=_body(OTHER_RESERVATION))
    assert resp.status_code == 201
    assert resp.json()["reservation_id"] == OTHER_RESERVATION
    lookup.assert_not_called()
