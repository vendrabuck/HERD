"""Issue #337: config-version restore must apply the same active-reservation
guard topology restore has (services/cabling/app/routes/versions.py), so a
config rollback cannot pull the rug out from under someone else's active
reservation. Two layers are covered here:

1. app.services.reservation_guard.find_blocking_reservations_for_device --
   the cross-service HTTP call to reservations' /internal/by-device endpoint,
   including the upstream-unreachable-is-503 standardization (issue #131),
   which deliberately differs from cabling's fail-open equivalent.
2. The restore router wiring: a blocking reservation held by someone else
   409s with the topology-restore wording shape; one held by the caller
   themselves does not block (the existing ACL widening already treats
   reservation ownership as equivalent to `manage` for this action).
"""

import io
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.services.reservation_guard import find_blocking_reservations_for_device
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ADMIN_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000099"

# Where the by-device reservation lookup is made; patched per test to drive
# the reachable / transport-error / non-200 branches directly.
_RESERVATIONS_GET = "app.services.reservation_guard.httpx.AsyncClient.get"


def override_admin():
    return {"sub": ADMIN_ID, "username": "admin", "role": "admin"}


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


_mock_storage: dict[str, bytes] = {}


def _mock_upload(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def _mock_delete(key: str) -> None:
    _mock_storage.pop(key, None)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _mock_storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch("app.services.driver_service.upload_object", side_effect=_mock_upload),
        patch("app.services.driver_service.delete_object", side_effect=_mock_delete),
    ):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


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


async def _create_device(client) -> str:
    global _driver_counter
    _driver_counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"Drv{_driver_counter}", "connection_type": "Management"},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert drv.status_code == 201
    driver_id = drv.json()["id"]

    tmpl = await client.post("/templates", json={**_TEMPLATE, "driver_id": driver_id})
    assert tmpl.status_code == 201
    template_id = tmpl.json()["id"]

    resp = await client.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_version(client, device_id: str, vlan: int = 100) -> str:
    resp = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": vlan}},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# --- Router wiring: blocking vs. self-owned vs. none ------------------------


@pytest.mark.asyncio
async def test_restore_blocked_by_active_reservation_of_another_user(client):
    device_id = await _create_device(client)
    version_id = await _create_version(client, device_id)

    with patch(
        "app.routers.device_configs.find_blocking_reservations_for_device",
        new=AsyncMock(
            return_value=[
                {
                    "id": "r1",
                    "user_id": OTHER_USER_ID,
                    "status": "ACTIVE",
                    "end_time": "2030-01-01T00:00:00Z",
                }
            ]
        ),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/restore",
            json={},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["message"] == "Device has active reservations; restore blocked"
    assert detail["reservations"][0]["id"] == "r1"
    assert detail["reservations"][0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_restore_allowed_when_caller_owns_the_blocking_reservation(client):
    """A reservation owner restoring their own device's config is the
    self-service case the ACL widening already permits; the guard must not
    re-block it."""
    device_id = await _create_device(client)
    version_id = await _create_version(client, device_id)

    with patch(
        "app.routers.device_configs.find_blocking_reservations_for_device",
        new=AsyncMock(
            return_value=[
                {
                    "id": "r-mine",
                    "user_id": ADMIN_ID,
                    "status": "ACTIVE",
                    "end_time": "2030-01-01T00:00:00Z",
                }
            ]
        ),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/restore",
            json={},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_restore_proceeds_and_calls_the_guard_when_no_blocking_reservations(client):
    device_id = await _create_device(client)
    version_id = await _create_version(client, device_id)

    with patch(
        "app.routers.device_configs.find_blocking_reservations_for_device",
        new=AsyncMock(return_value=[]),
    ) as mock_guard:
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/restore",
            json={},
        )
    assert resp.status_code == 201
    assert mock_guard.await_count == 1
    assert mock_guard.await_args.args[0] == uuid.UUID(device_id)


@pytest.mark.asyncio
async def test_restore_blocked_mixed_owned_and_unowned_reservations_lists_only_others(client):
    """When both the caller's own reservation and someone else's are active on
    the device, the 409 must still fire (someone else is exposed) and must
    list only the blocking-for-them reservation, not the caller's own."""
    device_id = await _create_device(client)
    version_id = await _create_version(client, device_id)

    with patch(
        "app.routers.device_configs.find_blocking_reservations_for_device",
        new=AsyncMock(
            return_value=[
                {
                    "id": "r-mine",
                    "user_id": ADMIN_ID,
                    "status": "ACTIVE",
                    "end_time": "2030-01-01T00:00:00Z",
                },
                {
                    "id": "r-theirs",
                    "user_id": OTHER_USER_ID,
                    "status": "PENDING_PROVISION",
                    "end_time": "2030-02-01T00:00:00Z",
                },
            ]
        ),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/restore",
            json={},
        )
    assert resp.status_code == 409
    ids = [r["id"] for r in resp.json()["detail"]["reservations"]]
    assert ids == ["r-theirs"]


@pytest.mark.asyncio
async def test_restore_blocked_by_reservation_upstream_unreachable_503(client):
    """A transport error talking to reservations must not silently let the
    restore through; it standardizes to 503 (issue #131), unlike the
    fail-open topology guard."""
    device_id = await _create_device(client)
    version_id = await _create_version(client, device_id)

    with patch(_RESERVATIONS_GET, new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/restore",
            json={},
        )
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]


# --- find_blocking_reservations_for_device (guard module, direct) -----------


@pytest.mark.asyncio
async def test_find_blocking_reservations_filters_non_blocking_statuses():
    device_id = uuid.uuid4()
    payload = [
        {"id": "r1", "user_id": OTHER_USER_ID, "status": "ACTIVE", "end_time": "t"},
        {"id": "r2", "user_id": OTHER_USER_ID, "status": "PENDING", "end_time": "t"},
        {"id": "r3", "user_id": OTHER_USER_ID, "status": "PENDING_PROVISION", "end_time": "t"},
        {"id": "r4", "user_id": OTHER_USER_ID, "status": "COMPLETED", "end_time": "t"},
        {"id": "r5", "user_id": OTHER_USER_ID, "status": "CANCELLED", "end_time": "t"},
        {"id": "r6", "user_id": OTHER_USER_ID, "status": "FAILED", "end_time": "t"},
    ]

    class _FakeResponse:
        status_code = 200

        def json(self):
            return payload

    with patch(_RESERVATIONS_GET, new=AsyncMock(return_value=_FakeResponse())):
        result = await find_blocking_reservations_for_device(device_id)

    assert {r["id"] for r in result} == {"r1", "r2", "r3"}


@pytest.mark.asyncio
async def test_find_blocking_reservations_transport_error_raises_503():
    with patch(_RESERVATIONS_GET, new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        with pytest.raises(HTTPException) as excinfo:
            await find_blocking_reservations_for_device(uuid.uuid4())
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_find_blocking_reservations_non_200_raises_503():
    class _FakeResponse:
        status_code = 500

        def json(self):
            return []

    with patch(_RESERVATIONS_GET, new=AsyncMock(return_value=_FakeResponse())):
        with pytest.raises(HTTPException) as excinfo:
            await find_blocking_reservations_for_device(uuid.uuid4())
    assert excinfo.value.status_code == 503
