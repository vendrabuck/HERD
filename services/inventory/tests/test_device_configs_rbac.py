"""Auth-denial sweep for device-config and apply-job endpoints (roadmap #9).

Two flavors of denial are covered:

1. Anonymous calls (no Authorization header) must be rejected by HTTPBearer
   before reaching handler logic. FastAPI's default HTTPBearer auto_error=True
   surfaces this as 403; the contract enforced here is "401 or 403", which is
   robust to either being raised by the auth layer.
2. The cancel-job ownership rule has an admin override (`_is_admin` in
   `services/inventory/app/routers/apply_jobs.py:33`); the existing
   `test_cancel_other_users_job_forbidden` proves the deny path. This file
   adds the matching allow path: a different admin can cancel another user's
   pending job.
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

OWNER_ID = "00000000-0000-0000-0000-000000000010"
OTHER_ADMIN_ID = "00000000-0000-0000-0000-000000000020"
USER_ID = "00000000-0000-0000-0000-000000000030"


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


def _override_owner_admin():
    return {"sub": OWNER_ID, "username": "owner", "role": "admin"}


def _override_other_admin():
    return {"sub": OTHER_ADMIN_ID, "username": "admin2", "role": "admin"}


def _override_plain_user():
    return {"sub": USER_ID, "username": "u30", "role": "user"}


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
async def anon_client():
    """Client with no auth override; HTTPBearer rejects every protected route."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# Placeholder UUIDs are fine for anon: HTTPBearer raises before handler logic.
_DEV = str(uuid.uuid4())
_VER = str(uuid.uuid4())
_JOB = str(uuid.uuid4())

ANON_ENDPOINTS: list[tuple[str, str, dict | None]] = [
    # device_configs.py
    ("GET", f"/devices/{_DEV}/config-versions", None),
    ("GET", f"/devices/{_DEV}/config-versions/diff?from={_VER}&to={_VER}", None),
    ("GET", f"/devices/{_DEV}/config-versions/{_VER}", None),
    ("POST", f"/devices/{_DEV}/config-versions", {"config": {}}),
    (
        "POST",
        f"/devices/{_DEV}/config-versions/{_VER}/restore",
        {"description": "x"},
    ),
    ("POST", f"/devices/{_DEV}/config-versions/{_VER}/apply", None),
    # apply_jobs.py
    (
        "POST",
        f"/devices/{_DEV}/config-versions/{_VER}/schedule",
        {"scheduled_for": "2030-01-01T00:00:00+00:00"},
    ),
    ("GET", f"/devices/{_DEV}/apply-jobs", None),
    ("DELETE", f"/apply-jobs/{_JOB}", None),
]


@pytest.mark.parametrize("method,path,body", ANON_ENDPOINTS)
@pytest.mark.asyncio
async def test_anonymous_denied(anon_client, method, path, body):
    """Every device-config endpoint rejects an unauthenticated caller."""
    if body is None:
        resp = await anon_client.request(method, path)
    else:
        resp = await anon_client.request(method, path, json=body)
    assert resp.status_code in (401, 403), (method, path, resp.status_code, resp.text)


_TEMPLATE_BODY = {
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


async def _seed_pending_job_owned_by_owner(client) -> str:
    """Drive the API as owner-admin to produce a pending job; return job_id."""
    drv = await client.post(
        "/drivers",
        data={"name": f"Drv-{uuid.uuid4().hex[:6]}", "connection_type": "Management"},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert drv.status_code == 201, drv.text
    driver_id = drv.json()["id"]

    tmpl = await client.post("/templates", json={**_TEMPLATE_BODY, "driver_id": driver_id})
    assert tmpl.status_code == 201, tmpl.text
    template_id = tmpl.json()["id"]

    dev = await client.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert dev.status_code == 201, dev.text
    device_id = dev.json()["id"]

    ver = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 10}, "description": "v1"},
    )
    assert ver.status_code == 201, ver.text
    version_id = ver.json()["id"]

    sched = await client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
    )
    assert sched.status_code == 201, sched.text
    return sched.json()["id"]


@pytest.mark.asyncio
async def test_admin_can_cancel_other_users_pending_job():
    """`_is_admin` carve-out lets a different admin cancel someone else's job (204)."""
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_owner_admin
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            job_id = await _seed_pending_job_owned_by_owner(ac)

        # Switch identity: a different admin attempts the cancel.
        app.dependency_overrides[get_current_user_payload] = _override_other_admin

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.delete(f"/apply-jobs/{job_id}")
        assert resp.status_code == 204, resp.text

        # Confirm the job flipped to cancelled, not just a no-op 204.
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # The list endpoint surfaces status; we need device_id to query it.
            # Re-derive via the job row through a second cancel attempt that 409s.
            second = await ac.delete(f"/apply-jobs/{job_id}")
        assert second.status_code == 409, second.text
        assert "cancelled" in second.json().get("detail", "").lower()
    finally:
        app.dependency_overrides.clear()


