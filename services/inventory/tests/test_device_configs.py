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


@pytest.fixture(autouse=True)
def _noop_reservation_guard():
    """Default: no active reservations on the device, so restore proceeds.

    Mirrors cabling's test_topology_versions._noop_reservation_guard fixture
    for the topology-restore lock this guard is patterned after.
    """
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
async def test_apply_reports_execution_403_by_status_only(user_client):
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
            "app.routers.device_configs._user_has_explicit_manage",
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
    # The status only (issue #1093): execution's plain string detail is not relayed.
    assert body["error"] == "execution answered HTTP 403"


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
            "app.routers.device_configs._user_has_explicit_manage",
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


# ---- driver-published config schema (issue #23, slice 4) ----
#
# create/restore prefer the driver-published schema over the registry, with a
# fail-open fallback to the registry when execution is unreachable. We patch the
# resolver (published_schema_for_device) at the router boundary so these stay
# pure unit tests with no execution service.

# A published schema narrower than the Management registry: only hostname,
# additionalProperties:false. The registry would accept `vlan`/`ip`; this does
# not, which is how we prove the published schema overrides the registry.
_PUBLISHED_MGMT_SCHEMA = {
    "type": "object",
    "properties": {"hostname": {"type": "string", "maxLength": 16}},
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_create_uses_published_schema_accept(client):
    device_id = await _create_device(client)
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=_PUBLISHED_MGMT_SCHEMA),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"hostname": "fw-1"}},
        )
    assert resp.status_code == 201
    assert resp.json()["config"] == {"hostname": "fw-1"}


@pytest.mark.asyncio
async def test_create_published_schema_overrides_registry_reject(client):
    """`vlan` is accepted by the Management registry but NOT by the published
    schema (additionalProperties:false), so the published schema rejects it."""
    device_id = await _create_device(client)
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=_PUBLISHED_MGMT_SCHEMA),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100}},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # role-prefixed, schema-validation wording
    assert "schema validation" in detail


@pytest.mark.asyncio
async def test_create_falls_back_to_registry_when_no_published_schema(client):
    """has_schema:False resolves to None; validation uses the registry, which
    accepts `vlan` and `ip`."""
    device_id = await _create_device(client)
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100, "ip": "10.0.0.1"}},
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_fails_open_to_registry_when_execution_unreachable(client):
    """If the resolver returns None because execution was unreachable, the write
    still succeeds against the registry rather than 503-ing."""
    device_id = await _create_device(client)
    # published_schema_for_device already fails open to None internally; emulate
    # that here. The write must succeed against the registry.
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 4094}},
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_unsafe_published_schema_falls_back_to_registry(client):
    """A hostile published schema (remote $ref) must not break the write; the
    router catches PublishedSchemaError and falls back to the registry."""
    device_id = await _create_device(client)
    hostile = {
        "type": "object",
        "properties": {"x": {"$ref": "http://169.254.169.254/latest/"}},
    }
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=hostile),
    ):
        # registry accepts {"vlan": 10}; the hostile published schema is rejected
        # and we fall back to the registry, so this succeeds.
        resp = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 10}},
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_restore_re_validates_against_published_schema(client):
    """A previously-saved config that the now-tightened published schema rejects
    must 422 on restore, not silently restore an invalid config."""
    device_id = await _create_device(client)
    # First save passes the registry (no published schema yet).
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=None),
    ):
        created = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100}},
        )
    vid = created.json()["id"]

    # Driver later publishes a schema that forbids `vlan`; restore must 422.
    with patch(
        "app.routers.device_configs.published_schema_for_device",
        new=AsyncMock(return_value=_PUBLISHED_MGMT_SCHEMA),
    ):
        restore = await client.post(
            f"/devices/{device_id}/config-versions/{vid}/restore",
            json={},
        )
    assert restore.status_code == 422


# --- GET /devices/{id}/config-versions/latest/internal (issue #20) ---


