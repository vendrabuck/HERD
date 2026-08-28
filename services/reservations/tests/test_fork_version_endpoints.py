"""GET .../fork/versions/{id} and POST .../fork/versions/{id}/restore (issue #622).

Mirrors test_fork_endpoints.py's conventions: the cabling HTTP call is stubbed by
patching ``_cabling_fork_call``, so no cabling stack runs. These cover the
ownership/status gating matrix and the forwarding + error mapping; the actual
restore-to-draft behavior (canvas replace, version append, fork_connections
untouched) is cabling-side and pinned by services/cabling/tests/test_fork_versions.py.
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
from app.routers.reservations import FORK_RESTORE_REQUIRES_ACTIVE, bearer_scheme
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


# --- GET /{id}/fork/versions/{version_id} --------------------------------------------


@pytest.mark.asyncio
async def test_get_fork_version_owner_forwards_200():
    rid = await _insert_reservation()
    version_id = uuid.uuid4()
    body = {"id": str(version_id), "fork_id": str(uuid.uuid4()), "version_number": 1}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork/versions/{version_id}")
    assert resp.status_code == 200
    assert resp.json() == body
    call.assert_awaited_once_with("GET", f"/internal/forks/{rid}/versions/{version_id}")


@pytest.mark.asyncio
async def test_get_fork_version_other_user_404_and_no_cabling_call():
    rid = await _insert_reservation()
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OTHER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork/versions/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Reservation not found"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_fork_version_admin_allowed_for_other_owner():
    rid = await _insert_reservation(owner=OWNER_ID)
    body = {"id": str(uuid.uuid4()), "fork_id": str(uuid.uuid4()), "version_number": 1}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ):
        async with _client_as(ADMIN_ID, role="admin") as ac:
            resp = await ac.get(f"/{rid}/fork/versions/{uuid.uuid4()}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_fork_version_allowed_on_completed_reservation():
    """Any reservation status may read a past version, same rule as GET /{id}/fork."""
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED)
    body = {"id": str(uuid.uuid4()), "fork_id": str(uuid.uuid4()), "version_number": 3}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, body)),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork/versions/{uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json() == body


@pytest.mark.asyncio
async def test_get_fork_version_cabling_404_passthrough():
    """A foreign or nonexistent version relays cabling's 404 verbatim."""
    rid = await _insert_reservation()
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(404, {"detail": "Version not found"})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork/versions/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Version not found"


@pytest.mark.asyncio
async def test_get_fork_version_cabling_unreachable_503():
    rid = await _insert_reservation()
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.get(f"/{rid}/fork/versions/{uuid.uuid4()}")
    assert resp.status_code == 503


# --- POST /{id}/fork/versions/{version_id}/restore ------------------------------------


@pytest.mark.asyncio
async def test_restore_active_owner_forwards():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    version_id = uuid.uuid4()
    result = {
        "id": str(uuid.uuid4()),
        "valid": True,
        "invalid_edges": [],
        "version": {"version_number": 2, "restored_from_id": str(version_id)},
    }
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, result)),
    ) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{version_id}/restore")
    assert resp.status_code == 200
    assert resp.json() == result
    call.assert_awaited_once_with("POST", f"/internal/forks/{rid}/versions/{version_id}/restore")


@pytest.mark.asyncio
async def test_restore_active_admin_allowed_for_other_owner():
    rid = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    result = {"id": str(uuid.uuid4()), "valid": True, "invalid_edges": [], "version": {}}
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, result)),
    ):
        async with _client_as(ADMIN_ID, role="admin") as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_restore_other_user_404_and_no_cabling_call():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OTHER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Reservation not found"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_pending_409_and_no_cabling_call():
    rid = await _insert_reservation(status=ReservationStatus.PENDING)
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == FORK_RESTORE_REQUIRES_ACTIVE
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_completed_409_and_no_cabling_call():
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED)
    with patch("app.routers.reservations._cabling_fork_call", new=AsyncMock()) as call:
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == FORK_RESTORE_REQUIRES_ACTIVE
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_409_body_is_structured_error_shape():
    """Issue #622's contract pins {"error": "reservation_not_active"}, not a string."""
    rid = await _insert_reservation(status=ReservationStatus.CANCELLED)
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "reservation_not_active"}


@pytest.mark.asyncio
async def test_restore_cabling_404_passthrough():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(404, {"detail": "Version not found"})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Version not found"


@pytest.mark.asyncio
async def test_restore_cabling_409_archived_passthrough():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(409, {"detail": "Fork is archived and cannot be edited"})),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Fork is archived and cannot be edited"


@pytest.mark.asyncio
async def test_restore_cabling_unreachable_503():
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service")),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_restore_does_not_stage_wiring_changed():
    """Restore forwards to cabling only; it must never call the execution/outbox
    staging path save_reservation_fork uses (stage_wiring_changed)."""
    rid = await _insert_reservation(status=ReservationStatus.ACTIVE)
    result = {"id": str(uuid.uuid4()), "valid": True, "invalid_edges": [], "version": {}}
    with (
        patch(
            "app.routers.reservations._cabling_fork_call",
            new=AsyncMock(return_value=_resp(200, result)),
        ),
        patch("app.routers.reservations.stage_wiring_changed", new=AsyncMock()) as stage,
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(f"/{rid}/fork/versions/{uuid.uuid4()}/restore")
    assert resp.status_code == 200
    stage.assert_not_awaited()
