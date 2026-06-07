"""Edge-case and error-branch coverage for the inventory routers.

Targets branches the happy-path suites skip: 404s on unknown devices/jobs,
naive-timestamp normalization on schedule, malformed-token visibility denials,
the internal resolve-by-name empty-names short-circuit, and the apply path's
httpx transport-error / non-JSON-body handling.

Harness mirrors the established per-file pattern (own in-memory engine, app
dependency overrides, mocked MinIO).
"""

import io
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ADMIN = {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}
# A user payload whose `sub` is not a valid UUID, to drive the fail-closed
# token-subject branches.
BAD_SUB_USER = {"sub": "not-a-uuid", "username": "viewer", "role": "user"}


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


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch(
            "app.services.driver_service.upload_object",
            side_effect=lambda key, data, **_: _storage.__setitem__(key, data),
        ),
        patch(
            "app.services.driver_service.delete_object",
            side_effect=lambda key: _storage.pop(key, None),
        ),
    ):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_TEMPLATE = {
    "name": "Firewall",
    "icon": "data:image/png;base64,iVBOR",
    "vendor": "V",
    "model": "M",
    "sections": [
        {"name": "General", "fields": [{"key": "model", "label": "Model", "type": "string"}]}
    ],
}

_counter = 0


async def _seed_device(client, connection_type: str = "Management") -> tuple[str, str]:
    """Create driver + template + device + one config version. Returns (device_id, version_id)."""
    global _counter
    _counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"EdgeDrv{_counter}", "connection_type": connection_type},
        files={"file": (f"d{_counter}.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert drv.status_code == 201, drv.text
    tpl = await client.post("/templates", json={**_TEMPLATE, "driver_id": drv.json()["id"]})
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
    device_id = dev.json()["id"]
    cv = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 100}})
    assert cv.status_code == 201, cv.text
    return device_id, cv.json()["id"]


# --- apply_jobs: 404s and naive-timestamp normalization ---------------------