@pytest.mark.asyncio
async def test_latest_internal_returns_highest_version(client):
    """The internal endpoint serves the newest version with its full config."""
    device_id = await _create_device(client)
    for i in range(3):
        resp = await client.post(
            f"/devices/{device_id}/config-versions",
            json={"config": {"vlan": 100 + i}},
        )
        assert resp.status_code == 201

    resp = await client.get(
        f"/devices/{device_id}/config-versions/latest/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["version_number"] == 3
    assert data["config"] == {"vlan": 102}


@pytest.mark.asyncio
async def test_latest_internal_bad_token(client):
    device_id = await _create_device(client)
    resp = await client.get(
        f"/devices/{device_id}/config-versions/latest/internal",
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_latest_internal_missing_token(client):
    device_id = await _create_device(client)
    resp = await client.get(f"/devices/{device_id}/config-versions/latest/internal")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_latest_internal_no_versions_404(client):
    device_id = await _create_device(client)
    resp = await client.get(
        f"/devices/{device_id}/config-versions/latest/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404
    assert "No config versions" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_latest_internal_unknown_device_404(client):
    resp = await client.get(
        f"/devices/{uuid.uuid4()}/config-versions/latest/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404


# --- Group-visibility gate on the three JWT-gated reads (issue #718) ---
#
# `list_config_versions`, `diff_config_versions`, and `get_config_version` now
# apply the same non-admin group-visibility gate as `GET /devices/{id}`: a
# device outside the caller's groups 404s with the identical detail as the
# device read, so the two endpoints cannot be used to tell "hidden" from
# "absent". Admins stay unfiltered. Mirrors
# test_devices.test_get_device_non_admin_denied_when_not_visible.
#
# One shared AsyncClient per test (built directly, not via the `client`/
# `user_client` fixtures): the device and its versions are seeded as admin,
# then the identity override is switched to a plain user for the read under
# test, all against the same app instance. Using both fixtures at once would
# race their dependency-override setup against each other.


async def _seed_device_with_two_versions(ac) -> tuple[str, str, str]:
    """As admin: create a device with two config versions; return
    (device_id, version_id_1, version_id_2)."""
    device_id = await _create_device(ac)
    v1 = await ac.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 10}, "description": "v1"},
    )
    assert v1.status_code == 201, v1.text
    v2 = await ac.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 20}, "description": "v2"},
    )
    assert v2.status_code == 201, v2.text
    return device_id, v1.json()["id"], v2.json()["id"]


