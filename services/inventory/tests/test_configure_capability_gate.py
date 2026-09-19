"""Tests for the driver-capability apply gate (issues #839/#840).

Capability comes from the driver's connection type, not a declared flag: only
a Management driver's contract includes `configure`
(herd_common.device_config.CONFIGURE_CONNECTION_TYPES). These tests cover
both apply entry points (POST .../schedule and POST .../apply), the ordering
against the existing 403 authorization check, the critical distinction that
config VERSION creation is unaffected on every connection type, and today's
no-op behavior when a device has no resolvable driver at all.
"""

import io
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}


def override_user():
    return {"sub": "00000000-0000-0000-0000-000000000002", "username": "viewer", "role": "user"}


_mock_storage: dict[str, bytes] = {}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _mock_storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mock_upload(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def _mock_delete(key: str) -> None:
    _mock_storage.pop(key, None)


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch("app.services.driver_service.upload_object", side_effect=_mock_upload),
        patch("app.services.driver_service.delete_object", side_effect=_mock_delete),
    ):
        yield


@pytest.fixture(autouse=True)
def _noop_reservation_guard():
    with patch(
        "app.routers.device_configs.find_blocking_reservations_for_device",
        new=AsyncMock(return_value=[]),
    ) as m:
        yield m


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_TEMPLATE = {
    "name": "Router",
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


async def _create_driver(client, connection_type: str) -> str:
    global _driver_counter
    _driver_counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"GateDrv{_driver_counter}", "connection_type": connection_type},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert drv.status_code == 201, drv.text
    return drv.json()["id"]


async def _create_device(client, connection_type: str) -> tuple[str, str]:
    """Returns (device_id, driver_id)."""
    driver_id = await _create_driver(client, connection_type)
    tpl = await client.post("/templates", json={**_TEMPLATE, "driver_id": driver_id})
    assert tpl.status_code == 201, tpl.text
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
    return dev.json()["id"], driver_id


def _future(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


L3_CONFIG = {
    "routes": [{"destination": "203.0.113.0/24", "next_hop": "192.0.2.1", "interface": "eth1"}]
}

STRUCTURED_MESSAGE = (
    "This device's driver implements the Layer 3 Switch contract, which has no "
    "configure method, so a config apply cannot run. Config versions on this "
    "device store intent only."
)


# --- The critical distinction: config VERSIONS remain creatable everywhere ---


@pytest.mark.asyncio
async def test_create_config_version_on_layer3_device_still_201(client):
    device_id, _ = await _create_device(client, "Layer 3 Switch")
    resp = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": L3_CONFIG, "description": "route intent"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["connection_type"] == "Layer 3 Switch"


# --- POST .../schedule ---


@pytest.mark.asyncio
async def test_schedule_layer3_driver_409_structured_detail_and_no_job_created(client):
    device_id, driver_id = await _create_device(client, "Layer 3 Switch")
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": L3_CONFIG})
    assert cv.status_code == 201
    vid = cv.json()["id"]

    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": False},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "driver_cannot_configure"
    assert detail["connection_type"] == "Layer 3 Switch"
    assert detail["message"] == STRUCTURED_MESSAGE

    from app.models.device_config_apply_job import DeviceConfigApplyJob  # noqa: PLC0415

    async with TestSessionLocal() as session:
        count = (
            await session.execute(select(func.count()).select_from(DeviceConfigApplyJob))
        ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_schedule_non_manager_gets_403_not_409():
    """A caller with no manage grant on a Layer 3 Switch device gets 403 (the
    authorization check), never the 409, so an unauthorized caller learns
    nothing about the device's driver."""
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, _ = await _create_device(ac, "Layer 3 Switch")
            cv = await ac.post(f"/devices/{device_id}/config-versions", json={"config": L3_CONFIG})
            vid = cv.json()["id"]

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.routers.apply_jobs._user_can_manage_device",
            new=AsyncMock(return_value=False),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{vid}/schedule",
                    json={"scheduled_for": _future(), "dry_run": False},
                )
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_schedule_management_driver_still_succeeds(client):
    """Regression: the existing, common path is unaffected by the new gate."""
    device_id, _ = await _create_device(client, "Management")
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 5}})
    vid = cv.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": False},
    )
    assert resp.status_code == 201, resp.text


# --- POST .../apply ---


@pytest.mark.asyncio
async def test_apply_layer3_driver_409_and_execution_not_called(client):
    device_id, _ = await _create_device(client, "Layer 3 Switch")
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": L3_CONFIG})
    vid = cv.json()["id"]

    calls: list[str] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append(url)
            raise AssertionError("execution must not be called when the driver cannot configure")

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "driver_cannot_configure"
    assert detail["message"] == STRUCTURED_MESSAGE
    assert calls == []


@pytest.mark.asyncio
async def test_apply_non_manager_gets_403_not_409():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, _ = await _create_device(ac, "Layer 3 Switch")
            cv = await ac.post(f"/devices/{device_id}/config-versions", json={"config": L3_CONFIG})
            vid = cv.json()["id"]

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=False),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{vid}/apply",
                    headers={"Authorization": "Bearer t"},
                )
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_apply_management_driver_still_succeeds(client):
    device_id, _ = await _create_device(client, "Management")
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 7}})
    vid = cv.json()["id"]

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "55555555-5555-5555-5555-555555555555", "status": "SUCCESS"}

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return FakeResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"


# --- No driver resolvable: today's (unchanged) behavior, pinned ---


async def _null_out_driver(device_id: str) -> None:
    """Directly null the device's template.driver_id, bypassing the API
    (TemplateCreate requires a driver on a device template), to reach the
    'no driver resolvable' state the gate must leave alone."""
    from app.models.device import Device  # noqa: PLC0415
    from app.models.template import DeviceTemplate  # noqa: PLC0415

    async with TestSessionLocal() as session:
        device = await session.get(Device, uuid.UUID(device_id))
        template = await session.get(DeviceTemplate, device.template_id)
        template.driver_id = None
        await session.commit()


@pytest.mark.asyncio
async def test_schedule_no_driver_resolvable_pins_todays_behavior(client):
    """Today, schedule_apply_job never inspects the driver at all (except the
    dry_run branch, sidestepped here with dry_run=False), so a device with no
    resolvable driver still gets a job created. The new gate must be a no-op
    for this case, not a new refusal."""
    device_id, _ = await _create_device(client, "Management")
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})
    vid = cv.json()["id"]
    await _null_out_driver(device_id)

    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": False},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_apply_no_driver_resolvable_pins_todays_behavior(client):
    """Today, apply_config_version never inspects the driver either; it
    always calls execution unconditionally. The new gate must be a no-op for
    this case too."""
    device_id, _ = await _create_device(client, "Management")
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})
    vid = cv.json()["id"]
    await _null_out_driver(device_id)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "66666666-6666-6666-6666-666666666666", "status": "SUCCESS"}

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return FakeResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"
