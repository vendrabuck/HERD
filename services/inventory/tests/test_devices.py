import io
import uuid
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


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


def override_auth_user():
    return {"sub": "00000000-0000-0000-0000-000000000002", "username": "testuser", "role": "user"}


_mock_storage: dict[str, bytes] = {}


def mock_upload_object(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def mock_delete_object(key: str) -> None:
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
        patch("app.services.driver_service.upload_object", side_effect=mock_upload_object),
        patch("app.services.driver_service.delete_object", side_effect=mock_delete_object),
    ):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


TEMPLATE_PAYLOAD = {
    "name": "Firewall",
    "icon": "data:image/png;base64,iVBOR",
    "sections": [
        {
            "name": "General",
            "fields": [
                {"key": "model", "label": "Model", "type": "string", "required": True},
                {"key": "interfaces", "label": "Interfaces", "type": "number"},
                {"key": "ha_enabled", "label": "HA Enabled", "type": "boolean"},
                {
                    "key": "vendor",
                    "label": "Vendor",
                    "type": "dropdown",
                    "options": ["Juniper", "Fortinet", "Cisco"],
                },
            ],
        },
        {
            "name": "Location",
            "fields": [
                {"key": "rack", "label": "Rack", "type": "string"},
            ],
        },
    ],
}


_driver_counter = 0


async def _create_driver(client, name_suffix="") -> str:
    global _driver_counter
    _driver_counter += 1
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={
            "name": f"Test Driver {_driver_counter}{name_suffix}",
            "connection_type": "Management",
        },
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert drv_resp.status_code == 201
    return drv_resp.json()["id"]


