"""Router tests for the user-facing wiring-status and wiring-retry proxies.

ADR 0007 Decision 6 (issue #345 P3b phase 4). Cover the owner-or-admin gating matrix
(the fork-endpoint idiom), the ACTIVE-only guard on retry with its pinned 409 wording,
verbatim relay of execution's structured responses, and the 503 mappings. The execution
HTTP call is stubbed by patching the single helper the router imports
(_execution_wiring_call), so no execution stack runs.
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
from app.routers.reservations import WIRING_RETRY_NOT_PROVISIONED, bearer_scheme
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
    *, owner: str = OWNER_ID, status: ReservationStatus = ReservationStatus.ACTIVE
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        res = Reservation(
            user_id=uuid.UUID(owner),
            owner_name="owner",
            device_ids=[str(uuid.uuid4())],
            topology_id=None,
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


# --- GET /{id}/wiring-status: ownership matrix ------------------------------


@pytest.mark.asyncio
async def test_status_owner_forwards_200():
    rid = await _insert_reservation()
    body = {"reservation_id": str(rid), "frozen": False, "connections": []}
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/wiring-status")
    assert resp.status_code == 200
    assert resp.json() == body
    call.assert_awaited_once_with("GET", f"/internal/reservations/{rid}/wiring-status")


@pytest.mark.asyncio
async def test_status_other_user_404_and_no_execution_call():
    rid = await _insert_reservation()
    with patch("app.routers.reservations._execution_wiring_call", new=AsyncMock()) as call:
        async with _client_as(OTHER_ID) as ac:
            resp = await ac.get(f"/{rid}/wiring-status")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Reservation not found"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_admin_allowed_for_other_owner():
    rid = await _insert_reservation(owner=OWNER_ID)
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(200, {"reservation_id": str(rid), "connections": []})),
    ):
        async with _client_as(ADMIN_ID, role="admin") as ac:
            resp = await ac.get(f"/{rid}/wiring-status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_status_allowed_for_completed_reservation():
    """As-built read: wiring-status is allowed for any status, like GET /{id}/fork."""
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED)
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(200, {"reservation_id": str(rid), "connections": []})),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/wiring-status")
    assert resp.status_code == 200
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_execution_5xx_maps_to_503():
    rid = await _insert_reservation()
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(500, {"detail": "boom"})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/wiring-status")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_status_execution_unreachable_maps_to_503():
    rid = await _insert_reservation()
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact execution service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/wiring-status")
    assert resp.status_code == 503


# --- POST /{id}/wiring/retry ------------------------------------------------


@pytest.mark.asyncio
async def test_retry_owner_forwards_and_relays():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    body = {"reservation_id": str(rid), "results": [{"outcome": "reconnected"}]}
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 200
    assert resp.json() == body
    call.assert_awaited_once_with("POST", f"/internal/reservations/{rid}/wiring/retry")


@pytest.mark.asyncio
async def test_retry_other_user_404_and_no_execution_call():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch("app.routers.reservations._execution_wiring_call", new=AsyncMock()) as call:
        async with _client_as(OTHER_ID) as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 404
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_admin_allowed():
    rid = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(200, {"reservation_id": str(rid), "results": []})),
    ):
        async with _client_as(ADMIN_ID, role="admin") as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "status",
    [ReservationStatus.PENDING, ReservationStatus.PENDING_PROVISION],
)
@pytest.mark.asyncio
async def test_retry_pre_provision_409_pinned_wording(status):
    """PENDING and PENDING_PROVISION have no provisioned wiring, so retry is refused 409."""
    rid = await _insert_reservation(status=status)
    with patch("app.routers.reservations._execution_wiring_call", new=AsyncMock()) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"] == WIRING_RETRY_NOT_PROVISIONED
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    [
        ReservationStatus.COMPLETED,
        ReservationStatus.CANCELLED,
        ReservationStatus.FAILED,
    ],
)
@pytest.mark.asyncio
async def test_retry_allowed_for_terminal_statuses(status):
    """ADR 0009 phase 3 (issue #369): retry is permitted for the terminal statuses so a
    stuck release-direction disconnect can finish after a reservation ends. The proxy
    forwards to execution, which scopes the wiring freeze to build-direction rows."""
    rid = await _insert_reservation(status=status)
    body = {"reservation_id": str(rid), "results": []}
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 200
    call.assert_awaited_once_with("POST", f"/internal/reservations/{rid}/wiring/retry")


@pytest.mark.asyncio
async def test_retry_execution_frozen_409_passthrough():
    """Execution's own 409 (wiring frozen) relays to the user verbatim."""
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    detail = "Reservation wiring is frozen; retry is not allowed"
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(return_value=_resp(409, {"detail": detail})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"] == detail


@pytest.mark.asyncio
async def test_retry_execution_unreachable_maps_to_503():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._execution_wiring_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact execution service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/wiring/retry")
    assert resp.status_code == 503