@pytest.mark.asyncio
async def test_list_config_versions_non_admin_denied_when_not_visible():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, _, _ = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value=set()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(f"/devices/{device_id}/config-versions")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_config_versions_non_admin_allowed_when_visible():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, _, _ = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value={uuid.UUID(device_id)}),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(f"/devices/{device_id}/config-versions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_config_versions_admin_unfiltered(client):
    device_id, _, _ = await _seed_device_with_two_versions(client)

    # Admin never consults visibility at all; patch it to blow up to prove
    # the admin path never calls it.
    with patch(
        "app.services.device_visibility._resolve_visible_device_ids",
        new=AsyncMock(side_effect=AssertionError("admin must not check visibility")),
    ):
        resp = await client.get(f"/devices/{device_id}/config-versions")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_diff_config_versions_non_admin_denied_when_not_visible():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, v1, v2 = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value=set()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(f"/devices/{device_id}/config-versions/diff?from={v1}&to={v2}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_diff_config_versions_non_admin_allowed_when_visible():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, v1, v2 = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value={uuid.UUID(device_id)}),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(f"/devices/{device_id}/config-versions/diff?from={v1}&to={v2}")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_diff_config_versions_admin_unfiltered(client):
    device_id, v1, v2 = await _seed_device_with_two_versions(client)

    with patch(
        "app.services.device_visibility._resolve_visible_device_ids",
        new=AsyncMock(side_effect=AssertionError("admin must not check visibility")),
    ):
        resp = await client.get(f"/devices/{device_id}/config-versions/diff?from={v1}&to={v2}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_config_version_non_admin_denied_when_not_visible():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, v1, _ = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value=set()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(f"/devices/{device_id}/config-versions/{v1}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_config_version_non_admin_allowed_when_visible():
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, v1, _ = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value={uuid.UUID(device_id)}),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(f"/devices/{device_id}/config-versions/{v1}")
        assert resp.status_code == 200
        assert resp.json()["id"] == v1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_config_version_admin_unfiltered(client):
    device_id, v1, _ = await _seed_device_with_two_versions(client)

    with patch(
        "app.services.device_visibility._resolve_visible_device_ids",
        new=AsyncMock(side_effect=AssertionError("admin must not check visibility")),
    ):
        resp = await client.get(f"/devices/{device_id}/config-versions/{v1}")
    assert resp.status_code == 200
    assert resp.json()["id"] == v1


@pytest.mark.asyncio
async def test_config_version_reads_404_detail_matches_device_read():
    """The config-version-read 404 must be indistinguishable from the device
    read's 404 (same status, same detail), so the endpoint cannot be used to
    tell "hidden" from "absent" (issue #718)."""
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, v1, _ = await _seed_device_with_two_versions(ac)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value=set()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                config_resp = await ac.get(f"/devices/{device_id}/config-versions/{v1}")

        with patch(
            "app.routers.devices._resolve_visible_device_ids",
            new=AsyncMock(return_value=set()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                device_resp = await ac.get(f"/devices/{device_id}")

        assert config_resp.status_code == device_resp.status_code == 404
        assert config_resp.json()["detail"] == device_resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ---- version numbering under concurrent writes (issue #1095) ----


def test_model_declares_unique_device_version_index():
    """A schema built by create_all must carry the unique index migration 0013 made,
    or concurrent creates on a fresh install store the same number twice."""
    from app.models.device_config_version import DeviceConfigVersion  # noqa: PLC0415

    indexes = {ix.name: ix for ix in DeviceConfigVersion.__table__.indexes}
    ix = indexes["ix_device_config_versions_device_version"]
    assert ix.unique is True
    assert [c.name for c in ix.columns] == ["device_id", "version_number"]


@pytest.mark.asyncio
async def test_create_all_schema_refuses_duplicate_version_number(client):
    """The create_all schema (the one these tests run on) enforces uniqueness."""
    from app.models.device_config_version import DeviceConfigVersion  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    device_id = uuid.UUID(await _create_device(client))
    async with TestSessionLocal() as session:
        for _ in range(2):
            session.add(
                DeviceConfigVersion(
                    device_id=device_id,
                    version_number=1,
                    connection_type="Management",
                    config={},
                    created_by=uuid.uuid4(),
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


def _colliding_numbers(collisions: int):
    """A stand-in for _next_version_number that answers 1 (already taken) for the
    first `collisions` calls, as a concurrent writer that won the race would make
    the read look, then answers the real max+1."""
    from app.routers import device_configs  # noqa: PLC0415

    real = device_configs._next_version_number
    calls = {"n": 0}

    async def fake(db, device_id):
        calls["n"] += 1
        if calls["n"] <= collisions:
            return 1
        return await real(db, device_id)

    return fake, calls


@pytest.mark.asyncio
async def test_create_retries_after_version_number_collision(client):
    device_id = await _create_device(client)
    first = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})
    assert first.json()["version_number"] == 1

    fake, calls = _colliding_numbers(2)
    with patch("app.routers.device_configs._next_version_number", new=fake):
        resp = await client.post(
            f"/devices/{device_id}/config-versions", json={"config": {"vlan": 2}}
        )
    assert resp.status_code == 201
    assert resp.json()["version_number"] == 2
    assert resp.json()["config"] == {"vlan": 2}
    assert calls["n"] == 3
    listing = await client.get(f"/devices/{device_id}/config-versions")
    assert [i["version_number"] for i in listing.json()["items"]] == [2, 1]


@pytest.mark.asyncio
async def test_restore_retries_after_version_number_collision(client):
    device_id = await _create_device(client)
    first = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})
    fake, _ = _colliding_numbers(1)
    with patch("app.routers.device_configs._next_version_number", new=fake):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{first.json()['id']}/restore", json={}
        )
    assert resp.status_code == 201
    assert resp.json()["version_number"] == 2
    assert resp.json()["restored_from_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_create_answers_409_when_version_allocation_keeps_colliding(client):
    device_id = await _create_device(client)
    await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 1}})

    fake, calls = _colliding_numbers(10_000)
    with patch("app.routers.device_configs._next_version_number", new=fake):
        resp = await client.post(
            f"/devices/{device_id}/config-versions", json={"config": {"vlan": 2}}
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Could not allocate a config version number under concurrent writes; retry the request"
    )
    assert calls["n"] == 5
    listing = await client.get(f"/devices/{device_id}/config-versions")
    assert listing.json()["total"] == 1


# ---- cancel versus the scheduler's claim (issue #1088) ----


async def _schedule_job(client) -> tuple[str, str]:
    device_id = await _create_device(client)
    create = await client.post(
        f"/devices/{device_id}/config-versions", json={"config": {"vlan": 7}}
    )
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{create.json()['id']}/schedule",
        json={"scheduled_for": _future()},
    )
    assert sched.status_code == 201
    return device_id, sched.json()["id"]


async def _job_status(job_id: str) -> str:
    from app.models.device_config_apply_job import DeviceConfigApplyJob  # noqa: PLC0415

    async with TestSessionLocal() as session:
        job = await session.get(DeviceConfigApplyJob, uuid.UUID(job_id))
        return job.status