async def _create_template(client, vendor: str = "Juniper Networks", model: str = "EX3300") -> str:
    driver_id = await _create_driver(client)
    payload = {
        **TEMPLATE_PAYLOAD,
        "driver_id": driver_id,
        "vendor": vendor,
        "model": model,
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_unknown_identity_template(client) -> str:
    """Create a device template directly via the ORM so it has vendor='unknown'."""
    driver_id = await _create_driver(client)
    from app.models.template import DeviceTemplate

    async with TestSessionLocal() as session:
        tmpl = DeviceTemplate(
            name=f"unknown-{uuid.uuid4()}",
            template_type="device",
            driver_id=uuid.UUID(driver_id),
            exclusive=True,
            icon=None,
            description=None,
            vendor="unknown",
            model="unknown",
            sections=TEMPLATE_PAYLOAD["sections"],
        )
        session.add(tmpl)
        await session.commit()
        await session.refresh(tmpl)
        return str(tmpl.id)


def _device_payload(template_id: str) -> dict:
    return {
        "name": "FW-01",
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {
            "model": "EX3300",
            "interfaces": 8,
            "ha_enabled": False,
            "vendor": "Juniper",
            "rack": "A1",
        },
    }


@pytest.mark.asyncio
async def test_create_device(client):
    tid = await _create_template(client)
    resp = await client.post("/devices", json=_device_payload(tid))
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "FW-01"
    assert data["template_id"] == tid
    assert data["template_name"] == "Firewall"
    assert data["template_icon"] == "data:image/png;base64,iVBOR"
    assert data["topology_type"] == "PHYSICAL"
    assert data["field_data"]["model"] == "EX3300"
    assert data["template_vendor"] == "Juniper Networks"
    assert data["template_model"] == "EX3300"
    assert data["template_part_number"] is None


@pytest.mark.asyncio
async def test_device_response_carries_driver_sha256_and_filename(client):
    """The device payload must expose the driver package's sha256 and filename.

    The execution service keys its driver-package cache on driver_sha256; if the
    device payload omits it, the cache key is a constant and never invalidates
    when a driver package is replaced (e.g. one that newly publishes
    config_schema()). Pin both fields on the create, public GET, and internal
    GET (the route execution actually fetches) so the contract cannot regress.
    """
    driver_id = await _create_driver(client)
    # Read the driver package back to learn its computed sha256 / filename.
    drv = (await client.get(f"/drivers/{driver_id}")).json()
    expected_sha = drv["sha256"]
    expected_filename = drv["filename"]

    tmpl_resp = await client.post(
        "/templates",
        json={**TEMPLATE_PAYLOAD, "driver_id": driver_id, "vendor": "V", "model": "M"},
    )
    tid = tmpl_resp.json()["id"]
    create = await client.post("/devices", json=_device_payload(tid))
    assert create.status_code == 201
    body = create.json()
    assert body["driver_sha256"] == expected_sha
    assert body["driver_filename"] == expected_filename

    device_id = body["id"]
    # Public GET.
    got = (await client.get(f"/devices/{device_id}")).json()
    assert got["driver_sha256"] == expected_sha
    assert got["driver_filename"] == expected_filename

    # Internal GET: this is the exact route execution's fetch_device uses, so it
    # is the contract that actually unblocks cache invalidation.
    internal = await client.get(
        f"/devices/{device_id}/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert internal.status_code == 200
    assert internal.json()["driver_sha256"] == expected_sha
    assert internal.json()["driver_filename"] == expected_filename


@pytest.mark.asyncio
async def test_list_devices(client):
    tid = await _create_template(client)
    await client.post("/devices", json=_device_payload(tid))
    resp = await client.get("/devices")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_get_device(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.get(f"/devices/{device_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == device_id
    assert resp.json()["template_name"] == "Firewall"


@pytest.mark.asyncio
async def test_get_device_non_admin_denied_when_not_visible(client):
    """Regression (IDOR): a non-admin must not read a device outside their group
    visibility via GET /devices/{id}; it returns 404, not the device + field_data."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]

    # Switch the caller to a non-admin for the read.
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.devices._resolve_visible_device_ids",
        new=AsyncMock(return_value=set()),  # user can see no devices
    ):
        denied = await client.get(f"/devices/{device_id}")
    assert denied.status_code == 404

    # Same user, but now the device is in their visible set: allowed.
    with patch(
        "app.routers.devices._resolve_visible_device_ids",
        new=AsyncMock(return_value={uuid.UUID(device_id)}),
    ):
        allowed = await client.get(f"/devices/{device_id}")
    assert allowed.status_code == 200
    assert allowed.json()["id"] == device_id


@pytest.mark.asyncio
async def test_single_device_read_fails_closed_on_auth_outage(client):
    """When the auth service is down (visibility resolution raises 503), a
    non-admin single-device read returns 503, not the device. Fail closed."""
    from fastapi import HTTPException

    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]

    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.devices._resolve_visible_device_ids",
        new=AsyncMock(side_effect=HTTPException(status_code=503, detail="auth down")),
    ):
        resp = await client.get(f"/devices/{device_id}")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_device_list_fails_closed_on_auth_outage(client):
    """When the auth service is down, a non-admin device list returns 503
    instead of fail-open showing every device."""
    from fastapi import HTTPException

    tid = await _create_template(client)
    await client.post("/devices", json=_device_payload(tid))

    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.devices._resolve_visible_device_ids",
        new=AsyncMock(side_effect=HTTPException(status_code=503, detail="auth down")),
    ):
        resp = await client.get("/devices")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_update_device(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.put(f"/devices/{device_id}", json={"status": "OFFLINE"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OFFLINE"


@pytest.mark.asyncio
async def test_delete_device(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.delete(f"/devices/{device_id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/devices/{device_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_filter_by_topology_type(client):
    tid = await _create_template(client)
    await client.post("/devices", json={**_device_payload(tid), "topology_type": "PHYSICAL"})
    await client.post(
        "/devices",
        json={**_device_payload(tid), "name": "Cloud-FW-01", "topology_type": "CLOUD"},
    )
    resp = await client.get("/devices?topology_type=PHYSICAL")
    assert resp.status_code == 200
    assert all(d["topology_type"] == "PHYSICAL" for d in resp.json()["items"])


@pytest.mark.asyncio
async def test_user_cannot_create_device(user_client, client):
    tid = await _create_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await user_client.post("/devices", json=_device_payload(tid))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_update_device(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.put(f"/devices/{device_id}", json={"status": "OFFLINE"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete_device(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.delete(f"/devices/{device_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_can_list_devices(user_client):
    # Mock group-visibility resolution so the list path does not reach the
    # (absent) auth service. Visibility resolution failing closed is covered by
    # test_device_list_fails_closed_on_auth_outage.
    with patch(
        "app.routers.devices._resolve_visible_device_ids",
        new=AsyncMock(return_value=set()),
    ):
        resp = await user_client.get("/devices")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_internal_status_update(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RESERVED"


@pytest.mark.asyncio
async def test_internal_status_update_bad_token(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403


# --- fault-injection seam (issue #214) ---


@pytest.mark.asyncio
async def test_fault_injection_fails_sentinel_device(client, monkeypatch):
    """With HERD_FAULT_INJECTION on, a sentinel-named device's status update is
    refused with 503 and the device status is left UNCHANGED (the fault raises
    before set_device_status runs)."""
    monkeypatch.setenv("HERD_FAULT_INJECTION", "1")
    tid = await _create_template(client)
    payload = {**_device_payload(tid), "name": "FW-__herd_fault_status__-01"}
    create_resp = await client.post("/devices", json=payload)
    device_id = create_resp.json()["id"]

    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == ("fault injection: simulated inventory status-update failure")

    # The status update never ran: the device is still AVAILABLE.
    get_resp = await client.get(
        f"/devices/{device_id}/internal", headers={"X-Internal-Token": "test-token"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_fault_injection_skips_normal_device(client, monkeypatch):
    """With the env on but a normally-named device, the status update succeeds:
    the sentinel name gate, not the env alone, selects the failing device."""
    monkeypatch.setenv("HERD_FAULT_INJECTION", "1")
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]

    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RESERVED"


@pytest.mark.asyncio
async def test_fault_injection_inert_when_env_unset(client, monkeypatch):
    """With HERD_FAULT_INJECTION unset (default), even a sentinel-named device
    updates normally: the env gate, not just the name, must be tripped."""
    monkeypatch.delenv("HERD_FAULT_INJECTION", raising=False)
    tid = await _create_template(client)
    payload = {**_device_payload(tid), "name": "FW-__herd_fault_status__-02"}
    create_resp = await client.post("/devices", json=payload)
    device_id = create_resp.json()["id"]

    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RESERVED"


# --- 404 tests ---


@pytest.mark.asyncio
async def test_get_device_not_found(client):
    resp = await client.get(f"/devices/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_device_not_found(client):
    resp = await client.put(f"/devices/{uuid.uuid4()}", json={"status": "OFFLINE"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_device_not_found(client):
    resp = await client.delete(f"/devices/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- Internal status missing token ---


@pytest.mark.asyncio
async def test_internal_status_update_missing_token(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
    )
    assert resp.status_code == 422


# --- Filter by status ---


@pytest.mark.asyncio
async def test_filter_by_status(client):
    tid = await _create_template(client)
    await client.post("/devices", json=_device_payload(tid))
    create2 = await client.post(
        "/devices", json={**_device_payload(tid), "name": "FW-02", "status": "AVAILABLE"}
    )
    device2_id = create2.json()["id"]
    await client.put(f"/devices/{device2_id}", json={"status": "OFFLINE"})
    resp = await client.get("/devices?status=AVAILABLE")
    assert resp.status_code == 200
    devices = resp.json()["items"]
    assert all(d["status"] == "AVAILABLE" for d in devices)
    assert len(devices) == 1


# --- Filter by template_id ---


@pytest.mark.asyncio
async def test_filter_by_template_id(client):
    tid = await _create_template(client)
    # Create a second driver + template
    content = b"PK\x03\x04test"
    drv2 = await client.post(
        "/drivers",
        data={"name": "Switch Driver", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    switch_sections = [
        {
            "name": "Info",
            "fields": [
                {"key": "ports", "label": "Ports", "type": "number"},
            ],
        },
    ]
    resp2 = await client.post(
        "/templates",
        json={
            "name": "Switch",
            "driver_id": drv2.json()["id"],
            "vendor": "Generic",
            "model": "Switch",
            "sections": switch_sections,
        },
    )
    tid2 = resp2.json()["id"]

    await client.post("/devices", json=_device_payload(tid))
    await client.post(
        "/devices",
        json={
            "name": "SW-01",
            "template_id": tid2,
            "topology_type": "PHYSICAL",
            "field_data": {"ports": 48},
        },
    )
    resp = await client.get(f"/devices?template_id={tid}")
    assert resp.status_code == 200
    devices = resp.json()["items"]
    assert len(devices) == 1
    assert devices[0]["template_id"] == tid


@pytest.mark.asyncio
async def test_pagination_limit(client):
    tid = await _create_template(client)
    for i in range(3):
        await client.post("/devices", json={**_device_payload(tid), "name": f"FW-{i}"})
    resp = await client.get("/devices?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2


@pytest.mark.asyncio
async def test_pagination_skip(client):
    tid = await _create_template(client)
    for i in range(3):
        await client.post("/devices", json={**_device_payload(tid), "name": f"FW-{i}"})
    resp = await client.get("/devices?skip=2")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_combined_filters(client):
    tid = await _create_template(client)
    await client.post("/devices", json=_device_payload(tid))
    await client.post(
        "/devices",
        json={**_device_payload(tid), "name": "Cloud-FW", "topology_type": "CLOUD"},
    )
    resp = await client.get(f"/devices?template_id={tid}&topology_type=PHYSICAL")
    assert resp.status_code == 200
    devices = resp.json()["items"]
    assert len(devices) == 1
    assert devices[0]["topology_type"] == "PHYSICAL"


@pytest.mark.asyncio
async def test_device_full_lifecycle(client):
    """Create, update name, GET (verify update), delete, GET (verify 404)."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    assert create_resp.status_code == 201
    device_id = create_resp.json()["id"]
    update_resp = await client.put(f"/devices/{device_id}", json={"name": "FW-RENAMED"})
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "FW-RENAMED"
    get_resp = await client.get(f"/devices/{device_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "FW-RENAMED"
    del_resp = await client.delete(f"/devices/{device_id}")
    assert del_resp.status_code == 204
    gone_resp = await client.get(f"/devices/{device_id}")
    assert gone_resp.status_code == 404


# --- Field validation tests ---


@pytest.mark.asyncio
async def test_create_device_missing_required_field(client):
    tid = await _create_template(client)
    payload = _device_payload(tid)
    del payload["field_data"]["model"]  # model is required
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 422
    assert "model" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_device_wrong_field_type(client):
    tid = await _create_template(client)
    payload = _device_payload(tid)
    payload["field_data"]["interfaces"] = "not-a-number"
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 422
    assert "interfaces" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_device_invalid_dropdown_value(client):
    tid = await _create_template(client)
    payload = _device_payload(tid)
    payload["field_data"]["vendor"] = "InvalidVendor"
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 422
    assert "vendor" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_device_unknown_field(client):
    tid = await _create_template(client)
    payload = _device_payload(tid)
    payload["field_data"]["nonexistent"] = "value"
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 422
    assert "nonexistent" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_device_invalid_template(client):
    resp = await client.post(
        "/devices",
        json={
            "name": "FW-01",
            "template_id": str(uuid.uuid4()),
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert resp.status_code == 422
    assert "Template not found" in resp.json()["detail"]


# --- Template delete blocked by devices ---


@pytest.mark.asyncio
async def test_delete_template_blocked_by_devices(client):
    tid = await _create_template(client)
    await client.post("/devices", json=_device_payload(tid))
    resp = await client.delete(f"/templates/{tid}")
    assert resp.status_code == 409
    assert "devices still reference" in resp.json()["detail"]


# --- Additional field validation tests ---


@pytest.mark.asyncio
async def test_create_device_with_multiword_dropdown_value(client):
    tid = await _create_template(client)
    payload = _device_payload(tid)
    payload["field_data"]["vendor"] = "Juniper"
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201
    assert resp.json()["field_data"]["vendor"] == "Juniper"


@pytest.mark.asyncio
async def test_create_device_optional_fields_omitted(client):
    tid = await _create_template(client)
    payload = {
        "name": "FW-Minimal",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {
            "model": "EX3300",
        },
    }
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201
    assert resp.json()["field_data"]["model"] == "EX3300"


@pytest.mark.asyncio
async def test_create_device_empty_field_data_no_required(client):
    """Template with no required fields; empty field_data should succeed."""
    driver_id = await _create_driver(client)
    template_payload = {
        "name": "Simple Template",
        "driver_id": driver_id,
        "vendor": "V",
        "model": "M",
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {"key": "notes", "label": "Notes", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    tid = resp.json()["id"]
    device_resp = await client.post(
        "/devices",
        json={
            "name": "DEV-01",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert device_resp.status_code == 201


@pytest.mark.asyncio
async def test_create_device_empty_field_data_with_required(client):
    """Template with required fields; empty field_data should fail."""
    tid = await _create_template(client)
    resp = await client.post(
        "/devices",
        json={
            "name": "FW-Bad",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert resp.status_code == 422
    assert "model" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_device_field_data(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    new_field_data = {
        "model": "EX4400",
        "interfaces": 16,
        "ha_enabled": True,
        "vendor": "Juniper",
        "rack": "B2",
    }
    resp = await client.put(f"/devices/{device_id}", json={"field_data": new_field_data})
    assert resp.status_code == 200
    assert resp.json()["field_data"]["model"] == "EX4400"
    assert resp.json()["field_data"]["interfaces"] == 16


@pytest.mark.asyncio
async def test_update_device_invalid_field_data(client):
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.put(
        f"/devices/{device_id}",
        json={"field_data": {"model": "EX4400", "vendor": "InvalidVendor"}},
    )
    assert resp.status_code == 422
    assert "vendor" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_device_boolean_field(client):
    tid = await _create_template(client)
    for val in [True, False]:
        payload = _device_payload(tid)
        payload["name"] = f"FW-bool-{val}"
        payload["field_data"]["ha_enabled"] = val
        resp = await client.post("/devices", json=payload)
        assert resp.status_code == 201
        assert resp.json()["field_data"]["ha_enabled"] is val


@pytest.mark.asyncio
async def test_create_device_number_field_float(client):
    tid = await _create_template(client)
    payload = _device_payload(tid)
    payload["field_data"]["interfaces"] = 8.5
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201
    assert resp.json()["field_data"]["interfaces"] == 8.5


@pytest.mark.asyncio
async def test_create_device_with_port_template_fails(client):
    """Using a port template to create a device should fail."""
    port_template = {
        "name": "GigE Port",
        "template_type": "port",
        "sections": [
            {
                "name": "Config",
                "fields": [
                    {"key": "speed", "label": "Speed", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=port_template)
    assert resp.status_code == 201
    pt_id = resp.json()["id"]
    resp = await client.post(
        "/devices",
        json={
            "name": "Bad Device",
            "template_id": pt_id,
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert resp.status_code == 422
    assert "not a device template" in resp.json()["detail"]


# --- No-auth and pagination tests ---


@pytest.fixture
async def no_auth_client():
    """Client with DB override but no auth override, so real JWT validation runs."""
    app.dependency_overrides[get_db] = override_get_db
    # Do NOT override get_current_user_payload
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_devices_invalid_token(no_auth_client):
    """Request with invalid Bearer token returns 401."""
    resp = await no_auth_client.get("/devices", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pagination_skip_and_limit_combined(client):
    """Create 5 devices, skip=1 limit=2 returns exactly 2."""
    tid = await _create_template(client)
    for i in range(5):
        await client.post("/devices", json={**_device_payload(tid), "name": f"FW-{i}"})
    resp = await client.get("/devices?skip=1&limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2


# --- Duplicate name tests ---


@pytest.mark.asyncio
async def test_create_device_duplicate_name(client):
    """Creating two devices with the same name returns 409."""
    tid = await _create_template(client)
    resp1 = await client.post("/devices", json=_device_payload(tid))
    assert resp1.status_code == 201
    resp2 = await client.post("/devices", json=_device_payload(tid))
    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"]


@pytest.mark.asyncio
async def test_update_device_duplicate_name(client):
    """Updating a device name to match an existing device returns 409."""
    tid = await _create_template(client)
    await client.post("/devices", json=_device_payload(tid))
    resp2 = await client.post("/devices", json={**_device_payload(tid), "name": "FW-02"})
    assert resp2.status_code == 201
    device2_id = resp2.json()["id"]
    resp = await client.put(f"/devices/{device2_id}", json={"name": "FW-01"})
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


# --- Default value tests ---


@pytest.mark.asyncio
async def test_create_device_with_default_applied(client):
    """Field with default; field_data omits it; verify default is auto-filled."""
    driver_id = await _create_driver(client)
    template_payload = {
        "name": "DefaultTemplate",
        "driver_id": driver_id,
        "vendor": "V",
        "model": "M",
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {
                        "key": "region",
                        "label": "Region",
                        "type": "string",
                        "required": True,
                        "default": "us-west-1",
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    assert resp.status_code == 201
    tid = resp.json()["id"]
    device_resp = await client.post(
        "/devices",
        json={
            "name": "DEV-Default",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert device_resp.status_code == 201
    assert device_resp.json()["field_data"]["region"] == "us-west-1"


@pytest.mark.asyncio
async def test_default_not_overridden(client):
    """Field with default; field_data provides a value; verify user value wins."""
    driver_id = await _create_driver(client)
    template_payload = {
        "name": "DefaultTemplate2",
        "driver_id": driver_id,
        "vendor": "V",
        "model": "M",
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {
                        "key": "region",
                        "label": "Region",
                        "type": "string",
                        "required": True,
                        "default": "us-west-1",
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    assert resp.status_code == 201
    tid = resp.json()["id"]
    device_resp = await client.post(
        "/devices",
        json={
            "name": "DEV-Override",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "field_data": {"region": "eu-central-1"},
        },
    )
    assert device_resp.status_code == 201
    assert device_resp.json()["field_data"]["region"] == "eu-central-1"


@pytest.mark.asyncio
async def test_template_default_type_mismatch(client):
    """Template with wrong-type default returns 422."""
    template_payload = {
        "name": "BadDefault",
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {
                        "key": "count",
                        "label": "Count",
                        "type": "number",
                        "default": "not-a-number",
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_template_dropdown_default_not_in_options(client):
    """Dropdown default not in options returns 422."""
    template_payload = {
        "name": "BadDropdown",
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {
                        "key": "color",
                        "label": "Color",
                        "type": "dropdown",
                        "options": ["red", "blue"],
                        "default": "green",
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_template_dropdown_default_valid(client):
    """Dropdown default in options returns 201."""
    driver_id = await _create_driver(client)
    template_payload = {
        "name": "GoodDropdown",
        "driver_id": driver_id,
        "vendor": "V",
        "model": "M",
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {
                        "key": "color",
                        "label": "Color",
                        "type": "dropdown",
                        "options": ["red", "blue", "green"],
                        "default": "blue",
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    assert resp.status_code == 201
    assert resp.json()["sections"][0]["fields"][0]["default"] == "blue"


# --- Exclusive flag on device response ---


@pytest.mark.asyncio
async def test_internal_status_update_nonexistent_device(client):
    """Valid token, bad device ID returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await client.post(
        f"/devices/{fake_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_internal_status_update_to_available(client):
    """Set status to AVAILABLE."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    # First set to RESERVED
    await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    # Then back to AVAILABLE
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "AVAILABLE"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_internal_status_update_to_maintenance(client):
    """Set status to MAINTENANCE."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "MAINTENANCE"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "MAINTENANCE"


@pytest.mark.asyncio
async def test_pagination_skip_exceeds_total(client):
    """skip=10 on 3 devices returns []."""
    tid = await _create_template(client)
    for i in range(3):
        await client.post("/devices", json={**_device_payload(tid), "name": f"FW-skip-{i}"})
    resp = await client.get("/devices?skip=10")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.fixture
async def superadmin_client():
    def override_auth_superadmin():
        return {
            "sub": "00000000-0000-0000-0000-000000000003",
            "username": "testsuperadmin",
            "role": "superadmin",
        }

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_superadmin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_superadmin_can_create_device(superadmin_client):
    """Superadmin write access works."""
    driver_id = await _create_driver(superadmin_client)
    resp = await superadmin_client.post(
        "/templates",
        json={**TEMPLATE_PAYLOAD, "driver_id": driver_id, "vendor": "V", "model": "M"},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]
    resp = await superadmin_client.post("/devices", json=_device_payload(tid))
    assert resp.status_code == 201
    assert resp.json()["name"] == "FW-01"


@pytest.mark.asyncio
async def test_list_devices_empty(client):
    """No devices returns 200 + []."""
    resp = await client.get("/devices")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_update_device_name_to_same_name(client):
    """Rename to current name succeeds (no self-conflict)."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.put(f"/devices/{device_id}", json={"name": "FW-01"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "FW-01"


@pytest.mark.asyncio
async def test_device_response_includes_exclusive(client):
    """Create device with non-exclusive template; verify exclusive: false in GET response."""
    driver_id = await _create_driver(client)
    template_payload = {
        "name": "Shared Router",
        "driver_id": driver_id,
        "vendor": "V",
        "model": "M",
        "exclusive": False,
        "sections": [
            {
                "name": "Info",
                "fields": [
                    {"key": "model", "label": "Model", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=template_payload)
    assert resp.status_code == 201
    tid = resp.json()["id"]
    device_resp = await client.post(
        "/devices",
        json={
            "name": "RTR-01",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert device_resp.status_code == 201
    assert device_resp.json()["exclusive"] is False
    # Also verify via GET
    device_id = device_resp.json()["id"]
    get_resp = await client.get(f"/devices/{device_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["exclusive"] is False


@pytest.mark.asyncio
async def test_search_devices_by_name(client):
    tid = await _create_template(client)
    await client.post("/devices", json={**_device_payload(tid), "name": "Alpha-FW-01"})
    await client.post("/devices", json={**_device_payload(tid), "name": "Alpha-FW-02"})
    await client.post("/devices", json={**_device_payload(tid), "name": "Bravo-SW-01"})

    resp = await client.get("/devices", params={"search": "Alpha"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    names = [d["name"] for d in data["items"]]
    assert "Alpha-FW-01" in names
    assert "Alpha-FW-02" in names
    assert "Bravo-SW-01" not in names


@pytest.mark.asyncio
async def test_search_devices_no_match(client):
    tid = await _create_template(client)
    await client.post("/devices", json={**_device_payload(tid), "name": "Alpha-FW-01"})

    resp = await client.get("/devices", params={"search": "nonexistent"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


# --- Health endpoint ---


@pytest.mark.asyncio
async def test_health_endpoint(client):
    """GET /health returns 200 with service info."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "inventory"


# --- dut_only filter ---


@pytest.mark.asyncio
async def test_list_devices_dut_only_filter(client):
    """dut_only=true filters out infrastructure devices."""
    # Create management (DUT) driver + template + device
    dut_driver_id = await _create_driver(client, name_suffix="-dut")
    resp = await client.post(
        "/templates",
        json={
            **TEMPLATE_PAYLOAD,
            "name": "DUT Template",
            "driver_id": dut_driver_id,
            "vendor": "V",
            "model": "M",
        },
    )
    assert resp.status_code == 201
    dut_tid = resp.json()["id"]
    await client.post(
        "/devices",
        json={
            "name": "DUT-Device",
            "template_id": dut_tid,
            "topology_type": "PHYSICAL",
            "field_data": {"model": "EX3300"},
        },
    )

    # Create infrastructure driver + template + device
    content = b"PK\x03\x04test"
    infra_drv = await client.post(
        "/drivers",
        data={"name": "Infra Driver DUT Test", "connection_type": "Layer 1 Switch"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert infra_drv.status_code == 201
    infra_driver_id = infra_drv.json()["id"]
    resp = await client.post(
        "/templates",
        json={
            **TEMPLATE_PAYLOAD,
            "name": "Infra Template",
            "driver_id": infra_driver_id,
            "vendor": "V",
            "model": "M",
        },
    )
    assert resp.status_code == 201
    infra_tid = resp.json()["id"]
    await client.post(
        "/devices",
        json={
            "name": "Infra-Device",
            "template_id": infra_tid,
            "topology_type": "PHYSICAL",
            "field_data": {"model": "L1-256"},
        },
    )

    # Without dut_only: both devices
    resp_all = await client.get("/devices")
    assert resp_all.status_code == 200
    all_names = [d["name"] for d in resp_all.json()["items"]]
    assert "DUT-Device" in all_names
    assert "Infra-Device" in all_names

    # With dut_only=true: only DUT
    resp_dut = await client.get("/devices", params={"dut_only": "true"})
    assert resp_dut.status_code == 200
    dut_names = [d["name"] for d in resp_dut.json()["items"]]
    assert "DUT-Device" in dut_names
    assert "Infra-Device" not in dut_names


@pytest.mark.asyncio
async def test_list_devices_search_empty(client):
    """Search with no matching results returns empty list and total 0."""
    tid = await _create_template(client)
    await client.post("/devices", json={**_device_payload(tid), "name": "Device-One"})

    resp = await client.get("/devices", params={"search": "zzz-no-match"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


# --- Internal device endpoint ---


@pytest.mark.asyncio
async def test_internal_get_device(client):
    """GET /devices/{id}/internal with valid token returns device."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.get(
        f"/devices/{device_id}/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == device_id


@pytest.mark.asyncio
async def test_internal_get_device_bad_token(client):
    """GET /devices/{id}/internal with bad token returns 403."""
    tid = await _create_template(client)
    create_resp = await client.post("/devices", json=_device_payload(tid))
    device_id = create_resp.json()["id"]
    resp = await client.get(
        f"/devices/{device_id}/internal",
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_get_device_not_found(client):
    """GET /devices/{id}/internal with valid token but nonexistent device returns 404."""
    resp = await client.get(
        f"/devices/{uuid.uuid4()}/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404


# --- Template identity guard on device-create ---


@pytest.mark.asyncio
async def test_create_device_blocked_when_template_vendor_unknown(client):
    """Device-create rejects 422 when the template has vendor='unknown'."""
    tid = await _create_unknown_identity_template(client)
    resp = await client.post("/devices", json=_device_payload(tid))
    assert resp.status_code == 422
    assert "unknown hardware identity" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_device_succeeds_when_template_identity_known(client):
    """Device-create returns 201 when the template has a known vendor and model."""
    tid = await _create_template(client, vendor="Cisco", model="Catalyst 9300")
    resp = await client.post("/devices", json=_device_payload(tid))
    assert resp.status_code == 201
    data = resp.json()
    assert data["template_vendor"] == "Cisco"
    assert data["template_model"] == "Catalyst 9300"
