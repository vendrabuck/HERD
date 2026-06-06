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
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
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