@pytest.mark.asyncio
async def test_cancel_loses_to_a_claim_that_committed_after_its_read(client):
    """The claim lands between the cancel's read (pending) and its write: the cancel
    must change nothing and answer 409, never 204 over a job that is firing."""
    from app.models.device_config_apply_job import DeviceConfigApplyJob  # noqa: PLC0415
    from app.routers.apply_jobs import cancel_apply_job  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    from sqlalchemy import update  # noqa: PLC0415

    _, job_id = await _schedule_job(client)
    async with TestSessionLocal() as cancel_session:
        # The cancel's read: the row is pending in this session's identity map.
        stale = await cancel_session.get(DeviceConfigApplyJob, uuid.UUID(job_id))
        assert stale.status == "pending"
        # The scheduler's claim commits from another session.
        async with TestSessionLocal() as claim_session:
            claim = await claim_session.execute(
                update(DeviceConfigApplyJob)
                .where(
                    DeviceConfigApplyJob.id == uuid.UUID(job_id),
                    DeviceConfigApplyJob.status == "pending",
                )
                .values(status="running")
            )
            await claim_session.commit()
            assert claim.rowcount == 1
        with pytest.raises(HTTPException) as exc:
            await cancel_apply_job(uuid.UUID(job_id), payload=override_admin(), db=cancel_session)
    assert exc.value.status_code == 409
    assert exc.value.detail == "Job is 'running', not cancellable"
    assert await _job_status(job_id) == "running"


@pytest.mark.asyncio
async def test_cancelled_job_is_never_fired_by_a_later_claim(client):
    """The other order: the cancel commits first, so the claim changes no row and
    nothing reaches execution."""
    from app.models.device_config_apply_job import DeviceConfigApplyJob  # noqa: PLC0415
    from app.services.apply_scheduler import fire_job  # noqa: PLC0415

    _, job_id = await _schedule_job(client)
    async with TestSessionLocal() as sched_session:
        # The scheduler read the row while it was still pending.
        job = await sched_session.get(DeviceConfigApplyJob, uuid.UUID(job_id))
        resp = await client.delete(f"/apply-jobs/{job_id}")
        assert resp.status_code == 204
        post = AsyncMock(return_value=("success", None, None))
        with patch("app.services.apply_scheduler._post_internal_execute", new=post):
            await fire_job(sched_session, job, client=None)
    post.assert_not_awaited()
    assert await _job_status(job_id) == "cancelled"


# ---- one success rule for both apply paths (issue #1094) ----


def _apply_client_answering(status_code: int, body):
    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = ""

        def json(self):
            return body

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers=None):
            return FakeResponse()

    return lambda **kw: FakeClient()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "run_id", "error"),
    [
        (
            {"id": "55555555-5555-5555-5555-555555555555"},
            "55555555-5555-5555-5555-555555555555",
            "execution returned non-success status",
        ),
        (
            {"id": "55555555-5555-5555-5555-555555555555", "status": None},
            "55555555-5555-5555-5555-555555555555",
            "execution returned non-success status",
        ),
        (["SUCCESS"], None, "execution returned malformed JSON"),
        ("SUCCESS", None, "execution returned malformed JSON"),
    ],
)
async def test_immediate_apply_without_a_success_status_is_failed(client, body, run_id, error):
    """A 2xx with no run status, or JSON that is not an object, is never a success,
    and the pointer stays put (it used to default to success)."""
    device_id = await _create_device(client)
    v = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 3}})
    with patch("app.routers.device_configs.httpx.AsyncClient", _apply_client_answering(200, body)):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{v.json()['id']}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "version_id": v.json()["id"],
        "run_id": run_id,
        "status": "failed",
        "error": error,
    }
    assert await _read_device_current_pointer(device_id) is None


@pytest.mark.asyncio
async def test_immediate_apply_relays_a_timeout_run_status(client):
    device_id = await _create_device(client)
    v = await client.post(f"/devices/{device_id}/config-versions", json={"config": {"vlan": 3}})
    body = {"id": str(uuid.uuid4()), "status": "TIMEOUT", "error": "driver timed out"}
    with patch("app.routers.device_configs.httpx.AsyncClient", _apply_client_answering(200, body)):
        resp = await client.post(
            f"/devices/{device_id}/config-versions/{v.json()['id']}/apply",
            headers={"Authorization": "Bearer t"},
        )
    assert resp.json()["status"] == "timeout"
    assert resp.json()["error"] == "driver timed out"
    assert await _read_device_current_pointer(device_id) is None
