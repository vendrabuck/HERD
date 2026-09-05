"""Iter-3 ACL widening: reservation owners can schedule applies without an
explicit `manage` grant on the device.

The herd-common helper makes two HTTP calls (ACL service then reservations
service). These tests patch the helper directly so we exercise the
inventory route's behavior in isolation. The helper itself is unit-tested
in services/common/tests/test_acl.py.
"""

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device_config_apply_job import DeviceConfigApplyJob
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ADMIN_PAYLOAD = {
    "sub": "00000000-0000-0000-0000-000000000001",
    "username": "admin",
    "role": "admin",
}
USER_PAYLOAD = {
    "sub": "00000000-0000-0000-0000-000000000002",
    "username": "viewer",
    "role": "user",
}


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


_storage: dict[str, bytes] = {}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mock_upload(key: str, data: bytes, **_: object) -> None:
    _storage[key] = data


def _mock_delete(key: str) -> None:
    _storage.pop(key, None)


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch("app.services.driver_service.upload_object", side_effect=_mock_upload),
        patch("app.services.driver_service.delete_object", side_effect=_mock_delete),
    ):
        yield


_TEMPLATE = {
    "name": "Firewall",
    "icon": "data:image/png;base64,iVBOR",
    "vendor": "V",
    "model": "M",
    "sections": [
        {
            "name": "General",
            "fields": [{"key": "model", "label": "Model", "type": "string"}],
        }
    ],
}


_driver_counter = 0