@pytest.mark.asyncio
async def test_schedule_unknown_device_404(client):
    """Schedule against a device that does not exist hits the device 404 branch."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    resp = await client.post(
        f"/devices/{uuid.uuid4()}/config-versions/{uuid.uuid4()}/schedule",
        json={"scheduled_for": future},
    )
    assert resp.status_code == 404
    assert "Device not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_schedule_accepts_naive_future_timestamp(client):
    """A naive (tz-less) future timestamp is normalized to UTC, not rejected."""
    device_id, version_id = await _seed_device(client)
    naive_future = (
        (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    )
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": naive_future},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_list_apply_jobs_unknown_device_404(client):
    resp = await client.get(f"/devices/{uuid.uuid4()}/apply-jobs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_apply_job_not_found_404(client):
    resp = await client.get(f"/apply-jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert "Apply job not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_cancel_unknown_job_404(client):
    resp = await client.delete(f"/apply-jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_apply_job_returns_job(client):
    device_id, version_id = await _seed_device(client)
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": future},
    )
    job_id = sched.json()["id"]
    resp = await client.get(f"/apply-jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id


# --- device_configs: connection-type and apply transport errors -------------


@pytest.mark.asyncio
async def test_create_config_version_without_driver_connection_type_422(client):
    """A device whose template has no driver cannot resolve a connection_type.

    The router's `_connection_type_for` returns 422 when the template has no
    driver (or the driver has no connection_type). `connection_type` is NOT
    NULL on the driver row, so we exercise the no-driver branch by detaching
    the driver from the template directly in the DB.
    """
    device_id, _ = await _seed_device(client)
    from app.models.device import Device
    from app.models.template import DeviceTemplate

    async with TestSessionLocal() as db:
        device = await db.get(Device, uuid.UUID(device_id))
        template = await db.get(DeviceTemplate, device.template_id)
        template.driver_id = None
        await db.commit()

    resp = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})
    assert resp.status_code == 422
    assert "connection_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_apply_handles_execution_transport_error(client):
    """An httpx transport error during apply yields a failed result, not a 500."""
    device_id, version_id = await _seed_device(client)

    class RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            raise httpx.ConnectError("execution unreachable")

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: RaisingClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "unreachable" in body["error"]


@pytest.mark.asyncio
async def test_apply_handles_non_json_error_body(client):
    """A >=400 response whose body is not JSON falls back to .text for the error."""
    device_id, version_id = await _seed_device(client)

    class TextOnlyResponse:
        status_code = 502

        def json(self):
            raise ValueError("not json")

        @property
        def text(self):
            return "bad gateway"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return TextOnlyResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "502" in body["error"]
    assert "bad gateway" in body["error"]


@pytest.mark.asyncio
async def test_apply_handles_non_json_success_body(client):
    """A 2xx response whose body is not JSON degrades to an empty payload."""
    device_id, version_id = await _seed_device(client)

    class TextOnlyResponse:
        status_code = 200

        def json(self):
            raise ValueError("not json")

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return TextOnlyResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{version_id}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    # No run id in body -> status defaults to "success", pointer not moved.
    assert resp.json()["status"] == "success"


# --- devices: malformed-subject visibility denials + resolve short-circuit ---


@pytest.mark.asyncio
async def test_list_devices_malformed_token_subject_401():
    """A non-admin token with an invalid `sub` fails closed with 401."""
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: BAD_SUB_USER
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/devices", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 401
        assert "invalid token subject" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_device_malformed_token_subject_404(client):
    """get_device_by_id with an invalid non-admin `sub` returns 404 (no leak)."""
    device_id, _ = await _seed_device(client)
    app.dependency_overrides[get_current_user_payload] = lambda: BAD_SUB_USER
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/devices/{device_id}", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_admin_list_runs_real_visibility_resolution(client):
    """Drive the real `_resolve_visible_device_ids` (only the auth-service fetch
    is mocked). A user in no groups sees no DUTs; the device list comes back
    empty rather than leaking every device."""
    from unittest.mock import AsyncMock

    await _seed_device(client)
    user_payload = {
        "sub": "00000000-0000-0000-0000-000000000099",
        "username": "viewer",
        "role": "user",
    }
    app.dependency_overrides[get_current_user_payload] = lambda: user_payload
    with patch(
        "app.routers.device_groups._fetch_user_group_ids",
        new=AsyncMock(return_value=[]),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/devices", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# --- _user_can_manage_device wrappers forward to the shared helper ----------


@pytest.mark.asyncio
async def test_device_configs_manage_wrapper_forwards_to_helper():
    from unittest.mock import AsyncMock

    from app.routers import device_configs

    with patch.object(
        device_configs,
        "user_has_manage_or_owns_active_reservation",
        new=AsyncMock(return_value=True),
    ) as mock_helper:
        ok = await device_configs._user_can_manage_device("u", uuid.uuid4(), "Bearer t")
    assert ok is True
    assert mock_helper.await_count == 1


@pytest.mark.asyncio
async def test_apply_jobs_manage_wrapper_forwards_to_helper():
    from unittest.mock import AsyncMock

    from app.routers import apply_jobs

    with patch.object(
        apply_jobs,
        "user_has_manage_or_owns_active_reservation",
        new=AsyncMock(return_value=False),
    ) as mock_helper:
        ok = await apply_jobs._user_can_manage_device("u", uuid.uuid4(), None)
    assert ok is False
    assert mock_helper.await_count == 1


@pytest.mark.asyncio
async def test_resolve_by_name_empty_names_returns_empty(client):
    """resolve-by-name with only blank names short-circuits to an empty map."""
    resp = await client.post(
        "/devices/resolve-by-name",
        json={"names": ["", ""]},
        headers={"X-Internal-Token": "test-token"},
    )
    # Empty-string names are filtered out, leaving an empty set that
    # short-circuits before any DB query.
    assert resp.status_code == 200
    assert resp.json()["resolved"] == {}
