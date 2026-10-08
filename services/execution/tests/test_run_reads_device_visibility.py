"""Run reads follow the caller's device-group visibility (CFG-RUN-3, CFG-TX-5).

A non-admin reservation owner's run list and run transcript read include only
runs on devices inventory's visible-devices answer lists for the caller. The
answer is resolved once per request with the caller's bearer; a lookup that
cannot be answered is 503 with no rows; a hidden run on the transcript read
answers byte for byte like an unknown run id. Admins are unfiltered and never
cause a lookup.
"""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from app.database import Base, get_db
from app.main import app
from app.routers import executions as ex_router
from app.routers.executions import get_current_user_payload, require_admin
from app.services import device_visibility
from app.services.device_visibility import (
    VISIBILITY_UNAVAILABLE_DETAIL,
    VisibleDevicesUnavailable,
    fetch_visible_device_ids,
    resolve_caller_visibility,
)
from app.services.execution_service import create_execution_run, insert_command_log
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_RealAsyncClient = httpx.AsyncClient

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RESERVATION_ID = uuid.uuid4()
VISIBLE_DEVICE = uuid.uuid4()
HIDDEN_DEVICE = uuid.uuid4()

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "alice", "role": "user"}
AUTH = {"Authorization": "Bearer user-token"}

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
def as_user(monkeypatch):
    """A non-admin owner of RESERVATION_ID (ownership is the first gate, stubbed true)."""
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    monkeypatch.setattr(ex_router, "_user_owns_reservation", AsyncMock(return_value=True))
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def as_admin():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[require_admin] = lambda: ADMIN_PAYLOAD
    yield
    app.dependency_overrides.clear()


async def _seed_run(device_id: uuid.UUID, *, with_command: bool = False) -> uuid.UUID:
    async with TestSessionLocal() as session:
        run = await create_execution_run(
            session,
            device_id=device_id,
            driver_id=uuid.uuid4(),
            driver_sha256="sha",
            action="connect_ports",
            user_id=uuid.uuid4(),
            input_params={"HERD_device_name": "switch"},
            reservation_id=RESERVATION_ID,
        )
        if with_command:
            await insert_command_log(session, run.id, [{"command": "show run", "response": "x"}])
        return run.id


def _inventory_answers(handler):
    """Route the visibility lookup's httpx client through a MockTransport handler."""

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _visible(*device_ids):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"device_ids": [str(d) for d in device_ids]})

    return handler


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- GET /runs ---


@pytest.mark.asyncio
async def test_owner_run_list_holds_only_visible_devices(as_user, monkeypatch):
    visible_run = await _seed_run(VISIBLE_DEVICE)
    await _seed_run(HIDDEN_DEVICE)
    await _seed_run(HIDDEN_DEVICE)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"device_ids": [str(VISIBLE_DEVICE)]})

    monkeypatch.setattr(device_visibility.httpx, "AsyncClient", _inventory_answers(handler))
    async with await _client() as ac:
        resp = await ac.get(f"/runs?reservation_id={RESERVATION_ID}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [str(visible_run)]
    assert body["total"] == 1
    # One lookup, with the caller's own bearer and identity.
    assert len(seen) == 1
    assert seen[0].url.path == "/device-groups/visible-devices"
    assert seen[0].url.params["user_id"] == USER_ID
    assert seen[0].headers["Authorization"] == "Bearer user-token"


@pytest.mark.asyncio
async def test_owner_run_list_empty_when_no_device_is_visible(as_user, monkeypatch):
    await _seed_run(HIDDEN_DEVICE)
    monkeypatch.setattr(device_visibility.httpx, "AsyncClient", _inventory_answers(_visible()))
    async with await _client() as ac:
        resp = await ac.get(f"/runs?reservation_id={RESERVATION_ID}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_owner_run_list_device_filter_on_hidden_device_is_empty(as_user, monkeypatch):
    await _seed_run(HIDDEN_DEVICE)
    monkeypatch.setattr(
        device_visibility.httpx, "AsyncClient", _inventory_answers(_visible(VISIBLE_DEVICE))
    )
    async with await _client() as ac:
        resp = await ac.get(
            f"/runs?reservation_id={RESERVATION_ID}&device_id={HIDDEN_DEVICE}", headers=AUTH
        )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def _transport_failure(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("inventory down")


def _non_200(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"detail": "boom"})


def _not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})


def _wrong_shape(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=["not", "an", "object"])


def _ids_not_strings(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"device_ids": [1, 2]})


def _ids_not_uuids(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"device_ids": ["not-a-uuid"]})


def _ids_missing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"devices": []})


