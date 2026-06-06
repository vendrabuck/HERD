"""Tests for /devices/{id}/config-versions router (roadmap item #9 iter 1)."""

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


async def _create_driver(client, connection_type: str = "Management") -> str:
    global _driver_counter
    _driver_counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"Drv{_driver_counter}", "connection_type": connection_type},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert drv.status_code == 201
    return drv.json()["id"]


async def _create_template(client, connection_type: str = "Management") -> str:
    driver_id = await _create_driver(client, connection_type=connection_type)
    resp = await client.post("/templates", json={**_TEMPLATE, "driver_id": driver_id})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_device(client, connection_type: str = "Management") -> str:
    tid = await _create_template(client, connection_type=connection_type)
    resp = await client.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_config_version_happy_path(client):
    device_id = await _create_device(client)
    resp = await client.post(
        f"/devices/{device_id}/config-versions",
        json={
            "config": {"vlan": 100, "ip": "10.0.0.1", "hostname": "fw-1"},
            "description": "first",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["version_number"] == 1
    assert data["config"]["vlan"] == 100
    assert data["connection_type"] == "Management"
    assert data["author_name"] == "admin"
    assert data["description"] == "first"


@pytest.mark.asyncio
async def test_create_config_version_validates(client):
    device_id = await _create_device(client)
    resp = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"admin_password": "sneaky"}},
    )
    assert resp.status_code == 422
    assert "admin_password" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_config_version_unsupported_connection_type(client):
    device_id = await _create_device(client, connection_type="Layer 1 Switch")
    resp = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 10}},
    )
    assert resp.status_code == 422
    assert "Layer 1 Switch" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_versions_list_paginated(client):
    device_id = await _create_device(client)
    for i in range(3):
        await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100 + i}},
        )
    resp = await client.get(f"/devices/{device_id}/config-versions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    # newest first
    assert [item["version_number"] for item in data["items"]] == [3, 2, 1]
    # list payload omits config blob
    assert "config" not in data["items"][0]


@pytest.mark.asyncio
async def test_get_config_version_detail(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 222, "hostname": "host"}},
    )
    vid = create.json()["id"]
    resp = await client.get(f"/devices/{device_id}/config-versions/{vid}")
    assert resp.status_code == 200
    assert resp.json()["config"] == {"vlan": 222, "hostname": "host"}


@pytest.mark.asyncio
async def test_get_config_version_not_found(client):
    device_id = await _create_device(client)
    resp = await client.get(f"/devices/{device_id}/config-versions/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_diff_config_versions(client):
    device_id = await _create_device(client)
    a = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 100}})
    b = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 200}})
    aid, bid = a.json()["id"], b.json()["id"]
    resp = await client.get(
        f"/devices/{device_id}/config-versions/diff",
        params={"from": aid, "to": bid},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "vlan" in body["diff"]
    assert "100" in body["diff"]
    assert "200" in body["diff"]
    assert body["version_a"] == aid
    assert body["version_b"] == bid


@pytest.mark.asyncio
async def test_restore_creates_new_version(client):
    device_id = await _create_device(client)
    a = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 100}})
    await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 200}})
    aid = a.json()["id"]

    restore = await client.post(
        f"/devices/{device_id}/config-versions/{aid}/restore",
        json={},
    )
    assert restore.status_code == 201
    rdata = restore.json()
    assert rdata["version_number"] == 3
    assert rdata["config"] == {"vlan": 100}
    assert rdata["restored_from_id"] == aid

    versions = await client.get(f"/devices/{device_id}/config-versions")
    assert versions.json()["items"][0]["version_number"] == 3


@pytest.mark.asyncio
async def test_restore_not_found(client):
    device_id = await _create_device(client)
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{uuid.uuid4()}/restore",
        json={},
    )
    assert resp.status_code == 404


# ---- current_config_version_id pointer semantics ----
#
# The pointer means "what version is currently applied on the device". The
# previous behavior flipped it on every save, which broke that meaning: a
# draft you never applied still showed as "current". These tests fix that
# behavior contract.


