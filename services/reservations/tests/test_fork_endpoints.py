"""Router tests for the user-facing reservation fork endpoints (issue #25 P3a phase 3).

These cover the ownership/gating matrix and the cabling forwarding + error mapping.
The cabling HTTP calls are stubbed by patching the two service helpers the router
imports (_cabling_fork_call, _lazy_create_reservation_fork), so no cabling stack runs.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import (
    FORK_EDIT_REQUIRES_ACTIVE,
    FORK_SAVE_REQUIRES_ACTIVE,
    bearer_scheme,
)
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

OWNER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())

NOW = datetime.now(timezone.utc)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_bearer():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-token")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _client_as(sub: str, role: str = "user") -> AsyncClient:
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _insert_reservation(
    *,
    owner: str = OWNER_ID,
    status: ReservationStatus = ReservationStatus.ACTIVE,
    topology_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        res = Reservation(
            user_id=uuid.UUID(owner),
            owner_name="owner",
            device_ids=[str(uuid.uuid4())],
            topology_id=topology_id,
            topology_type=TopologyType.PHYSICAL,
            purpose="test",
            start_time=NOW - timedelta(hours=1),
            end_time=NOW + timedelta(hours=2),
            status=status,
        )
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


def _resp(status_code: int, json_body=None) -> httpx.Response:
    if json_body is None:
        return httpx.Response(status_code)
    return httpx.Response(status_code, json=json_body)


# --- GET /{id}/fork: ownership matrix -------------------------------------------------


@pytest.mark.asyncio
async def test_get_fork_owner_forwards_200():
    rid = await _insert_reservation()
    fork_body = {"id": str(uuid.uuid4()), "reservation_id": str(rid), "status": "ACTIVE"}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, fork_body)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 200
    assert resp.json() == fork_body
    call.assert_awaited_once_with("GET", f"/internal/forks/{rid}")


@pytest.mark.asyncio
async def test_get_fork_other_user_404_and_no_cabling_call():
    rid = await _insert_reservation()
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OTHER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Reservation not found"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_fork_admin_allowed_for_other_owner():
    rid = await _insert_reservation(owner=OWNER_ID)
    fork_body = {"reservation_id": str(rid), "status": "ACTIVE"}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, fork_body)),
    ):
        async with _client_as(ADMIN_ID, role="admin") as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 200


# --- GET /{id}/fork: lazy-create semantics -------------------------------------------


@pytest.mark.asyncio
async def test_get_fork_lazy_creates_on_active_miss():
    topo = uuid.uuid4()
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE, topology_id=topo)
    created_body = {"reservation_id": str(rid), "status": "ACTIVE"}
    call = AsyncMock(side_effect=[_resp(404), _resp(200, created_body)])
    lazy = AsyncMock()
    with (
        patch("app.routers.reservations._cabling_fork_call", new=call),
        patch("app.routers.reservations._lazy_create_reservation_fork", new=lazy),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 200
    assert resp.json() == created_body
    lazy.assert_awaited_once_with(rid, topo, OWNER_ID)
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_get_fork_lazy_creates_with_no_parent_topology():
    """Case A: an ACTIVE reservation with topology_id NULL still lazy-creates a fork."""
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE, topology_id=None)
    call = AsyncMock(side_effect=[_resp(404), _resp(200, {"reservation_id": str(rid)})])
    lazy = AsyncMock()
    with (
        patch("app.routers.reservations._cabling_fork_call", new=call),
        patch("app.routers.reservations._lazy_create_reservation_fork", new=lazy),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 200
    lazy.assert_awaited_once_with(rid, None, OWNER_ID)


@pytest.mark.asyncio
async def test_get_fork_ended_reservation_no_fork_404_and_no_lazy_create():
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED, topology_id=uuid.uuid4())
    call = AsyncMock(return_value=_resp(404))
    lazy = AsyncMock()
    with (
        patch("app.routers.reservations._cabling_fork_call", new=call),
        patch("app.routers.reservations._lazy_create_reservation_fork", new=lazy),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Fork not found"
    lazy.assert_not_awaited()
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_get_fork_reads_ended_reservation_when_fork_exists():
    """As-built read is allowed for a terminal reservation whose fork exists."""
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED, topology_id=uuid.uuid4())
    body = {"reservation_id": str(rid), "status": "ARCHIVED"}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ARCHIVED"


@pytest.mark.asyncio
async def test_get_fork_cabling_5xx_maps_to_503():
    rid = await _insert_reservation()
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(500, {"detail": "boom"})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_get_fork_cabling_unreachable_maps_to_503():
    rid = await _insert_reservation()
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 503


# --- PUT /{id}/fork/canvas -----------------------------------------------------------


@pytest.mark.asyncio
async def test_put_canvas_active_forwards():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    ok = {"id": str(uuid.uuid4()), "valid": True, "invalid_edges": []}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, ok)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.put(f"/{rid}/fork/canvas", json={"canvas_data": {"nodes": []}})
    assert resp.status_code == 200
    assert resp.json() == ok
    call.assert_awaited_once_with(
        "PUT", f"/internal/forks/{rid}/canvas", json_body={"canvas_data": {"nodes": []}}
    )


@pytest.mark.asyncio
async def test_put_canvas_non_active_409_pinned_wording():
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED)
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.put(f"/{rid}/fork/canvas", json={"canvas_data": {}})
    assert resp.status_code == 409
    assert resp.json()["detail"] == FORK_EDIT_REQUIRES_ACTIVE
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_canvas_archived_409_passthrough():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(409, {"detail": "Fork is archived and cannot be edited"})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.put(f"/{rid}/fork/canvas", json={"canvas_data": {}})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Fork is archived and cannot be edited"


@pytest.mark.asyncio
async def test_put_canvas_cabling_unreachable_503():
    """Mirrors the GET and save siblings: a transport failure maps to 503."""
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.put(f"/{rid}/fork/canvas", json={"canvas_data": {}})
    assert resp.status_code == 503


# --- POST /{id}/fork/save ------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_active_forwards_and_stamps_created_by():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    result = {"fork_id": str(uuid.uuid4()), "version_number": 2, "released": [], "built": []}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, result)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {"nodes": []}})
    assert resp.status_code == 200
    call.assert_awaited_once_with(
        "POST",
        f"/internal/forks/{rid}/save",
        json_body={"canvas_data": {"nodes": []}, "created_by": OWNER_ID},
    )


@pytest.mark.asyncio
async def test_save_non_active_409_pinned_wording():
    rid = await _insert_reservation(status=ReservationStatus.CANCELLED)
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {}})
    assert resp.status_code == 409
    assert resp.json()["detail"] == FORK_SAVE_REQUIRES_ACTIVE
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_port_conflict_409_structured_passthrough():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    structured = {
        "detail": {
            "error": "port_conflict",
            "conflicts": [{"reservation_id": str(uuid.uuid4()), "port": "eth0"}],
        }
    }
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(409, structured)),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {}})
    assert resp.status_code == 409
    # The structured detail dict is relayed verbatim, not stringified.
    assert resp.json()["detail"] == structured["detail"]


@pytest.mark.asyncio
async def test_save_cabling_unreachable_503():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {}})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_save_stage_wiring_changed_failure_still_returns_200():
    """A staging failure must never turn a durable cabling save into a user error.

    Patches stage_wiring_changed in the ROUTER module namespace (the actual
    save-handler call site), not the service module: the existing staging-failure
    coverage patches app.services.reservation_service.stage_wiring_changed, which
    pins a different call site (_prune_removed_devices_from_fork), not this one.
    """
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    result = {
        "fork_id": str(uuid.uuid4()),
        "version_number": 3,
        "released": [],
        "built": [],
    }
    with (
        patch(
            "app.routers.reservations._cabling_fork_call",
            new=AsyncMock(return_value=_resp(200, result)),
        ),
        patch(
            "app.routers.reservations.stage_wiring_changed",
            new=AsyncMock(side_effect=RuntimeError("staging boom")),
        ) as staged,
        patch(
            "app.database.AsyncSession.rollback",
            new=AsyncMock(),
        ) as rollback,
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {"nodes": []}})

    assert resp.status_code == 200
    assert resp.json() == result
    staged.assert_awaited_once()
    rollback.assert_awaited_once()