_UNANSWERABLE = [
    _transport_failure,
    _non_200,
    _not_json,
    _wrong_shape,
    _ids_not_strings,
    _ids_not_uuids,
    _ids_missing,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", _UNANSWERABLE)
async def test_owner_run_list_fails_closed_when_visibility_unanswerable(
    as_user, monkeypatch, handler
):
    await _seed_run(VISIBLE_DEVICE)
    monkeypatch.setattr(device_visibility.httpx, "AsyncClient", _inventory_answers(handler))
    async with await _client() as ac:
        resp = await ac.get(f"/runs?reservation_id={RESERVATION_ID}", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"detail": VISIBILITY_UNAVAILABLE_DETAIL}


@pytest.mark.asyncio
async def test_owner_run_list_ownership_is_still_the_first_gate(as_user, monkeypatch):
    """A non-owner is refused 403 before any visibility lookup."""
    monkeypatch.setattr(ex_router, "_user_owns_reservation", AsyncMock(return_value=False))
    lookup = AsyncMock(side_effect=AssertionError("no lookup for a non-owner"))
    monkeypatch.setattr(device_visibility, "fetch_visible_device_ids", lookup)
    async with await _client() as ac:
        resp = await ac.get(f"/runs?reservation_id={RESERVATION_ID}", headers=AUTH)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Reservation not owned by caller"}
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_run_list_is_unfiltered_and_asks_nothing(as_admin, monkeypatch):
    await _seed_run(VISIBLE_DEVICE)
    await _seed_run(HIDDEN_DEVICE)
    lookup = AsyncMock(side_effect=AssertionError("admins are never filtered"))
    monkeypatch.setattr(device_visibility, "fetch_visible_device_ids", lookup)
    async with await _client() as ac:
        resp = await ac.get(f"/runs?reservation_id={RESERVATION_ID}")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2
    lookup.assert_not_awaited()


# --- GET /runs/{id}/commands ---


@pytest.mark.asyncio
async def test_owner_reads_transcript_of_visible_run(as_user, monkeypatch):
    run_id = await _seed_run(VISIBLE_DEVICE, with_command=True)
    monkeypatch.setattr(
        device_visibility.httpx, "AsyncClient", _inventory_answers(_visible(VISIBLE_DEVICE))
    )
    async with await _client() as ac:
        resp = await ac.get(f"/runs/{run_id}/commands", headers=AUTH)
    assert resp.status_code == 200
    assert [row["command"] for row in resp.json()] == ["show run"]


@pytest.mark.asyncio
async def test_hidden_run_transcript_answers_exactly_like_an_unknown_run(as_user, monkeypatch):
    hidden_run = await _seed_run(HIDDEN_DEVICE, with_command=True)
    monkeypatch.setattr(
        device_visibility.httpx, "AsyncClient", _inventory_answers(_visible(VISIBLE_DEVICE))
    )
    async with await _client() as ac:
        hidden = await ac.get(f"/runs/{hidden_run}/commands", headers=AUTH)
        unknown = await ac.get(f"/runs/{uuid.uuid4()}/commands", headers=AUTH)
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.content == unknown.content
    assert hidden.json() == {"detail": "Execution run not found"}
    assert hidden.headers["content-type"] == unknown.headers["content-type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", _UNANSWERABLE)
async def test_transcript_read_fails_closed_when_visibility_unanswerable(
    as_user, monkeypatch, handler
):
    run_id = await _seed_run(VISIBLE_DEVICE, with_command=True)
    monkeypatch.setattr(device_visibility.httpx, "AsyncClient", _inventory_answers(handler))
    async with await _client() as ac:
        resp = await ac.get(f"/runs/{run_id}/commands", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"detail": VISIBILITY_UNAVAILABLE_DETAIL}


@pytest.mark.asyncio
async def test_admin_reads_any_transcript_without_a_lookup(as_admin, monkeypatch):
    run_id = await _seed_run(HIDDEN_DEVICE, with_command=True)
    lookup = AsyncMock(side_effect=AssertionError("admins are never filtered"))
    monkeypatch.setattr(device_visibility, "fetch_visible_device_ids", lookup)
    async with await _client() as ac:
        resp = await ac.get(f"/runs/{run_id}/commands")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    lookup.assert_not_awaited()


# --- the helper itself ---


@pytest.mark.asyncio
async def test_fetch_visible_device_ids_without_a_token_is_unanswerable():
    with pytest.raises(VisibleDevicesUnavailable):
        await fetch_visible_device_ids(USER_ID, None)


@pytest.mark.asyncio
async def test_fetch_visible_device_ids_parses_the_answer(monkeypatch):
    monkeypatch.setattr(
        device_visibility.httpx,
        "AsyncClient",
        _inventory_answers(_visible(VISIBLE_DEVICE, HIDDEN_DEVICE)),
    )
    assert await fetch_visible_device_ids(USER_ID, "Bearer t") == {VISIBLE_DEVICE, HIDDEN_DEVICE}


@pytest.mark.asyncio
async def test_resolve_caller_visibility_is_none_for_admins():
    assert await resolve_caller_visibility(ADMIN_PAYLOAD, None) is None
    assert await resolve_caller_visibility({"sub": ADMIN_ID, "role": "superadmin"}, None) is None


@pytest.mark.asyncio
async def test_resolve_caller_visibility_is_503_for_an_unanswerable_non_admin():
    with pytest.raises(HTTPException) as exc_info:
        await resolve_caller_visibility(USER_PAYLOAD, None)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == VISIBILITY_UNAVAILABLE_DETAIL