async def _read_device_current_pointer(device_id: str) -> str | None:
    """Read device.current_config_version_id directly from the test DB."""
    from app.models.device import Device  # noqa: PLC0415

    async with TestSessionLocal() as session:
        device = await session.get(Device, uuid.UUID(device_id))
        assert device is not None
        return str(device.current_config_version_id) if device.current_config_version_id else None


@pytest.mark.asyncio
async def test_create_version_does_not_flip_current_pointer(client):
    """Saving a new config version is a draft; the device's current pointer must not move."""
    device_id = await _create_device(client)
    await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    assert await _read_device_current_pointer(device_id) is None


@pytest.mark.asyncio
async def test_restore_does_not_flip_current_pointer(client):
    """Restoring a prior version also creates a draft; pointer must not move."""
    device_id = await _create_device(client)
    first = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})
    await client.post(f"/devices/{device_id}/config-versions/{first.json()['id']}/restore", json={})
    assert await _read_device_current_pointer(device_id) is None


@pytest.mark.asyncio
async def test_apply_success_flips_current_pointer(client):
    """On a successful apply, the pointer moves to the applied version."""
    device_id = await _create_device(client)
    v = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 100}})
    vid = v.json()["id"]
    assert await _read_device_current_pointer(device_id) is None

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "33333333-3333-3333-3333-333333333333", "status": "SUCCESS"}

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return FakeResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert await _read_device_current_pointer(device_id) == vid


@pytest.mark.asyncio
async def test_apply_failure_does_not_flip_current_pointer(client):
    """A failed apply must NOT move the pointer; device stays on whatever was last applied."""
    device_id = await _create_device(client)
    v = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 100}})
    vid = v.json()["id"]

    class FakeResponse:
        status_code = 200  # execution service responded, but the run itself failed

        def json(self):
            return {
                "id": "44444444-4444-4444-4444-444444444444",
                "status": "FAILED",
                "error": "device unreachable",
            }

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return FakeResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert await _read_device_current_pointer(device_id) is None


@pytest.mark.asyncio
async def test_apply_calls_execution_with_method_kwargs(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 555, "ip": "10.0.0.5"}},
    )
    vid = create.json()["id"]

    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "11111111-1111-1111-1111-111111111111", "status": "SUCCESS"}

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            return FakeResponse()

    with patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert captured["body"]["action"] == "configure"
    assert captured["body"]["method_kwargs"] == {"vlan": 555, "ip": "10.0.0.5"}
    assert captured["body"]["device_id"] == device_id


@pytest.mark.asyncio
async def test_apply_surfaces_403_verbatim(user_client):
    """Non-admin /execute returns 403; apply should mark the result failed."""
    # Create device + config under admin first by switching overrides.
    app.dependency_overrides[get_current_user_payload] = override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        device_id = await _create_device(ac)
        create = await ac.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100}},
        )
        vid = create.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_user

    class FakeResponse:
        status_code = 403

        def json(self):
            return {"detail": "Admin access required"}

        @property
        def text(self):
            return "Admin access required"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return FakeResponse()

    # The inventory apply gate admits this caller (manage grant); the 403 under
    # test is the downstream execution service's, surfaced as a failed result.
    with (
        patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()),
        patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=True),
        ),
    ):
        resp = await user_client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "403" in body["error"]
    assert "Admin access required" in body["error"]


