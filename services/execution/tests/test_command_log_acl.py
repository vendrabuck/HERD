"""ACL tests for GET /runs/{run_id}/commands.

Mirrors the iter-2 carve-out on GET /runs: admins always pass; non-admin
reservation owners pass when the run is tied to their reservation. Runs with
no reservation_id (e.g. ad-hoc device checks) remain admin-only.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.database import Base, get_db
from app.main import app
from app.routers import executions as ex_router
from app.routers.executions import get_current_user_payload, require_admin
from app.services.execution_service import create_execution_run, insert_command_log
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RESERVATION_ID = uuid.uuid4()

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "alice", "role": "user"}

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


async def _seed_run_with_commands(reservation_id: uuid.UUID | None) -> uuid.UUID:
    async with TestSessionLocal() as session:
        run = await create_execution_run(
            session,
            device_id=uuid.uuid4(),
            driver_id=uuid.uuid4(),
            driver_sha256="sha",
            action="configure",
            user_id=uuid.UUID(USER_ID),
            input_params={},
            reservation_id=reservation_id,
        )
        await insert_command_log(
            session,
            run.id,
            [{"command": "vlan 100", "response": "OK"}],
        )
        return run.id


def _mock_reservations_response(status_code: int):
    """Patch httpx.AsyncClient so the reservations-service ownership lookup
    returns the given status. 200 means the caller owns the reservation; 404
    means they don't.
    """
    resp = MagicMock()
    resp.status_code = status_code

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.fixture
def _override_db():
    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_can_read_any_run(_override_db):
    """Admin sees the transcript regardless of run.reservation_id."""
    run_id = await _seed_run_with_commands(reservation_id=None)
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[require_admin] = lambda: ADMIN_PAYLOAD
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/runs/{run_id}/commands")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_non_admin_owner_can_read(_override_db, monkeypatch):
    """Non-admin who owns the reservation gets 200."""
    run_id = await _seed_run_with_commands(reservation_id=RESERVATION_ID)
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    monkeypatch.setattr(
        ex_router.httpx, "AsyncClient", lambda *a, **kw: _mock_reservations_response(200)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            f"/runs/{run_id}/commands",
            headers={"Authorization": "Bearer fake-token"},
        )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_non_admin_non_owner_rejected(_override_db, monkeypatch):
    """Non-admin who does not own the reservation gets 403."""
    run_id = await _seed_run_with_commands(reservation_id=RESERVATION_ID)
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    monkeypatch.setattr(
        ex_router.httpx, "AsyncClient", lambda *a, **kw: _mock_reservations_response(404)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            f"/runs/{run_id}/commands",
            headers={"Authorization": "Bearer fake-token"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_run_without_reservation_rejected(_override_db):
    """Run with no reservation_id is admin-only even for would-be owners."""
    run_id = await _seed_run_with_commands(reservation_id=None)
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/runs/{run_id}/commands")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reservations_service_error_is_closed_by_default(_override_db, monkeypatch):
    """Reservations service returning 5xx is treated as 'not owned'."""
    run_id = await _seed_run_with_commands(reservation_id=RESERVATION_ID)
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD

    class _ErrorClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("reservations service down")

    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lambda *a, **kw: _ErrorClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            f"/runs/{run_id}/commands",
            headers={"Authorization": "Bearer fake-token"},
        )
    assert resp.status_code == 403