async def _seed_device_as_owner(ac) -> tuple[str, str]:
    """Create driver+template+device+version as owner-admin; return (device_id, version_id)."""
    drv = await ac.post(
        "/drivers",
        data={"name": f"Drv-{uuid.uuid4().hex[:6]}", "connection_type": "Management"},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert drv.status_code == 201, drv.text
    tmpl = await ac.post("/templates", json={**_TEMPLATE_BODY, "driver_id": drv.json()["id"]})
    assert tmpl.status_code == 201, tmpl.text
    dev = await ac.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": tmpl.json()["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert dev.status_code == 201, dev.text
    device_id = dev.json()["id"]
    ver = await ac.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 10}},
    )
    assert ver.status_code == 201, ver.text
    return device_id, ver.json()["id"]


@pytest.mark.asyncio
async def test_non_admin_schedule_denied_without_acl_grant():
    """Non-admin scheduling without `manage` grant on the device must 403."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        # Owner-admin seeds the device + version.
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, version_id = await _seed_device_as_owner(ac)

        # Switch to plain user; ACL helper denies.
        app.dependency_overrides[get_current_user_payload] = _override_plain_user
        with patch(
            "app.routers.apply_jobs._user_can_manage_device",
            new=AsyncMock(return_value=False),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{version_id}/schedule",
                    json={
                        "scheduled_for": (
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat()
                    },
                )
        assert resp.status_code == 403, resp.text
        assert "manage" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_non_admin_schedule_succeeds_with_acl_grant():
    """Non-admin scheduling with a `manage` grant succeeds (201)."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, version_id = await _seed_device_as_owner(ac)

        app.dependency_overrides[get_current_user_payload] = _override_plain_user
        with patch(
            "app.routers.apply_jobs._user_can_manage_device",
            new=AsyncMock(return_value=True),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{version_id}/schedule",
                    json={
                        "scheduled_for": (
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat()
                    },
                )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "pending"
        assert resp.json()["device_id"] == device_id
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_non_admin_create_version_denied_without_acl_grant():
    """A non-admin without `manage` cannot stage a new config version (403)."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, _ = await _seed_device_as_owner(ac)

        app.dependency_overrides[get_current_user_payload] = _override_plain_user
        with patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=False),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions",
                    json={"config": {"vlan": 7}},
                )
        assert resp.status_code == 403, resp.text
        assert "manage" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_non_admin_create_version_succeeds_with_acl_grant():
    """With `manage`, a non-admin can stage a new config version (201)."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, _ = await _seed_device_as_owner(ac)

        app.dependency_overrides[get_current_user_payload] = _override_plain_user
        with patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=True),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions",
                    json={"config": {"vlan": 7}},
                )
        assert resp.status_code == 201, resp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_non_admin_restore_denied_without_acl_grant():
    """A non-admin without `manage` cannot restore a prior version (403)."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, version_id = await _seed_device_as_owner(ac)

        app.dependency_overrides[get_current_user_payload] = _override_plain_user
        with patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=False),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{version_id}/restore",
                    json={},
                )
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_non_admin_apply_denied_without_acl_grant():
    """A non-admin without `manage` cannot apply a config version (403).

    Mirrors test_non_admin_schedule_denied_without_acl_grant: the apply path
    must enforce the same manage-or-active-reservation gate as its siblings, so
    an arbitrary authed user cannot push a config to any device.
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, version_id = await _seed_device_as_owner(ac)

        app.dependency_overrides[get_current_user_payload] = _override_plain_user
        with patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=False),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{version_id}/apply",
                )
        assert resp.status_code == 403, resp.text
        assert "manage" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_apply_with_malformed_run_id_returns_200_and_persists_pointer():
    """A non-UUID run_id from execution must not break the commit (fixes #2/#3).

    The execution service is mocked to return a successful run whose `id` is not
    a valid UUID. The endpoint must still return 200 with run_id=None, and the
    device's current-config pointer must be persisted (the bad run_id used to
    raise ValueError and skip the commit, then 500 on the response build).
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = _override_owner_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, version_id = await _seed_device_as_owner(ac)

        class _FakeResp:
            status_code = 200

            @staticmethod
            def json():
                return {"id": "not-a-uuid", "status": "SUCCESS"}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _FakeResp()

        with patch("app.routers.device_configs.httpx.AsyncClient", _FakeClient):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    f"/devices/{device_id}/config-versions/{version_id}/apply",
                )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["run_id"] is None
        assert payload["status"] == "success"

        # The success pointer must have been persisted despite the bad run_id.
        from app.models.device import Device

        async with TestSessionLocal() as session:
            device = await session.get(Device, uuid.UUID(device_id))
            assert device is not None
            assert str(device.current_config_version_id) == version_id
    finally:
        app.dependency_overrides.clear()