@pytest.mark.asyncio
async def test_apply_succeeds_for_non_admin_with_acl_grant(user_client):
    """A non-admin with a manage grant on the device can apply (success).

    The inventory apply gate now requires manage-or-active-reservation (matching
    create/restore/schedule); this test grants it, stubs execution's response as
    a success, and verifies the inventory router surfaces that success.
    """
    app.dependency_overrides[get_current_user_payload] = override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        device_id = await _create_device(ac)
        create = await ac.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100}},
        )
        vid = create.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_user

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "22222222-2222-2222-2222-222222222222", "status": "SUCCESS"}

        @property
        def text(self):
            return ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return FakeResponse()

    with (
        patch("app.routers.device_configs.httpx.AsyncClient", lambda **kw: FakeClient()),
        patch(
            "app.routers.device_configs._user_can_manage_device",
            new=AsyncMock(return_value=True),
        ),
    ):
        resp = await user_client.post(
            f"/devices/{device_id}/config-versions/{vid}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["run_id"] == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_create_config_for_unknown_device(client):
    fake = uuid.uuid4()
    resp = await client.post(
        f"/devices/{fake}/config-versions",
        json={"config": {"vlan": 1}},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_layer2_switch_vlan_assignments_validated(client):
    """Layer 2 Switch schema accepts vlan_assignments and rejects unknown keys."""
    device_id = await _create_device(client, connection_type="Layer 2 Switch")
    ok = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan_assignments": {"eth1": 100, "eth2": 200}}},
    )
    assert ok.status_code == 201
    bad = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan_assignments": {"eth1": 5000}}},
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_diff_with_same_versions(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}}
    )
    vid = create.json()["id"]
    resp = await client.get(
        f"/devices/{device_id}/config-versions/diff",
        params={"from": vid, "to": vid},
    )
    assert resp.status_code == 200
    # No changes -> empty diff body.
    assert resp.json()["diff"] == ""


# ---- Apply jobs (roadmap #9 iter 2 piece B) ------------------------------


def _future(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.mark.asyncio
async def test_schedule_apply_job(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(120)},
    )
    assert sched.status_code == 201
    body = sched.json()
    assert body["status"] == "pending"
    assert body["device_id"] == device_id
    assert body["version_id"] == vid


@pytest.mark.asyncio
async def test_schedule_apply_job_rejects_past_timestamp(client):
    """scheduled_for in the past must 422; the scheduler does not catch up missed runs."""
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": past},
    )
    assert resp.status_code == 422
    assert "future" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_schedule_apply_job_rejects_exactly_now(client):
    """scheduled_for == now is rejected to dodge clock-skew fire-before-create races."""
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    # Sub-second past is also past; pick now() to land just at-or-before the handler's clock read.
    now = datetime.now(timezone.utc).isoformat()
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": now},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_apply_job_unknown_version(client):
    device_id = await _create_device(client)
    fake_version = str(uuid.uuid4())
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{fake_version}/schedule",
        json={"scheduled_for": _future()},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_apply_jobs(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    for i in range(3):
        await client.post(
            f"/devices/{device_id}/config-versions/{vid}/schedule",
            json={"scheduled_for": _future(60 + i * 60)},
        )
    resp = await client.get(f"/devices/{device_id}/apply-jobs")
    assert resp.status_code == 200
    assert resp.json()["total"] == 3
    # newest scheduled first
    items = resp.json()["items"]
    assert items[0]["scheduled_for"] > items[2]["scheduled_for"]


@pytest.mark.asyncio
async def test_cancel_pending_job(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future()},
    )
    job_id = sched.json()["id"]

    resp = await client.delete(f"/apply-jobs/{job_id}")
    assert resp.status_code == 204

    listed = await client.get(f"/devices/{device_id}/apply-jobs")
    assert listed.json()["items"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_other_users_job_forbidden(client):
    """Admin schedules a job; non-admin non-owner cannot cancel it."""
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future()},
    )
    job_id = sched.json()["id"]

    # Switch to user, attempt cancel.
    app.dependency_overrides[get_current_user_payload] = override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/apply-jobs/{job_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cancel_already_cancelled_job(client):
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = create.json()["id"]
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future()},
    )
    job_id = sched.json()["id"]
    await client.delete(f"/apply-jobs/{job_id}")
    second = await client.delete(f"/apply-jobs/{job_id}")
    assert second.status_code == 409
