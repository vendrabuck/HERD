"""Topology DELETE guard (issue #977).

The invariant: no topology row referenced by a PENDING, PENDING_PROVISION, or
ACTIVE reservation is deleted. The route refuses with 409 topology_in_use, the
lookup fails CLOSED (503, nothing deleted) when reservations cannot answer, and
the existing 404 and 403 run first so an unauthorized caller learns nothing
about reservations. The edit-lock lookup `find_blocking_reservations` keeps its
fail-open behavior; the paired tests below pin that both answers coexist.

Route handlers are called directly (the repo's convention for coverage
attribution, see test_route_handlers_direct.py), with reservations mocked at the
httpx.AsyncClient boundary so the strict parser runs for real. One ASGI test
pins the 409 body as FastAPI serializes it.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models import *  # noqa: F401, F403  (register every table on Base.metadata)
from app.models.topology import Topology
from app.routes.topologies import create_topology, delete_topology
from app.schemas.topology import TopologyCreate
from app.services import reservation_guard
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

OWNER_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()

LIVE_STATUSES = ["PENDING", "PENDING_PROVISION", "ACTIVE"]
TERMINAL_STATUSES = ["COMPLETED", "CANCELLED", "FAILED"]


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _owner(role="user"):
    return {"sub": str(OWNER_ID), "username": "owner", "role": role}


async def _make_topology() -> uuid.UUID:
    async with TestSession() as db:
        created = await create_topology(
            body=TopologyCreate(name=f"Lab {uuid.uuid4().hex[:6]}"), payload=_owner(), db=db
        )
        return created.id


async def _exists(topology_id: uuid.UUID) -> bool:
    async with TestSession() as db:
        return await db.get(Topology, topology_id) is not None


def _reservations(*, status_code=200, body=None, raise_exc=None, json_exc=None):
    """Patch target for httpx.AsyncClient answering the by-topology GET."""
    response = MagicMock()
    response.status_code = status_code
    if json_exc is not None:
        response.json = MagicMock(side_effect=json_exc)
    else:
        response.json = MagicMock(return_value=body)
    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        client.get = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), client


def _row(topology_id, status, rid=None):
    return {
        "id": rid or str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "topology_id": str(topology_id),
        "status": status,
        "end_time": "2026-10-05T00:00:00+00:00",
    }


async def _delete(topology_id, payload=None):
    async with TestSession() as db:
        await delete_topology(topology_id=topology_id, payload=payload or _owner(), db=db)


# --- refusal and success -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE_STATUSES)
async def test_live_reservation_blocks_delete_with_its_id(status):
    tid = await _make_topology()
    rid = str(uuid.uuid4())
    factory, client = _reservations(body=[_row(tid, status, rid)])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc_info:
            await _delete(tid)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"error": "topology_in_use", "reservation_ids": [rid]}
    assert await _exists(tid)
    url = client.get.call_args.args[0]
    assert url.endswith(f"/internal/by-topology/{tid}")
    assert "X-Internal-Token" in client.get.call_args.kwargs["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
async def test_terminal_reservation_does_not_block(status):
    tid = await _make_topology()
    factory, _ = _reservations(body=[_row(tid, status)])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        await _delete(tid)
    assert not await _exists(tid)


@pytest.mark.asyncio
async def test_no_reservation_deletes():
    tid = await _make_topology()
    factory, _ = _reservations(body=[])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        await _delete(tid)
    assert not await _exists(tid)


@pytest.mark.asyncio
async def test_mixed_rows_report_only_live_ids_sorted_and_deduped():
    tid = await _make_topology()
    ids = sorted(str(uuid.uuid4()) for _ in range(3))
    body = [
        _row(tid, "ACTIVE", ids[2]),
        _row(tid, "COMPLETED"),
        _row(tid, "PENDING", ids[0]),
        _row(tid, "cancelled"),
        _row(tid, "pending_provision", ids[1]),
        _row(tid, "PENDING", ids[0]),
    ]
    factory, _ = _reservations(body=body)
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc_info:
            await _delete(tid, payload=_owner(role="admin"))
    assert exc_info.value.detail["reservation_ids"] == ids
    assert await _exists(tid)


# --- fail closed --------------------------------------------------------------


_UNVERIFIABLE_ANSWERS = {
    "transport_error": dict(raise_exc=httpx.ConnectError("connection refused")),
    "unexpected_exception": dict(raise_exc=RuntimeError("boom")),
    "http_500": dict(status_code=500, body={"detail": "boom"}),
    "http_503": dict(status_code=503, body=[]),
    "http_404": dict(status_code=404, body={"detail": "Not Found"}),
    "http_403_bad_token": dict(status_code=403, body={"detail": "Invalid internal token"}),
    "invalid_json": dict(json_exc=json.JSONDecodeError("bad", "x", 0)),
    "object_not_list": dict(body={"items": []}),
    "null_body": dict(body=None),
    "item_not_object": dict(body=["not-a-row"]),
    "item_missing_id": dict(body="MISSING_ID"),
    "item_missing_status": dict(body="MISSING_STATUS"),
    "status_not_string": dict(body="STATUS_INT"),
    "unknown_status": dict(body="UNKNOWN_STATUS"),
    "other_topology": dict(body="OTHER_TOPOLOGY"),
    "missing_topology_id": dict(body="MISSING_TOPOLOGY"),
}


def _shape(tid, marker):
    """Expand a body marker into a malformed by-topology answer for tid."""
    good = _row(tid, "COMPLETED")
    if marker == "MISSING_ID":
        good.pop("id")
    elif marker == "MISSING_STATUS":
        good.pop("status")
    elif marker == "STATUS_INT":
        good["status"] = 3
    elif marker == "UNKNOWN_STATUS":
        good["status"] = "ON_HOLD"
    elif marker == "OTHER_TOPOLOGY":
        good["topology_id"] = str(uuid.uuid4())
    elif marker == "MISSING_TOPOLOGY":
        good.pop("topology_id")
    else:
        return marker
    return [good]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_UNVERIFIABLE_ANSWERS))
async def test_unverifiable_answer_is_503_and_nothing_deleted(case):
    tid = await _make_topology()
    kwargs = dict(_UNVERIFIABLE_ANSWERS[case])
    if "body" in kwargs:
        kwargs["body"] = _shape(tid, kwargs["body"])
    factory, _ = _reservations(**kwargs)
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc_info:
            await _delete(tid)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == reservation_guard.TOPOLOGY_DELETE_UNVERIFIABLE_DETAIL
    assert await _exists(tid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["transport_error", "unexpected_exception", "http_500", "http_503", "http_404"]
)
async def test_edit_lock_lookup_still_fails_open_on_the_same_answers(case):
    """The strict variant must not have changed the edit lock's contract."""
    tid = uuid.uuid4()
    factory, _ = _reservations(**_UNVERIFIABLE_ANSWERS[case])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        assert await reservation_guard.find_blocking_reservations(tid) == []