def _build_zip(supports_dry_run: bool = True) -> bytes:
    """Driver zip with metadata so dry-run is unblocked when needed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", "class Driver:\n    pass\n")
        zf.writestr(
            "driver_metadata.json",
            json.dumps({"supports_dry_run": supports_dry_run, "version": "1.0"}),
        )
    return buf.getvalue()


async def _seed_device(client) -> tuple[str, str]:
    """Returns (device_id, version_id). Admin client only; seeding requires admin."""
    global _driver_counter
    _driver_counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"OwnDrv{_driver_counter}", "connection_type": "Management"},
        files={"file": (f"d{_driver_counter}.zip", io.BytesIO(_build_zip()), "application/zip")},
    )
    assert drv.status_code == 201, drv.text
    tpl = await client.post("/templates", json={**_TEMPLATE, "driver_id": drv.json()["id"]})
    assert tpl.status_code == 201
    dev = await client.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": tpl.json()["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert dev.status_code == 201, dev.text
    device_id = dev.json()["id"]
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    assert cv.status_code == 201
    return device_id, cv.json()["id"]


def _future_iso(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Reservation-owner free pass on schedule ---


@pytest.mark.asyncio
async def test_reservation_owner_can_schedule_without_explicit_grant(admin_client):
    """Iter-3 widening: a non-admin with no `manage` ACL but who owns an
    active reservation containing the device can schedule an apply."""
    device_id, version_id = await _seed_device(admin_client)

    # Switch to plain user; herd-common helper says "True" (reservation owner pass).
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    with patch(
        "app.routers.apply_jobs._user_can_manage_device",
        new=AsyncMock(return_value=True),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                f"/devices/{device_id}/config-versions/{version_id}/schedule",
                json={"scheduled_for": _future_iso()},
            )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_non_owner_without_grant_still_rejected(admin_client):
    """The widening is not a blanket free pass: helper saying False -> 403."""
    device_id, version_id = await _seed_device(admin_client)

    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    with patch(
        "app.routers.apply_jobs._user_can_manage_device",
        new=AsyncMock(return_value=False),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                f"/devices/{device_id}/config-versions/{version_id}/schedule",
                json={"scheduled_for": _future_iso()},
            )
    assert resp.status_code == 403
    assert "manage" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reservation_owner_can_create_config_version(admin_client):
    """Same widening on create_config_version, used by propose_config_change."""
    device_id, _ = await _seed_device(admin_client)

    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    with patch(
        "app.routers.device_configs._user_can_manage_device",
        new=AsyncMock(return_value=True),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                f"/devices/{device_id}/config-versions",
                json={"config": {"vlan": 200}, "description": "AI proposal"},
            )
    assert resp.status_code == 201, resp.text


# --- reservation_id schedule-time validation (issue #704) -------------------


class _FakeResp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _FakeReservationsAsyncClient:
    """Stand-in for httpx.AsyncClient, routing GETs by a substring match on URL."""

    def __init__(self, get_responses: dict[str, _FakeResp]):
        self._get = get_responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers=None, params=None, timeout=None):
        for key, resp in self._get.items():
            if key in url:
                return resp
        return _FakeResp(404, {})


async def _apply_job_row_count() -> int:
    async with TestSessionLocal() as session:
        return (
            await session.execute(select(func.count()).select_from(DeviceConfigApplyJob))
        ).scalar() or 0


@pytest.mark.asyncio
async def test_foreign_reservation_id_returns_422_and_writes_no_row(admin_client, monkeypatch):
    """Spec #704 test (5): a reservation_id the caller does not own (or that
    does not exist) returns 422 and the job row is never written."""
    device_id, version_id = await _seed_device(admin_client)
    foreign_reservation_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.routers.apply_jobs.settings.internal_api_token", "token", raising=False
    )
    fake_client = _FakeReservationsAsyncClient(
        get_responses={str(foreign_reservation_id): _FakeResp(404, {})}
    )
    monkeypatch.setattr(
        "app.routers.apply_jobs.httpx.AsyncClient", lambda *a, **kw: fake_client
    )

    assert await _apply_job_row_count() == 0
    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={
            "scheduled_for": _future_iso(),
            "reservation_id": str(foreign_reservation_id),
        },
    )
    assert resp.status_code == 422
    assert "reservation_id" in resp.json()["detail"]
    assert await _apply_job_row_count() == 0


@pytest.mark.asyncio
async def test_reservation_id_inactive_returns_422_and_writes_no_row(admin_client, monkeypatch):
    """The reservation exists but is not currently active -> 422, no row."""
    device_id, version_id = await _seed_device(admin_client)
    reservation_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.routers.apply_jobs.settings.internal_api_token", "token", raising=False
    )
    fake_client = _FakeReservationsAsyncClient(
        get_responses={
            str(reservation_id): _FakeResp(200, {"id": str(reservation_id), "is_active": False}),
        }
    )
    monkeypatch.setattr(
        "app.routers.apply_jobs.httpx.AsyncClient", lambda *a, **kw: fake_client
    )

    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": _future_iso(), "reservation_id": str(reservation_id)},
    )
    assert resp.status_code == 422
    assert await _apply_job_row_count() == 0


@pytest.mark.asyncio
async def test_reservation_id_active_but_not_owned_by_caller_returns_422(
    admin_client, monkeypatch
):
    """The reservation is active, but the caller does not own an active
    reservation containing this device -> 422, no row."""
    device_id, version_id = await _seed_device(admin_client)
    reservation_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.routers.apply_jobs.settings.internal_api_token", "token", raising=False
    )
    fake_client = _FakeReservationsAsyncClient(
        get_responses={
            str(reservation_id): _FakeResp(200, {"id": str(reservation_id), "is_active": True}),
            "/internal/active": _FakeResp(200, {"owns_active": False}),
        }
    )
    monkeypatch.setattr(
        "app.routers.apply_jobs.httpx.AsyncClient", lambda *a, **kw: fake_client
    )

    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": _future_iso(), "reservation_id": str(reservation_id)},
    )
    assert resp.status_code == 422
    assert await _apply_job_row_count() == 0


@pytest.mark.asyncio
async def test_reservation_id_valid_and_owned_schedules_successfully(admin_client, monkeypatch):
    """Positive control: an active reservation the caller owns, containing
    the device, is accepted."""
    device_id, version_id = await _seed_device(admin_client)
    reservation_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.routers.apply_jobs.settings.internal_api_token", "token", raising=False
    )
    fake_client = _FakeReservationsAsyncClient(
        get_responses={
            str(reservation_id): _FakeResp(200, {"id": str(reservation_id), "is_active": True}),
            "/internal/active": _FakeResp(200, {"owns_active": True}),
        }
    )
    monkeypatch.setattr(
        "app.routers.apply_jobs.httpx.AsyncClient", lambda *a, **kw: fake_client
    )

    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": _future_iso(), "reservation_id": str(reservation_id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["reservation_id"] == str(reservation_id)


@pytest.mark.asyncio
async def test_reservation_id_validation_fails_closed_when_unreachable(admin_client, monkeypatch):
    """Reservations unreachable -> 503, fail closed, no row written."""
    device_id, version_id = await _seed_device(admin_client)
    reservation_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.routers.apply_jobs.settings.internal_api_token", "token", raising=False
    )

    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None, params=None, timeout=None):
            import httpx

            raise httpx.ConnectError("reservations down")

    monkeypatch.setattr(
        "app.routers.apply_jobs.httpx.AsyncClient", lambda *a, **kw: _RaisingClient()
    )

    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": _future_iso(), "reservation_id": str(reservation_id)},
    )
    assert resp.status_code == 503
    assert await _apply_job_row_count() == 0


# --- scheduled_for horizon bound (issue #704) --------------------------------


@pytest.mark.asyncio
async def test_scheduled_for_beyond_horizon_returns_422(admin_client):
    """Spec #704 test (6): horizon plus one second is rejected."""
    device_id, version_id = await _seed_device(admin_client)
    max_days = settings.apply_job_max_horizon_days
    too_far = (datetime.now(timezone.utc) + timedelta(days=max_days, seconds=1)).isoformat()

    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": too_far},
    )
    assert resp.status_code == 422
    assert str(max_days) in resp.json()["detail"]


@pytest.mark.asyncio
async def test_scheduled_for_just_within_horizon_returns_201(admin_client):
    """Spec #704 test (6): horizon minus one second is accepted."""
    device_id, version_id = await _seed_device(admin_client)
    max_days = settings.apply_job_max_horizon_days
    just_within = (datetime.now(timezone.utc) + timedelta(days=max_days, seconds=-1)).isoformat()

    resp = await admin_client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": just_within},
    )
    assert resp.status_code == 201, resp.text