@pytest.mark.asyncio
async def test_edit_lock_lookup_still_skips_other_topology_rows():
    tid = uuid.uuid4()
    body = [_row(uuid.uuid4(), "ACTIVE"), _row(tid, "ACTIVE", "r1")]
    factory, _ = _reservations(body=body)
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        result = await reservation_guard.find_blocking_reservations(tid)
    assert [r["id"] for r in result] == ["r1"]


# --- 404 and 403 come first ---------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_topology_is_404_without_asking_reservations():
    factory, client = _reservations(body=[])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc_info:
            await _delete(uuid.uuid4())
    assert exc_info.value.status_code == 404
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_non_owner_is_403_without_asking_reservations():
    """Even when a live reservation exists, a non-owner sees only the 403."""
    tid = await _make_topology()
    factory, client = _reservations(body=[_row(tid, "ACTIVE")])
    other = {"sub": str(OTHER_ID), "username": "other", "role": "user"}
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc_info:
            await _delete(tid, payload=other)
    assert exc_info.value.status_code == 403
    client.get.assert_not_called()
    assert await _exists(tid)


# --- wire shape -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_409_body_shape():
    tid = await _make_topology()
    rid = str(uuid.uuid4())

    async def _db():
        async with TestSession() as session:
            yield session

    app.dependency_overrides[get_current_user_payload] = _owner
    app.dependency_overrides[get_db] = _db
    try:
        factory, _ = _reservations(body=[_row(tid, "PENDING", rid)])
        with patch.object(reservation_guard.httpx, "AsyncClient", factory):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                resp = await ac.delete(f"/topologies/{tid}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert resp.json() == {"detail": {"error": "topology_in_use", "reservation_ids": [rid]}}
